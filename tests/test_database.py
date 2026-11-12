import asyncio
import base64
import hashlib
import hmac
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import common
import container_exec
import database_deploy
import database_remote
import deploy
import smoke_test
from support import configured_environment, patch_env_dir

KEY = "sk-" + "m" * 40
UI_KEY = "sk-" + "u" * 40


def setUpModule():
    global environment
    environment = configured_environment()


def tearDownModule():
    environment.close()


def ui_token(claims, key=KEY):
    encode = lambda value: base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
    header = encode({"alg": "HS256"})
    payload = encode(claims)
    signature = hmac.new(key.encode(), (header + "." + payload).encode(), hashlib.sha256).digest()
    return header + "." + payload + "." + base64.urlsafe_b64encode(signature).decode().rstrip("=")


class AdminUiTests(unittest.TestCase):
    def setUp(self):
        self.claims = {"user_role": "proxy_admin", "user_id": "admin", "key": UI_KEY, "exp": time.time() + 300}

    def test_real_login_requires_db_and_uses_its_backed_key_for_admin_request(self):
        with patch("smoke_test.request_with_retry", return_value={"db": "connected"}), \
             patch("smoke_test.request_json", side_effect=[
                 {"token": ui_token(self.claims)}, {"data": [{"model_name": "gpt-4o"}]},
             ]) as request:
            result = smoke_test.test_admin_ui(
                "https://proxy.example", {"LITELLM_MASTER_KEY": KEY, "UI_PASSWORD": "private-password"},
            )
        self.assertEqual(result["login"], "passed")
        self.assertEqual(request.call_args.kwargs["token"], UI_KEY)
        self.assertNotIn(UI_KEY, json.dumps(result))

    def test_no_database_never_attempts_password_login(self):
        with patch("smoke_test.request_with_retry", return_value={"db": "Not connected"}), \
             patch("smoke_test.request_json") as login:
            with self.assertRaises(ValueError):
                smoke_test.admin_login("https://proxy.example", {"UI_PASSWORD": "private"})
            login.assert_not_called()

    def test_rollout_waits_for_connected_db_not_just_http_success(self):
        with patch("smoke_test.request_with_retry", side_effect=[
            {"status": "healthy", "db": "Not connected"}, {"status": "healthy", "db": "connected"},
        ]) as ready, patch("smoke_test.time.sleep"), patch("sys.stderr", new_callable=io.StringIO):
            smoke_test.wait_for_database("https://proxy.example", retry_seconds=30)
        self.assertEqual(ready.call_count, 2)

    def test_session_signature_role_expiry_and_backed_key_are_verified(self):
        bad = [
            {**self.claims, "exp": 0}, {**self.claims, "exp": "later"},
            {**self.claims, "user_role": "viewer"}, {**self.claims, "key": KEY},
            {**self.claims, "key": ""}, {**self.claims, "user_id": None},
        ]
        for claims in bad:
            with self.subTest(claims=claims), self.assertRaises(ValueError):
                smoke_test.decode_admin_session(ui_token(claims), KEY)
        with self.assertRaises(ValueError):
            smoke_test.decode_admin_session(ui_token(self.claims, "wrong-key"), KEY)
        session = smoke_test.decode_admin_session(ui_token(self.claims), KEY)
        self.assertNotIn(UI_KEY, repr(session))


