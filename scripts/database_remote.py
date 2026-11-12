"""Runs inside the deployed LiteLLM container through the protected exec channel."""

import asyncio
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import quote
import uuid


def database_url(host, database, user, token):
    if not isinstance(host, str) or not re.fullmatch(r"[a-z0-9-]+\.postgres\.database\.azure\.com", host):
        raise ValueError("Database host must be an Azure PostgreSQL server hostname without credentials or a path.")
    configured = os.environ.get("DATABASE_HOST")
    if configured and host != configured:
        raise ValueError("Database host differs from the approved deployment.")
    if database not in {"postgres", "litellm"}:
        raise ValueError("Unexpected database name.")
    from litellm.proxy.db.db_url_settings import translate_libpq_ssl_params

    url = (
        f"postgresql://{quote(user, safe='')}:{quote(token, safe='')}@{host}:5432/{database}"
        "?sslmode=verify-full&sslrootcert=/etc/ssl/certs/ca-certificates.crt"
    )
    return translate_libpq_ssl_params(url)


async def bootstrap(payload):
    from prisma import Prisma

    object_id = str(uuid.UUID(payload["appPrincipalId"]))
    role = "litellm_app"
    admin = Prisma(datasource={"url": database_url(
        payload["host"], "postgres", payload["adminName"], payload["token"],
    )})
    await admin.connect()
    try:
        roles = await admin.query_raw("SELECT rolname::text AS rolname FROM pg_roles WHERE rolname = $1", role)
        if roles:
            principals = await admin.query_raw(
                "SELECT to_jsonb(principal) AS principal FROM pgaadauth_list_principals(false) AS principal"
            )
            if any(not isinstance(row.get("principal"), dict) for row in principals):
                raise ValueError("Database returned invalid Entra principal metadata.")
            matching = [
                {key.lower().replace("_", ""): value for key, value in row["principal"].items()}
                for row in principals
            ]
            if not any(
                (row.get("rolename") == role or row.get("rolname") == role)
                and str(row.get("objectid")) == object_id
                for row in matching
            ):
                raise ValueError("Existing database role is not mapped to the expected app identity.")
        else:
            await admin.query_raw(
                "SELECT pgaadauth_create_principal_with_oid($1, $2, 'service', false, false)::text AS result",
                role, object_id,
            )
        await admin.execute_raw('GRANT CONNECT, CREATE, TEMPORARY ON DATABASE "litellm" TO "litellm_app"')
    finally:
        await admin.disconnect()
    app_db = Prisma(datasource={"url": database_url(
        payload["host"], "litellm", payload["adminName"], payload["token"],
    )})
    await app_db.connect()
    try:
        await app_db.execute_raw('GRANT USAGE, CREATE ON SCHEMA public TO "litellm_app"')
    finally:
        await app_db.disconnect()
    return {"role": role, "serverAdmin": False, "bootstrap": "passed"}


def app_database_url(host):
    from azure.identity import DefaultAzureCredential

    with DefaultAzureCredential() as credential:
        token = credential.get_token("https://ossrdbms-aad.database.windows.net/.default").token
    return database_url(host, "litellm", "litellm_app", token)


def migration_assets():
    from importlib.resources import files

    candidates = [Path(str(files("litellm_proxy_extras"))) / "schema.prisma"]
    for schema in candidates:
        if schema.is_file() and list((schema.parent / "migrations").glob("*/migration.sql")):
            break
    else:
        raise RuntimeError("The pinned image does not contain its versioned Prisma migration files.")
    cli = os.environ.get("PRISMA_CLI_PATH")
    if not cli or not Path(cli).is_file() or not os.access(cli, os.X_OK):
        raise RuntimeError("The pinned image native Prisma CLI is unavailable.")
    return schema, cli


async def inspect_schema(host):
    from prisma import Prisma

    db = Prisma(datasource={"url": app_database_url(host)})
    await db.connect()
    try:
        rows = await db.query_raw(
            'SELECT COUNT(*)::int AS applied FROM "_prisma_migrations" '
            'WHERE finished_at IS NOT NULL AND rolled_back_at IS NULL'
        )
        if not rows or rows[0]["applied"] < 1:
            raise ValueError("No completed Prisma migrations were found.")
        incomplete = await db.query_raw(
            'SELECT COUNT(*)::int AS pending FROM "_prisma_migrations" '
            'WHERE finished_at IS NULL AND rolled_back_at IS NULL'
        )
        if incomplete[0]["pending"]:
            raise ValueError("Database contains an unfinished Prisma migration.")
        tables = await db.query_raw(
            "SELECT to_regclass('public.\"LiteLLM_UserTable\"')::text AS users"
        )
        if not tables[0]["users"]:
            raise ValueError("LiteLLM user table is missing.")
        return {"database": "connected", "completedMigrations": rows[0]["applied"]}
    finally:
        await db.disconnect()


def main(payload):
    action = payload["action"]
    if action == "probe":
        schema, cli = migration_assets()
        result = {
            "prismaAvailable": importlib.util.find_spec("prisma") is not None,
            "managedIdentityAvailable": bool(os.environ.get("IDENTITY_ENDPOINT") and os.environ.get("IDENTITY_HEADER")),
            "nativeMigrationCliAvailable": bool(cli),
            "versionedMigrationFilesAvailable": schema.is_file(),
        }
        if not all(result.values()):
            raise RuntimeError("Deployed container lacks a required database bootstrap capability.")
        return result
    if action == "bootstrap":
        return asyncio.run(bootstrap(payload))
    if action == "migrate":
        schema, cli = migration_assets()
        environment = {
            **os.environ,
            "AZURE_POSTGRESQL_AUTH": "true",
            "DATABASE_HOST": payload["host"],
            "DATABASE_PORT": "5432",
            "DATABASE_USER": "litellm_app",
            "DATABASE_NAME": "litellm",
            "DATABASE_SSLMODE": "verify-full",
            "DATABASE_SSLROOTCERT": "/etc/ssl/certs/ca-certificates.crt",
        }
        for key in ("DATABASE_URL", "DIRECT_URL", "DATABASE_PASSWORD", "IAM_TOKEN_DB_AUTH"):
            environment.pop(key, None)
        environment["DATABASE_URL"] = app_database_url(payload["host"])
        completed = subprocess.run(
            [cli, "migrate", "deploy", "--schema", str(schema)],
            env=environment, cwd=str(schema.parent), capture_output=True, timeout=600, check=False,
        )
        if completed.returncode:
            raise RuntimeError(f"Pinned Prisma migrate deploy exited {completed.returncode}.")
        return asyncio.run(inspect_schema(payload["host"]))
    raise ValueError("Unsupported database operation.")


if __name__ == "__main__":
    payload = globals().get("PAYLOAD")
    if not isinstance(payload, dict):
        raise ValueError("Database operations require a protected exec payload.")
    print("LITELLM_DB_RESULT:" + json.dumps({"ok": True, "result": main(payload)}), flush=True)
