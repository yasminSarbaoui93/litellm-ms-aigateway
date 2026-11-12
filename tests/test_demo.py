import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import common
import deploy
import smoke_test
from support import configured_environment, patch_env_dir

KEY = "sk-" + "x" * 40


def setUpModule():
    global environment
    environment = configured_environment()


def tearDownModule():
    environment.close()


class ConfigurationTests(unittest.TestCase):
    def test_key_is_generated_once_with_private_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            stack, env = patch_env_dir(directory)
            with stack:
                key = common.master_key({}, generate=True)
                settings = common.local_settings()
                self.assertEqual(common.master_key(settings, generate=True), key)
                self.assertEqual(stat.S_IMODE(env.stat().st_mode), 0o600)

    def test_empty_example_key_can_be_filled_without_losing_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            stack, env = patch_env_dir(directory)
            with stack:
                env.write_text("AI_GATEWAY_API_KEY=existing-gateway-key\nLITELLM_MASTER_KEY=\n")
                key = common.master_key(common.local_settings(), generate=True)
                self.assertEqual(common.local_settings()["LITELLM_MASTER_KEY"], key)
                self.assertEqual(common.local_settings()["AI_GATEWAY_API_KEY"], "existing-gateway-key")

    def test_blank_environment_does_not_rotate_stored_key(self):
        with tempfile.TemporaryDirectory() as directory:
            stack, env = patch_env_dir(directory)
            with stack:
                env.write_text(f"LITELLM_MASTER_KEY={KEY}\n")
                with patch.dict(os.environ, {"LITELLM_MASTER_KEY": ""}):
                    self.assertEqual(common.master_key(common.local_settings(), generate=True), KEY)

    def test_invalid_key_rejected(self):
        for value in ["short", "sk-too-short", "sk-" + "a" * 35 + "\n"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                common.master_key({"LITELLM_MASTER_KEY": value})

    def test_env_syntax_is_not_executed(self):
        with tempfile.TemporaryDirectory() as directory:
            stack, path = patch_env_dir(directory)
            with stack:
                path.write_text("LITELLM_MASTER_KEY=$(echo not-executed)\n")
                self.assertEqual(common.local_settings()["LITELLM_MASTER_KEY"], "$(echo not-executed)")
                with self.assertRaises(ValueError):
                    common.master_key(common.local_settings())
                path.write_text("LITELLM_MASTER_KEY=one\nLITELLM_MASTER_KEY=two\n")
                with self.assertRaises(ValueError):
                    common.local_settings()

    def test_minimal_four_settings_derive_clean_defaults(self):
        cfg = common.load_config({
            "AZURE_SUBSCRIPTION_ID": "11111111-1111-4111-8111-111111111111",
            "AZURE_RESOURCE_GROUP": "rg-demo",
            "AI_GATEWAY_NAME": "gw-demo",
            "FOUNDRY_ACCOUNT_NAME": "foundry-demo",
        })
        self.assertEqual(cfg.foundry_resource_group, "rg-demo")
        self.assertEqual(cfg.foundry_api_base, "https://foundry-demo.openai.azure.com/")
        self.assertEqual(cfg.container_app_name, "ca-litellm-demo")
        self.assertEqual(cfg.postgres_server_name, "psql-litellm-demo")
        self.assertEqual(cfg.model_name, "gpt-4o")
        self.assertEqual(cfg.gateway_alias, "gpt-4o-litellm")

    def test_invalid_or_placeholder_settings_are_rejected(self):
        base = {
            "AZURE_SUBSCRIPTION_ID": "11111111-1111-4111-8111-111111111111",
            "AZURE_RESOURCE_GROUP": "rg-demo",
            "AI_GATEWAY_NAME": "gw-demo",
            "FOUNDRY_ACCOUNT_NAME": "foundry-demo",
        }
        invalid_overrides = [
            {"AZURE_SUBSCRIPTION_ID": "not-a-uuid"},
            {"AZURE_SUBSCRIPTION_ID": "<your-subscription-id>"},
            {"AZURE_RESOURCE_GROUP": "/subscriptions/x/resourceGroups/y"},
            {"FOUNDRY_API_BASE": "https://example.services.ai.azure.com/api/projects/demo"},
            {"LITELLM_IMAGE": "ghcr.io/berriai/litellm:latest"},
            {"NAME_PREFIX": "Invalid_Uppercase"},
        ]
        for override in invalid_overrides:
            with self.subTest(override=override), self.assertRaises(ValueError):
                common.load_config({**base, **override})

    def test_cli_errors_redact_credentials(self):
        result = subprocess.CompletedProcess([], 1, "", "error includes " + KEY)
        with patch("common.subprocess.run", return_value=result):
            with self.assertRaises(RuntimeError) as error:
                common.azure_json(["deployment", "sub", "validate"], sensitive=(KEY,))
        self.assertNotIn(KEY, str(error.exception))


class CompletionTests(unittest.TestCase):
    def setUp(self):
        self.response = {
            "model": "gpt-4o-2024-11-20",
            "choices": [{"message": {"content": "LITELLM_OK"}}],
            "usage": {"total_tokens": 25},
        }

    def test_valid_completion(self):
        self.assertEqual(
            smoke_test.validate_completion(self.response, "LITELLM_OK")["totalTokens"], 25,
        )

    def test_public_model_hub_lists_foundry_model_without_credentials(self):
        with patch("smoke_test.request_json", return_value={"data": [{"model_group": "gpt-4o"}]}) as request:
            result = smoke_test.test_model_hub("https://proxy.example/")
            self.assertEqual(result["models"], ["gpt-4o"])
            request.assert_called_once_with("https://proxy.example/public/v1/model_hub")

    def test_empty_or_malformed_public_hub_is_not_a_success(self):
        for data in [[], {}, {"data": []}, {"data": [None]}, {"data": [{"model_group": "other-model"}]}]:
            with self.subTest(data=data), patch("smoke_test.request_json", return_value=data):
                with self.assertRaises(ValueError):
                    smoke_test.test_model_hub("https://proxy.example")

    def test_success_shaped_errors_are_rejected(self):
        invalid = [
            {"error": {"message": "failed"}},
            {"choices": []},
            {**self.response, "choices": [None]},
            {**self.response, "choices": [{"message": {"content": None}}]},
            {**self.response, "choices": [{"message": {"content": "wrong marker"}}]},
            {**self.response, "usage": None},
            {**self.response, "usage": {"total_tokens": True}},
            {**self.response, "usage": {"total_tokens": 0}},
            {**self.response, "model": ""},
        ]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                smoke_test.validate_completion(value, "LITELLM_OK")

    def test_stream_requires_marker_and_done(self):
        payload = 'data: {"choices":[{"delta":{"content":"LITELLM_OK"}}]}\n\ndata: [DONE]\n'
        smoke_test.validate_stream(payload, "LITELLM_OK")
        for value in [payload.replace("LITELLM_OK", "wrong"), payload.replace("data: [DONE]", ""), 'data: {"error":{}}\n']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                smoke_test.validate_stream(value, "LITELLM_OK")

    def test_authentication_cannot_be_a_false_success(self):
        with patch("smoke_test.request_json", return_value={"choices": []}):
            with self.assertRaises(ValueError):
                smoke_test.reject_anonymous("https://proxy.example/v1/chat/completions", "gpt-4o")
        for status in [401, 403]:
            with patch("smoke_test.request_json", side_effect=common.HttpFailure(status, "https://proxy.example")):
                smoke_test.reject_anonymous("https://proxy.example/v1/chat/completions", "gpt-4o")
        with patch("smoke_test.request_json", side_effect=common.HttpFailure(404, "https://proxy.example")):
            with self.assertRaises(common.HttpFailure):
                smoke_test.reject_anonymous("https://proxy.example/v1/chat/completions", "gpt-4o")

    def test_wrong_auth_is_not_retried_without_budget(self):
        with patch("smoke_test.request_json", side_effect=common.HttpFailure(401, "https://proxy.example")) as request:
            with self.assertRaises(common.HttpFailure):
                smoke_test.completion("https://proxy.example", "gpt-4o", KEY, "LITELLM_OK")
            self.assertEqual(request.call_count, 1)

    def test_cold_start_retries_are_bounded(self):
        with patch("smoke_test.request_json", side_effect=common.HttpFailure(503, "https://proxy.example")), \
             patch("smoke_test.time.monotonic", side_effect=[0, 0, 1, 2]), \
             patch("smoke_test.time.sleep") as sleep, patch("sys.stderr", new_callable=io.StringIO):
            with self.assertRaises(common.HttpFailure):
                smoke_test.completion("https://proxy.example", "gpt-4o", KEY, "LITELLM_OK", retry_seconds=2)
            self.assertEqual(sleep.call_count, 1)

    def test_gateway_route_and_alias(self):
        with patch("smoke_test.completion", return_value={}) as completion, \
             patch("smoke_test.reject_anonymous"), patch("smoke_test.access_token", return_value="token") as token:
            smoke_test.test_gateway("https://gateway.example/default/models/v1", {})
            self.assertEqual(completion.call_args.args[:3], (
                "https://gateway.example/default/models/v1/chat/completions", "gpt-4o-litellm", "token",
            ))
            token.assert_called_once_with("https://ai.gateway.azure.com", {})

    def test_gateway_runtime_key_uses_bearer_token(self):
        with patch("smoke_test.completion", return_value={}) as completion, \
             patch("smoke_test.reject_anonymous"), patch("smoke_test.access_token") as token:
            smoke_test.test_gateway("https://gateway.example", {"AI_GATEWAY_API_KEY": KEY})
            self.assertEqual(completion.call_args.args[2], KEY)
            token.assert_not_called()

    def test_health_probe_uses_startup_retry_budget(self):
        with patch("smoke_test.request_with_retry") as request, \
             patch("smoke_test.completion", return_value={}), patch("smoke_test.reject_anonymous"):
            smoke_test.test_litellm("https://proxy.example", KEY, retry_seconds=120)
            request.assert_called_once_with("https://proxy.example/health/liveliness", retry_seconds=120)

    def test_http_rejects_plaintext_and_credential_redirects(self):
        for url in ["http://proxy.example", "https://user:password@proxy.example"]:
            with self.assertRaises(ValueError):
                common.request_json(url, token=KEY)
        self.assertIsNone(common.NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.example"))

    def test_http_errors_do_not_expose_upstream_body(self):
        error = urllib.error.HTTPError("https://proxy.example", 401, KEY, {}, io.BytesIO(KEY.encode()))
        with patch("common.urllib.request.OpenerDirector.open", side_effect=error):
            with self.assertRaises(common.HttpFailure) as failure:
                common.request_json("https://proxy.example", token=KEY)
            self.assertNotIn(KEY, str(failure.exception))


class DeploymentTests(unittest.TestCase):
    def test_existing_state_cannot_be_reused_for_another_target(self):
        cfg = common.load_config()
        with patch("deploy.read_state", return_value={
            "subscriptionId": cfg.subscription_id, "resourceGroupName": "other-group",
        }), patch("deploy.azure_json") as cli:
            with self.assertRaisesRegex(ValueError, "another subscription or resource group"):
                deploy.verify_target(cfg)
            cli.assert_not_called()

    def test_runtime_endpoints_follow_configuration(self):
        cfg = common.load_config({
            **common.local_settings(),
            "CONTAINER_APP_NAME": "ca-alternate",
            "POSTGRES_SERVER_NAME": "psql-alternate",
            "FOUNDRY_API_BASE": "https://alternate.openai.azure.com/",
            "FOUNDRY_API_VERSION": "2025-01-01-preview",
            "FOUNDRY_MODEL_NAME": "gpt-4o-mini",
        })
        self.assertTrue(cfg.container_app_id.endswith("/containerApps/ca-alternate"))
        self.assertEqual(cfg.database_host, "psql-alternate.postgres.database.azure.com")
        self.assertEqual(cfg.gateway_alias, "gpt-4o-mini-litellm")
        self.assertEqual(
            cfg.foundry_chat_url,
            "https://alternate.openai.azure.com/openai/deployments/gpt-4o-mini/chat/completions?api-version=2025-01-01-preview",
        )

    @unittest.skipUnless(
        os.environ.get("BICEP_CLI_PATH") or shutil.which("bicep"),
        "Set BICEP_CLI_PATH to run compiled-infrastructure assertions.",
    )
    def test_compiled_architecture_matches_plan_with_zero_warnings(self):
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as directory, patch("sys.stderr", stderr):
            path = deploy.compile_template(directory, common.local_settings())
            template = json.loads(path.read_text())
        self.assertEqual(stderr.getvalue(), "")
        modules = {r["name"]: r["properties"]["template"] for r in template["resources"]}
        hosting = modules["litellm-resources"]["resources"]
        resources = [
            resource for module in modules.values()
            for resource in (
                module["resources"].values() if isinstance(module["resources"], dict) else module["resources"]
            )
        ]
        self.assertEqual({r["type"] for r in hosting}, {
            "Microsoft.OperationalInsights/workspaces",
            "Microsoft.App/managedEnvironments",
            "Microsoft.App/containerApps",
        })
        forbidden = {
            "Microsoft.Resources/resourceGroups", "Microsoft.KeyVault/vaults",
            "Microsoft.ManagedIdentity/userAssignedIdentities", "Microsoft.ContainerRegistry/registries",
            "Microsoft.CognitiveServices/accounts",
        }
        self.assertFalse(any(r["type"] in forbidden for r in resources))
        app = next(r for r in hosting if r["type"] == "Microsoft.App/containerApps")
        self.assertEqual(app["identity"]["type"], "SystemAssigned")
        configuration = app["properties"]["configuration"]
        self.assertEqual(configuration["ingress"]["targetPort"], 4000)
        self.assertFalse(configuration["ingress"]["allowInsecure"])
        self.assertIn("'litellm-master-key'", configuration["secrets"])
        self.assertIn("parameters('litellmMasterKey')", configuration["secrets"])
        self.assertIn("parameters('enableAdminUi')", configuration["secrets"])
        self.assertIn("parameters('uiPassword')", configuration["secrets"])
        self.assertIn("parameters('litellmSaltKey')", configuration["secrets"])
        runtime = app["properties"]["template"]
        container = runtime["containers"][0]
        self.assertNotIn("command", container)
        self.assertEqual(container["args"], [
            "--config", "/etc/litellm/config.yaml", "--host", "0.0.0.0", "--port", "4000",
        ])
        self.assertEqual(runtime["scale"], {"minReplicas": 0, "maxReplicas": 1})
        self.assertEqual(container["resources"], {"cpu": 1, "memory": "2Gi"})
        self.assertIn("LITELLM_CONFIG_REVISION", json.dumps(container["env"]))
        self.assertIn("uniqueString(", json.dumps(container["env"]))
        self.assertIn("STORE_MODEL_IN_DB", json.dumps(container["env"]))
        config = (common.ROOT / "litellm/config.yaml").read_text()
        self.assertIn("public_model_groups:\n    - gpt-4o", config)
        for probe in container["probes"]:
            if probe["type"] == "Startup":
                self.assertEqual(
                    probe["httpGet"]["path"],
                    "[if(parameters('enableAdminUi'), '/health/readiness', '/health/liveliness')]",
                )
            else:
                self.assertEqual(probe["httpGet"]["path"], "/health/liveliness")
            self.assertEqual(probe["httpGet"]["port"], 4000)
        self.assertEqual(template["parameters"]["litellmMasterKey"]["type"].lower(), "securestring")
        self.assertFalse(any("key" in name.lower() or "secret" in name.lower() for name in template["outputs"]))
        gateway_res = modules["litellm-gateway"]["resources"]
        gateway = list(gateway_res.values()) if isinstance(gateway_res, dict) else gateway_res
        self.assertTrue(any(r["type"].endswith("/aliases") for r in gateway))
        provider = next(r for r in gateway if r["type"].endswith("/modelProviders"))
        self.assertEqual(provider["properties"]["kind"], "Custom")
        custom = provider["properties"]["custom"]
        self.assertEqual(custom["endpoint"], "[parameters('litellmUrl')]")
        self.assertEqual(custom["authentication"]["apiKey"]["headerName"], "Authorization")
        self.assertIn("litellmMasterKey", custom["authentication"]["apiKey"]["value"])
        model = next(r for r in gateway if r["type"].endswith("/models"))
        self.assertEqual(model["properties"]["supportedEndpoints"], ["/v1/chat/completions"])
        alias = next(r for r in gateway if r["type"].endswith("/aliases"))
        self.assertEqual(alias["properties"]["selectionOrder"], ["priority"])
        role = modules["litellm-foundry-access"]["resources"][0]
        self.assertEqual(role["properties"]["principalType"], "ServicePrincipal")
        self.assertIn("foundryAccountName", role["scope"])
        self.assertIn(
            "5e0bd9bd-7b93-4f28-af87-19fc36ad61bd", json.dumps(modules["litellm-foundry-access"]),
        )
        database = modules["litellm-postgres"]
        postgres_module = next(r for r in template["resources"] if r["name"] == "litellm-postgres")
        hosting_module = next(r for r in template["resources"] if r["name"] == "litellm-resources")
        self.assertEqual(postgres_module["properties"]["parameters"]["location"]["value"], "[parameters('postgresLocation')]")
        self.assertEqual(hosting_module["properties"]["parameters"]["location"]["value"], "[parameters('location')]")
        self.assertEqual(template["parameters"]["postgresLocation"]["defaultValue"], "northeurope")
        self.assertEqual(common.load_config().location, "swedencentral")
        pg_resources = list(database["resources"].values())
        server = next(r for r in pg_resources if r["type"] == "Microsoft.DBforPostgreSQL/flexibleServers")
        self.assertEqual(server["sku"], {"name": "Standard_B1ms", "tier": "Burstable"})
        self.assertEqual(server["properties"]["storage"]["storageSizeGB"], 32)
        self.assertEqual(server["properties"]["authConfig"]["passwordAuth"], "Disabled")
        self.assertEqual(server["properties"]["authConfig"]["activeDirectoryAuth"], "Enabled")
        self.assertNotIn("administratorLogin", server["properties"])
        self.assertNotIn("administratorLoginPassword", server["properties"])
        firewall = next(r for r in pg_resources if r["type"].endswith("/firewallRules"))
        self.assertEqual(firewall["copy"]["batchSize"], 1)
        self.assertEqual(firewall["copy"]["mode"], "serial")
        self.assertIn("unchangedFirewallIps", firewall["condition"])
        self.assertEqual(firewall["properties"]["startIpAddress"], firewall["properties"]["endIpAddress"])
        self.assertIn("allowedIps", firewall["properties"]["startIpAddress"])
        self.assertNotIn("0.0.0.0", json.dumps(database))
        self.assertTrue(any(r["type"].endswith("/databases") for r in pg_resources))
        self.assertTrue(any(r["type"].endswith("/administrators") for r in pg_resources))
        for name in ("uiPassword", "litellmSaltKey"):
            self.assertEqual(template["parameters"][name]["type"].lower(), "securestring")

    def test_native_bicep_uses_positional_source_argument(self):
        def fake_compile(arguments, **kwargs):
            self.assertEqual(arguments[:3], [
                "/native/bicep", "build", str(common.ROOT / "infra/main.bicep"),
            ])
            self.assertNotIn("--file", arguments)
            Path(arguments[arguments.index("--outfile") + 1]).write_text("{}")
            return subprocess.CompletedProcess(arguments, 0, "", "")

        with tempfile.TemporaryDirectory() as directory, \
             patch("deploy.subprocess.run", side_effect=fake_compile):
            deploy.compile_template(directory, {"BICEP_CLI_PATH": "/native/bicep"})

    def test_compiler_warnings_block_deployment(self):
        result = subprocess.CompletedProcess([], 0, "", "Warning BCP035: missing required property")
        with tempfile.TemporaryDirectory() as directory, \
             patch("deploy.subprocess.run", return_value=result), \
             patch("sys.stderr", new_callable=io.StringIO):
            with self.assertRaisesRegex(RuntimeError, "compiler warnings"):
                deploy.compile_template(directory, {"BICEP_CLI_PATH": "/native/bicep"})

    def test_bicep_parameters_exclude_secrets(self):
        params = common.load_config().bicep_parameters()
        self.assertIn("@sha256:", params["containerImage"]["value"])
        self.assertNotIn("litellmMasterKey", params)

    def test_missing_outputs_never_become_success(self):
        with self.assertRaises(ValueError):
            deploy.deployment_outputs({"properties": {"outputs": {}}})

    def test_gateway_outputs_can_be_empty_only_in_hosting_stage(self):
        data = {"properties": {"outputs": {
            "litellmUrl": {"value": "https://proxy.example"},
            "containerAppName": {"value": "ca-litellm-example"},
            "containerAppPrincipalId": {"value": "test-principal"},
            "gatewayUrl": {"value": ""},
            "gatewayAlias": {"value": ""},
        }}}
        deploy.deployment_outputs(data, require_gateway=False)
        with self.assertRaises(ValueError):
            deploy.deployment_outputs(data)

    def test_secure_parameters_use_ephemeral_private_file_and_clean_up(self):
        observed = {}

        def fake_azure(arguments, **kwargs):
            self.assertNotIn(KEY, " ".join(arguments))
            path = Path(arguments[arguments.index("--parameters") + 1][1:])
            observed["path"] = path
            data = json.loads(path.read_text())
            self.assertEqual(data["parameters"]["litellmMasterKey"]["value"], KEY)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            return {"status": "Succeeded"}

        with patch("deploy.compile_template", return_value=Path("compiled.json")), \
             patch("deploy.azure_json", side_effect=fake_azure):
            deploy.run_action("validate", KEY, settings=common.local_settings())
            self.assertFalse(observed["path"].exists())

    def test_failed_arm_state_is_not_reported_as_deployed(self):
        with patch("deploy.compile_template", return_value=Path("compiled.json")), \
             patch("deploy.azure_json", return_value={"properties": {"provisioningState": "Failed"}}):
            with self.assertRaises(RuntimeError):
                deploy.run_action("deploy", KEY, settings=common.local_settings())

    def test_deploy_requires_explicit_confirmation(self):
        with patch.object(sys, "argv", ["deploy.py", "deploy"]), \
             patch("sys.stderr", new_callable=io.StringIO), patch("deploy.verify_target") as verify:
            with self.assertRaises(SystemExit) as result:
                deploy.main()
            self.assertEqual(result.exception.code, 2)
            verify.assert_not_called()

    def test_missing_local_key_does_not_rotate_live_credentials(self):
        with patch.object(sys, "argv", ["deploy.py", "deploy", "--confirm"]), \
             patch("deploy.verify_target"), patch("deploy.azure_json", return_value=["existing-app"]), \
             patch("deploy.master_key") as key:
            with self.assertRaisesRegex(ValueError, "refusing silent key rotation"):
                deploy.main()
            key.assert_not_called()


if __name__ == "__main__":
    unittest.main()