class DatabaseSafetyTests(unittest.TestCase):
    def setUp(self):
        self.cfg = common.load_config()

    def test_existing_foundry_model_registry_entry_is_not_duplicated(self):
        data = {"data": [{"model_name": "gpt-4o", "litellm_params": {
            "model": "azure/gpt-4o", "api_base": "https://foundry-example.openai.azure.com/",
        }}]}
        with patch("database_deploy.request_json", return_value=data) as request:
            result = database_deploy.ensure_foundry_model(self.cfg, "https://proxy.example", KEY)
        self.assertEqual(result["persistentRegistry"], "verified")
        self.assertTrue(all(call.kwargs.get("body") is None for call in request.call_args_list))

    def test_empty_registry_imports_the_actual_foundry_connection(self):
        entry = {"data": [{"model_name": "gpt-4o", "litellm_params": {
            "model": "azure/gpt-4o", "api_base": "https://foundry-example.openai.azure.com/",
        }}]}
        with patch("database_deploy.request_json", side_effect=[{"data": []}, {}, entry]) as request:
            database_deploy.ensure_foundry_model(self.cfg, "https://proxy.example", KEY)
        payload = request.call_args_list[1].kwargs["body"]
        self.assertEqual(payload["litellm_params"]["model"], "azure/gpt-4o")
        self.assertNotIn("api_key", payload["litellm_params"])

    def test_runtime_identity_detects_restarts_and_ignores_stopped_containers(self):
        data = {"value": [{"name": "replica", "properties": {"containers": [
            {"name": "litellm", "containerId": "before", "restartCount": 0, "runningState": "Running"},
            {"name": "sidecar", "containerId": "other", "runningState": "Running"},
        ]}}]}
        with patch("database_deploy.azure_json", return_value=data):
            self.assertEqual(database_deploy.runtime_instances(self.cfg, "revision"), {("replica", "before", 0)})
        data["value"][0]["properties"]["containers"][0].update(containerId="after", restartCount=1)
        with patch("database_deploy.azure_json", return_value=data):
            self.assertEqual(database_deploy.runtime_instances(self.cfg, "revision"), {("replica", "after", 1)})

    def test_only_exact_existing_individual_ip_rules_are_skipped(self):
        rules = [
            {"properties": {"startIpAddress": "8.8.8.8", "endIpAddress": "8.8.8.8"}},
            {"properties": {"startIpAddress": "8.8.4.4", "endIpAddress": "8.8.4.10"}},
            {"properties": {"startIpAddress": "1.1.1.1", "endIpAddress": "1.1.1.1"}},
        ]
        allowed = ["8.8.8.8", "8.8.4.4"]
        self.assertEqual(database_deploy.unchanged_firewall_ips(allowed, rules), ["8.8.8.8"])
        with patch("database_deploy.firewall_rules", return_value=rules):
            with self.assertRaises(ValueError):
                database_deploy.verify_firewall(self.cfg, allowed)
        with patch("database_deploy.firewall_rules", return_value=rules[:1]):
            database_deploy.verify_firewall(self.cfg, ["8.8.8.8"])
            with self.assertRaises(ValueError):
                database_deploy.verify_firewall(self.cfg, allowed)

    def test_database_stages_update_local_state(self):
        with tempfile.TemporaryDirectory() as directory:
            stack, _ = patch_env_dir(directory)
            with stack, \
                 patch("database_deploy.run_action", return_value={"properties": {"outputs": {
                     "databaseHost": {"value": self.cfg.database_host},
                 }}}), patch("database_deploy.deployment_outputs", return_value={"containerAppName": "example"}), \
                 patch("database_deploy.verify_firewall"):
                common.write_state({"deploymentNames": ["hosting"]})
                database_deploy.apply_stage(
                    self.cfg, "databaseProvisioned", "database", KEY, common.local_settings(),
                    {"postgresAllowedIps": {"value": ["8.8.8.8"]}},
                )
                state = common.read_state()
                self.assertEqual(state["deploymentNames"], ["hosting", "database"])
                self.assertTrue(state["databaseEnabled"])

    def test_database_deployment_requires_confirmation(self):
        with patch.object(sys, "argv", ["database_deploy.py", "deploy"]), \
             patch("sys.stderr", new_callable=io.StringIO), \
             patch("database_deploy.verify_target") as verify:
            with self.assertRaises(SystemExit) as result:
                database_deploy.main()
            self.assertEqual(result.exception.code, 2)
            verify.assert_not_called()

    def test_firewall_only_accepts_explicit_global_ipv4_addresses(self):
        self.assertEqual(database_deploy.validate_ips(["8.8.8.8", "8.8.8.8"]), ["8.8.8.8"])
        for ips in [[], ["0.0.0.0"], ["127.0.0.1"], ["10.0.0.1"], ["::1"], ["20.1.1.1/24"]]:
            with self.subTest(ips=ips), self.assertRaises(ValueError):
                database_deploy.validate_ips(ips)

    def test_remote_database_host_must_match_configured_runtime_without_exposing_token(self):
        with patch.dict(os.environ, {"DATABASE_HOST": "expected.postgres.database.azure.com"}):
            for host in ["different.postgres.database.azure.com", "https://expected.postgres.database.azure.com"]:
                with self.subTest(host=host), self.assertRaises(ValueError) as failure:
                    database_remote.database_url(host, "litellm", "litellm_app", "private-token")
                self.assertNotIn("private-token", str(failure.exception))

    def test_internal_secrets_persist_once_without_changing_model_key(self):
        with tempfile.TemporaryDirectory() as directory:
            stack, env = patch_env_dir(directory)
            with stack:
                env.write_text("LITELLM_MASTER_KEY=" + KEY + "\n")
                first = database_deploy.internal_secrets({}, generate=True)
                settings = common.local_settings()
                second = database_deploy.internal_secrets(settings, generate=False)
                self.assertEqual(first, second)
                self.assertEqual(common.master_key(settings), KEY)
                self.assertEqual(env.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(ValueError):
            database_deploy.internal_secrets({}, generate=False)

    def test_deploy_cannot_disable_a_previously_enabled_database_silently(self):
        with patch("deploy.read_state", return_value={"databaseEnabled": True}):
            with self.assertRaisesRegex(ValueError, "preserve Admin UI"):
                deploy.run_action("deploy", KEY, settings=common.local_settings())

    def test_database_parameters_cannot_change_target_or_enable_ui_without_db(self):
        for extra in [
            {"resourceGroupName": {"value": "different-group"}},
            {"enableAdminUi": {"value": True}},
        ]:
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                deploy.run_action("validate", KEY, settings=common.local_settings(), extra_parameters=extra)

    def test_bootstrap_refuses_existing_role_mapped_to_another_identity(self):
        admin = MagicMock()
        admin.connect = AsyncMock()
        admin.disconnect = AsyncMock()
        admin.execute_raw = AsyncMock()
        admin.query_raw = AsyncMock(side_effect=[
            [{"rolname": "litellm_app"}],
            [{"principal": {"rolename": "litellm_app", "objectId": "different-identity"}}],
        ])
        fake = types.SimpleNamespace(Prisma=MagicMock(return_value=admin))
        with patch.dict(sys.modules, {"prisma": fake}), \
             patch("database_remote.database_url", return_value="private-url"):
            with self.assertRaisesRegex(ValueError, "expected app identity"):
                asyncio.run(database_remote.bootstrap({
                    "appPrincipalId": "33333333-3333-4333-8333-333333333333",
                    "host": self.cfg.database_host, "adminName": "admin", "token": "private-token",
                }))
        admin.execute_raw.assert_not_called()
        admin.disconnect.assert_awaited_once()

    def test_bootstrap_creates_only_the_expected_identity_and_database_grants(self):
        admin = MagicMock(connect=AsyncMock(), disconnect=AsyncMock(), execute_raw=AsyncMock())
        admin.query_raw = AsyncMock(side_effect=[[], [{"result": "created"}]])
        app_db = MagicMock(connect=AsyncMock(), disconnect=AsyncMock(), execute_raw=AsyncMock())
        fake = types.SimpleNamespace(Prisma=MagicMock(side_effect=[admin, app_db]))
        object_id = "33333333-3333-4333-8333-333333333333"
        with patch.dict(sys.modules, {"prisma": fake}), \
             patch("database_remote.database_url", return_value="private-url"):
            result = asyncio.run(database_remote.bootstrap({
                "appPrincipalId": object_id, "host": self.cfg.database_host,
                "adminName": "admin", "token": "private-token",
            }))
        self.assertEqual(result, {"role": "litellm_app", "serverAdmin": False, "bootstrap": "passed"})
        self.assertEqual(admin.query_raw.call_args.args[1:], ("litellm_app", object_id))
        self.assertIn("'service', false, false", admin.query_raw.call_args.args[0])
        self.assertIn('ON DATABASE "litellm"', admin.execute_raw.call_args.args[0])
        self.assertEqual(
            app_db.execute_raw.call_args.args[0], 'GRANT USAGE, CREATE ON SCHEMA public TO "litellm_app"',
        )

    def test_ui_secrets_are_redacted_and_never_used_as_scalar_cli_arguments(self):
        extra = {
            "enableDatabase": {"value": True}, "enableAdminUi": {"value": True},
            "uiPassword": {"value": "private-ui-password"}, "litellmSaltKey": {"value": "private-salt"},
        }
        with patch("deploy.compile_template", return_value=Path("compiled.json")), \
             patch("deploy.azure_json", return_value={"status": "Succeeded"}) as cli:
            deploy.run_action("validate", KEY, extra_parameters=extra, settings=common.local_settings())
            self.assertNotIn("private-ui-password", " ".join(cli.call_args.args[0]))
            self.assertNotIn("private-salt", " ".join(cli.call_args.args[0]))
            self.assertIn("private-ui-password", cli.call_args.kwargs["sensitive"])
            self.assertIn("private-salt", cli.call_args.kwargs["sensitive"])

    def test_failed_migration_is_not_reported_as_success(self):
        with patch("database_remote.migration_assets", return_value=(Path("/app/schema.prisma"), "/pinned/prisma")), \
             patch("database_remote.app_database_url", return_value="private-url"), \
             patch("database_remote.subprocess.run", return_value=types.SimpleNamespace(returncode=1)):
            with self.assertRaisesRegex(RuntimeError, "exited 1"):
                database_remote.main({"action": "migrate", "host": self.cfg.database_host})

    def test_migration_uses_native_pinned_cli_and_never_an_extra_gateway_process(self):
        with patch("database_remote.migration_assets", return_value=(Path("/app/schema.prisma"), "/pinned/prisma")), \
             patch("database_remote.app_database_url", return_value="private-mi-url"), \
             patch("database_remote.inspect_schema", new_callable=AsyncMock, return_value={"database": "connected"}), \
             patch("database_remote.subprocess.run", return_value=types.SimpleNamespace(returncode=0)) as run:
            result = database_remote.main({"action": "migrate", "host": self.cfg.database_host})
        self.assertEqual(run.call_args.args[0], ["/pinned/prisma", "migrate", "deploy", "--schema", "/app/schema.prisma"])
        self.assertEqual(run.call_args.kwargs["env"]["DATABASE_URL"], "private-mi-url")
        self.assertNotIn("private-mi-url", " ".join(run.call_args.args[0]))
        self.assertEqual(result["database"], "connected")


class ExecTransportTests(unittest.TestCase):
    def test_console_url_keeps_code_and_credentials_out_of_command_arguments(self):
        url = container_exec.console_url(
            "https://swedencentral.azurecontainerapps.dev/subscriptions/example", "revision", "replica",
        )
        self.assertEqual(parse_qs(urlsplit(url).query)["command"], ["/bin/sh"])
        for endpoint in ["http://untrusted.example", "https://untrusted.example"]:
            with self.assertRaises(ValueError):
                container_exec.console_url(endpoint, "revision", "replica")

    def test_protected_payload_is_only_sent_after_exact_echo_disabled_marker(self):
        class Channel:
            def __init__(self):
                self.ready = False
                self.closed = False
                self.sent = []
                self.frames = [
                    b"\x00\x01/app # ",
                    b'\x00\x01print("LITELLM_DB_RESULT:"+echoed_code)\r\nLITELLM_DB_READY\r\n',
                    b'\x00\x01LITELLM_DB_RESULT:{"ok":true,"result":{"passed":true}}\r\n',
                ]

            def recv(self):
                value = self.frames.pop(0)
                if b"LITELLM_DB_READY\r\n" in value:
                    self.ready = True
                return value

            def send_binary(self, value):
                if b"private-sql-token" in value:
                    if not self.ready:
                        raise AssertionError("Credential sent before echo was disabled")
                self.sent.append(value)

            def close(self):
                self.closed = True

        channel = Channel()
        fake_websocket = types.SimpleNamespace(
            create_connection=MagicMock(return_value=channel), WebSocketTimeoutException=TimeoutError,
        )
        responses = [
            {"properties": {
                "latestReadyRevisionName": "revision",
                "configuration": {"ingress": {"fqdn": "proxy.example"}},
            }},
            {"value": [{"name": "replica", "properties": {"containers": [{
                "name": "litellm", "logStreamEndpoint": "https://swedencentral.azurecontainerapps.dev/subscriptions/example",
            }]}}]},
            {"properties": {"token": "private-console-token"}},
        ]
        with patch.dict(sys.modules, {"websocket": fake_websocket}), \
             patch("container_exec.request_with_retry") as warm, \
             patch("container_exec.azure_json", side_effect=responses):
            result = container_exec.run_database_operation({"action": "bootstrap", "token": "private-sql-token"})
        warm.assert_called_once_with("https://proxy.example/health/liveliness", retry_seconds=120)
        self.assertTrue(result["passed"])
        self.assertTrue(channel.closed)
        chunks = [message for message in channel.sent if message.startswith(b"\x00\x00")][1:]
        self.assertTrue(chunks)
        self.assertLessEqual(max(map(len, chunks)), 514)
        self.assertIn(b"private-sql-token", b"".join(message[2:] for message in chunks))


if __name__ == "__main__":
    unittest.main()
