import argparse
import ipaddress
import json
import secrets
import subprocess
import sys
import time
import urllib.error

from common import (
    access_token, azure_json, load_config, local_settings, master_key,
    persist_local_settings, read_state, request_json, write_state,
)
from container_exec import API_VERSION, run_database_operation
from deploy import deployment_outputs, run_action, verify_target
from smoke_test import (
    admin_login, completion, request_with_retry, test_gateway,
    test_litellm, test_model_hub, verify_admin_access, wait_for_database,
)


def validate_ips(values):
    if not isinstance(values, list) or not values:
        raise ValueError("The database firewall requires the app's reported outbound IP addresses.")
    parsed = [ipaddress.ip_address(value) for value in values]
    if any(address.version != 4 or not address.is_global for address in parsed):
        raise ValueError("Database firewall addresses must be explicit public IPv4 addresses.")
    return [str(address) for address in sorted(set(parsed))]


def internal_secrets(settings, *, generate):
    result = {}
    updates = {}
    for name in ("UI_PASSWORD", "LITELLM_SALT_KEY"):
        value = settings.get(name)
        if not value:
            if not generate:
                raise ValueError(f"Recover the existing {name} in env/.env; refusing silent secret rotation.")
            value = secrets.token_urlsafe(32)
            updates[name] = value
        if len(value) < 32 or any(character in value for character in "\r\n"):
            raise ValueError(f"{name} must be a single-line secret of at least 32 characters.")
        result[name] = value
    if updates:
        persist_local_settings(updates)
    return result


def firewall_rules(cfg):
    servers = azure_json([
        "resource", "list", "--subscription", cfg.subscription_id,
        "--resource-group", cfg.resource_group,
        "--resource-type", "Microsoft.DBforPostgreSQL/flexibleServers",
        "--query", f"[?name=='{cfg.postgres_server_name}'].id",
    ])
    if not servers:
        return []
    return azure_json([
        "rest", "--method", "get", "--url",
        f"https://management.azure.com{servers[0]}/firewallRules?api-version=2026-10-01",
    ])["value"]


def unchanged_firewall_ips(allowed, rules):
    return sorted({
        row["properties"]["startIpAddress"]
        for row in rules
        if row["properties"]["startIpAddress"] == row["properties"]["endIpAddress"]
        and row["properties"]["startIpAddress"] in allowed
    })


def verify_firewall(cfg, allowed):
    rules = firewall_rules(cfg)
    if any(row["properties"]["startIpAddress"] != row["properties"]["endIpAddress"] for row in rules):
        raise ValueError("Database firewall contains an unexpected IP range.")
    if {row["properties"]["startIpAddress"] for row in rules} != set(allowed):
        raise ValueError("Database firewall does not match the Container App egress addresses.")


def parameters(cfg, app, user, credentials, *, admin_ui):
    allowed = validate_ips(app["properties"].get("outboundIpAddresses"))
    principal_name = user.get("userPrincipalName") or user.get("mail")
    if not principal_name:
        raise ValueError("Signed-in Entra user has no userPrincipalName or mail.")
    return {
        "enableDatabase": {"value": True},
        "enableAdminUi": {"value": admin_ui},
        "postgresAllowedIps": {"value": allowed},
        "postgresUnchangedFirewallIps": {"value": unchanged_firewall_ips(allowed, firewall_rules(cfg))},
        "entraAdminObjectId": {"value": user["id"]},
        "entraAdminName": {"value": principal_name},
        "uiPassword": {"value": credentials["UI_PASSWORD"]},
        "litellmSaltKey": {"value": credentials["LITELLM_SALT_KEY"]},
    }


def apply_stage(cfg, stage, name, key, settings, extra):
    response = run_action(
        "deploy", key, enable_gateway=True, settings=settings,
        deployment_name=name, extra_parameters=extra,
    )
    outputs = deployment_outputs(response)
    host = response["properties"]["outputs"]["databaseHost"]["value"]
    if host != cfg.database_host:
        raise ValueError("Provisioned database hostname does not match configuration.")
    verify_firewall(cfg, extra["postgresAllowedIps"]["value"])
    state = read_state()
    names = list(state.get("deploymentNames", []))
    if name not in names:
        names.append(name)
    write_state({
        "subscriptionId": cfg.subscription_id,
        "resourceGroupName": cfg.resource_group,
        "databaseEnabled": True,
        "adminUiEnabled": bool(state.get("adminUiEnabled") or stage == "adminUiProvisioned"),
        "status": stage,
        "deploymentNames": names,
        "outputs": {**state.get("outputs", {}), **outputs},
    })
    return outputs


def ensure_foundry_model(cfg, url, key):
    endpoint = url.rstrip("/") + "/model/info"
    expected = {
        "model": f"azure/{cfg.model_name}",
        "api_base": cfg.foundry_api_base,
        "api_version": cfg.foundry_api_version,
        "timeout": 90,
    }

    def matching_model(data):
        if not isinstance(data, dict) or not isinstance(data.get("data"), list):
            raise ValueError("Cannot verify the persistent model registry.")
        matches = [row for row in data["data"] if row.get("model_name") == cfg.model_name]
        for row in matches:
            params = row.get("litellm_params", {})
            if params.get("model") != expected["model"] or params.get("api_base", "").rstrip("/") != expected["api_base"].rstrip("/"):
                raise ValueError(f"Existing {cfg.model_name} registry entry points to a different backend.")
        return bool(matches)

    if not matching_model(request_json(endpoint, token=key)):
        request_json(url.rstrip("/") + "/model/new", token=key, body={
            "model_name": cfg.model_name, "litellm_params": expected,
        })
    deadline = time.monotonic() + 120
    while True:
        if matching_model(request_json(endpoint, token=key)):
            return {"model": cfg.model_name, "provider": expected["model"], "persistentRegistry": "verified"}
        if time.monotonic() >= deadline:
            raise ValueError("Foundry model did not appear in the persistent model registry.")
        time.sleep(3)


def runtime_instances(cfg, revision):
    replicas = azure_json([
        "rest", "--method", "get", "--url",
        cfg.arm_app_url + f"/revisions/{revision}/replicas?api-version={API_VERSION}",
    ])["value"]
    return {
        (replica["name"], container.get("containerId"), container.get("restartCount", 0))
        for replica in replicas for container in replica["properties"]["containers"]
        if container["name"] == "litellm" and container.get("runningState") == "Running"
    }


def restart_and_verify(cfg, url, settings):
    session = admin_login(url, settings, model=cfg.model_name, retry_seconds=300)
    app = azure_json(["rest", "--method", "get", "--url", cfg.arm_app_url + "?api-version=" + API_VERSION])
    revision = app["properties"]["latestReadyRevisionName"]
    before = runtime_instances(cfg, revision)
    if not before:
        raise RuntimeError("Cannot verify persistence without a running pre-restart instance.")
    result = subprocess.run([
        "az", "rest", "--method", "post", "--url",
        cfg.arm_app_url + f"/revisions/{revision}/restart?api-version={API_VERSION}",
        "--only-show-errors", "--output", "none",
    ], capture_output=True, text=True, timeout=120, check=False)
    if result.returncode:
        raise RuntimeError("Container revision restart failed; persistence has not been verified.")
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        after = runtime_instances(cfg, revision)
        if after and after != before:
            break
        time.sleep(3)
    else:
        raise RuntimeError("No runtime restart was observed; persistence is not verified.")
    wait_for_database(url, retry_seconds=300)
    verify_admin_access(url, session, model=cfg.model_name)
    return {
        "database": "connected", "login": "passed", "adminRequest": "passed",
        "persistedSessionAfterRestart": "passed", "runtimeRestartObserved": True,
    }


def main():
    parser = argparse.ArgumentParser(description="Deploy or verify PostgreSQL-backed LiteLLM Admin UI.")
    parser.add_argument("action", choices=["probe", "what-if", "deploy", "verify"])
    parser.add_argument("--confirm", action="store_true", help="Required for database provisioning.")
    args = parser.parse_args()
    if args.action == "deploy" and not args.confirm:
        parser.error("Database provisioning requires --confirm.")
    settings = local_settings()
    cfg = load_config(settings)
    verify_target(cfg)
    app = azure_json(["rest", "--method", "get", "--url", cfg.arm_app_url + "?api-version=" + API_VERSION])
    url = "https://" + app["properties"]["configuration"]["ingress"]["fqdn"]
    request_with_retry(url + "/health/liveliness", retry_seconds=120)
    if args.action == "probe":
        print(json.dumps(run_database_operation({"action": "probe"}, cfg=cfg), indent=2))
        return
    key = master_key(settings)
    if args.action == "verify":
        session = admin_login(url, settings, model=cfg.model_name, retry_seconds=300)
        print(json.dumps({"login": "passed", "adminRequest": "passed", "userId": session.user_id}))
        return
    user = azure_json(["ad", "signed-in-user", "show"])
    already_enabled = any(
        row["name"] == "AZURE_POSTGRESQL_AUTH" and row.get("value") == "true"
        for row in app["properties"]["template"]["containers"][0]["env"]
    )
    if args.action == "what-if":
        dummy = {name: secrets.token_urlsafe(32) for name in ("UI_PASSWORD", "LITELLM_SALT_KEY")}
        preview = run_action(
            "what-if", key, enable_gateway=True, settings=settings,
            extra_parameters=parameters(cfg, app, user, dummy, admin_ui=True),
            deployment_name=cfg.deployment_name("database"),
        )
        if preview.get("error") or any(row.get("changeType") == "Delete" for row in preview.get("changes", [])):
            raise RuntimeError("Database preview failed or proposed a deletion.")
        print(json.dumps({"preview": "passed", "secrets": "redacted"}))
        return
    if not already_enabled:
        run_database_operation({"action": "probe"}, cfg=cfg)
    previous_state = read_state()
    credentials = internal_secrets(
        settings, generate=not (already_enabled or previous_state.get("adminUiEnabled")),
    )
    settings.update(credentials)
    outputs = apply_stage(
        cfg, "databaseProvisioned", cfg.deployment_name("database"), key, settings,
        parameters(cfg, app, user, credentials, admin_ui=already_enabled),
    )
    if already_enabled and "databaseBootstrap" in previous_state and "databaseMigrations" in previous_state:
        bootstrap = previous_state["databaseBootstrap"]
        migrations = previous_state["databaseMigrations"]
    else:
        bootstrap = run_database_operation({
            "action": "bootstrap", "host": cfg.database_host,
            "adminName": user.get("userPrincipalName") or user["mail"],
            "appPrincipalId": outputs["containerAppPrincipalId"],
            "token": access_token("https://ossrdbms-aad.database.windows.net", settings),
        }, cfg=cfg)
        migrations = run_database_operation({"action": "migrate", "host": cfg.database_host}, cfg=cfg)
    outputs = apply_stage(
        cfg, "adminUiProvisioned", cfg.deployment_name("admin-ui"), key, settings,
        parameters(cfg, app, user, credentials, admin_ui=True),
    )
    registry = ensure_foundry_model(cfg, url, key)
    tests = {
        "adminUi": restart_and_verify(cfg, url, settings),
        "modelRegistry": registry,
        "modelHub": test_model_hub(url, model=cfg.model_name),
        "foundry": completion(
            cfg.foundry_chat_url, cfg.model_name,
            access_token("https://cognitiveservices.azure.com/", settings), "FOUNDRY_OK",
        ),
        "litellm": test_litellm(url, key, model=cfg.model_name, retry_seconds=300),
        "gateway": test_gateway(outputs["gatewayUrl"], settings, alias=cfg.gateway_alias, retry_seconds=300),
    }
    write_state({
        "status": "healthy",
        "tests": tests,
        "databaseBootstrap": bootstrap,
        "databaseMigrations": migrations,
    })
    print(json.dumps({"outputs": outputs, "tests": tests}, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError, KeyError, OSError, urllib.error.URLError, subprocess.SubprocessError) as error:
        print(f"Database deployment stopped: {error}", file=sys.stderr)
        sys.exit(1)
