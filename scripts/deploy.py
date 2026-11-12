import argparse
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import uuid

from common import ROOT, azure_json, load_config, local_settings, master_key, read_state, write_state
from smoke_test import test_gateway, test_litellm


def compile_template(directory, settings):
    output = Path(directory) / "main.json"
    compiler = settings.get("BICEP_CLI_PATH") or shutil.which("bicep")
    source = str(ROOT / "infra/main.bicep")
    command = [compiler, "build", source] if compiler else ["az", "bicep", "build", "--file", source]
    result = subprocess.run(
        [*command, "--outfile", str(output)],
        capture_output=True, text=True, timeout=120, check=False,
    )
    if result.stderr:
        print(result.stderr, file=sys.stderr)
    if result.returncode:
        raise RuntimeError("Bicep compilation failed. Set BICEP_CLI_PATH to a native Bicep binary if needed.")
    if "Warning " in result.stderr:
        raise RuntimeError("Bicep reported compiler warnings; resolve them before deployment.")
    if not output.is_file():
        raise RuntimeError("Bicep compiler did not produce the ARM template.")
    return output


def verify_target(cfg):
    state = read_state()
    if state and (
        state.get("subscriptionId") != cfg.subscription_id
        or state.get("resourceGroupName") != cfg.resource_group
    ):
        raise ValueError(
            "env/deployment-state.json belongs to another subscription or resource group; "
            "back up or remove it before switching targets."
        )
    account = azure_json(["account", "show"])
    if account["id"] != cfg.subscription_id:
        raise ValueError(
            f"Active Azure CLI subscription is {account['id']}; "
            f"run: az account set --subscription {cfg.subscription_id}"
        )
    azure_json(["group", "show", "--subscription", cfg.subscription_id, "--name", cfg.resource_group])


def run_action(
    action, key, *, enable_gateway=False, caller_object_id="", settings=None,
    deployment_name=None, extra_parameters=None,
):
    settings = local_settings() if settings is None else settings
    cfg = load_config(settings)
    parameters = cfg.bicep_parameters()
    parameters["litellmMasterKey"] = {"value": key}
    parameters["enableGateway"] = {"value": enable_gateway}
    parameters["gatewayCallerObjectId"] = {"value": caller_object_id}
    if extra_parameters is None and read_state().get("databaseEnabled"):
        raise ValueError("Database is enabled. Use scripts/database_deploy.py to preserve Admin UI settings.")
    if extra_parameters:
        allowed = {
            "enableDatabase", "enableAdminUi", "postgresServerName", "postgresAllowedIps",
            "postgresUnchangedFirewallIps", "entraAdminObjectId", "entraAdminName",
            "uiPassword", "litellmSaltKey",
        }
        if extra_parameters.keys() - allowed:
            raise ValueError("Unexpected database parameter override.")
        if extra_parameters.get("enableAdminUi", {}).get("value") and not extra_parameters.get("enableDatabase", {}).get("value"):
            raise ValueError("Admin UI requires a provisioned database.")
        parameters.update(extra_parameters)
    sensitive = (key, *(parameters.get(name, {}).get("value", "") for name in ("uiPassword", "litellmSaltKey")))
    name = deployment_name or cfg.deployment_name("gateway" if enable_gateway else "hosting")
    with tempfile.TemporaryDirectory(prefix="litellm-deploy-") as directory:
        template = compile_template(directory, settings)
        params_file = Path(directory) / "parameters.json"
        fd = os.open(params_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as file:
            json.dump({
                "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#",
                "contentVersion": "1.0.0.0",
                "parameters": parameters,
            }, file)
        command = "create" if action == "deploy" else action
        arguments = [
            "deployment", "sub", command,
            "--subscription", cfg.subscription_id,
            "--name", name,
            "--location", cfg.location,
            "--template-file", str(template),
            "--parameters", "@" + str(params_file),
        ]
        if action == "what-if":
            arguments.append("--no-pretty-print")
        result = azure_json(arguments, sensitive=sensitive, timeout=1800)
    if action == "deploy" and result.get("properties", {}).get("provisioningState") != "Succeeded":
        raise RuntimeError("ARM deployment did not reach Succeeded.")
    return result


def deployment_outputs(result, *, require_gateway=True):
    values = result["properties"]["outputs"]
    required = ("litellmUrl", "gatewayUrl", "containerAppName", "containerAppPrincipalId", "gatewayAlias")
    outputs = {}
    for name in required:
        entry = values.get(name)
        optional = not require_gateway and name in {"gatewayUrl", "gatewayAlias"}
        if not isinstance(entry, dict) or not isinstance(entry.get("value"), str):
            raise ValueError(f"Deployment output {name} is missing or invalid.")
        if not entry["value"] and not optional:
            raise ValueError(f"Deployment output {name} is missing or invalid.")
        outputs[name] = entry["value"]
    return outputs


def save_state(cfg, status, outputs, names, tests=None):
    write_state({
        "status": status,
        "subscriptionId": cfg.subscription_id,
        "resourceGroupName": cfg.resource_group,
        "deploymentNames": names,
        "outputs": outputs,
        "tests": tests or {},
    })


def configure_gateway(cfg, key, *, settings, caller_object_id=""):
    deployed = azure_json([
        "resource", "show", "--ids", cfg.container_app_id, "--api-version", "2026-07-01",
    ])
    url = "https://" + deployed["properties"]["configuration"]["ingress"]["fqdn"]
    test_litellm(url, key, model=cfg.model_name, retry_seconds=300)
    return run_action(
        "deploy", key, enable_gateway=True, caller_object_id=caller_object_id, settings=settings,
    )


def main():
    parser = argparse.ArgumentParser(description="Validate, preview, or deploy LiteLLM and AI Gateway Bicep templates.")
    parser.add_argument("action", choices=["validate", "what-if", "deploy"])
    parser.add_argument("--confirm", action="store_true", help="Required for resource provisioning.")
    parser.add_argument("--gateway-only", action="store_true", help="Configure AI Gateway against an existing LiteLLM app.")
    parser.add_argument(
        "--gateway-caller-object-id", default="",
        help="Assign AI Gateway Models User to this Entra user object ID at the gateway scope.",
    )
    args = parser.parse_args()
    if args.action == "deploy" and not args.confirm:
        parser.error("Provisioning requires --confirm.")
    if args.gateway_caller_object_id:
        uuid.UUID(args.gateway_caller_object_id)
    settings = local_settings()
    cfg = load_config(settings)
    verify_target(cfg)
    if args.action != "deploy":
        result = run_action(
            args.action, "sk-" + secrets.token_urlsafe(32), enable_gateway=True,
            caller_object_id=args.gateway_caller_object_id, settings=settings,
        )
        if result.get("error"):
            raise RuntimeError("Azure rejected the deployment validation or preview.")
        print(json.dumps({"action": args.action, "status": "passed"}))
        return
    if not settings.get("LITELLM_MASTER_KEY"):
        existing = azure_json([
            "resource", "list", "--subscription", cfg.subscription_id,
            "--resource-group", cfg.resource_group, "--resource-type", "Microsoft.App/containerApps",
            "--query", f"[?name=='{cfg.container_app_name}'].id",
        ])
        if existing:
            raise ValueError("LiteLLM already exists. Set its original LITELLM_MASTER_KEY; refusing silent key rotation.")
    key = master_key(settings, generate=True)
    if args.gateway_only:
        result = configure_gateway(cfg, key, settings=settings, caller_object_id=args.gateway_caller_object_id)
        outputs = deployment_outputs(result)
        names = [result["name"]]
        save_state(cfg, "gatewayProvisioned", outputs, names)
        tests = {"gateway": test_gateway(outputs["gatewayUrl"], settings, alias=cfg.gateway_alias, retry_seconds=300)}
    else:
        result = run_action("deploy", key, settings=settings)
        outputs = deployment_outputs(result, require_gateway=False)
        names = [result["name"]]
        save_state(cfg, "hostingProvisioned", outputs, names)
        tests = {"litellm": test_litellm(outputs["litellmUrl"], key, model=cfg.model_name, retry_seconds=300)}
        result = run_action(
            "deploy", key, enable_gateway=True,
            caller_object_id=args.gateway_caller_object_id, settings=settings,
        )
        outputs = deployment_outputs(result)
        names.append(result["name"])
        save_state(cfg, "gatewayProvisioned", outputs, names, tests)
        tests["gateway"] = test_gateway(outputs["gatewayUrl"], settings, alias=cfg.gateway_alias, retry_seconds=300)
    save_state(cfg, "healthy", outputs, names, tests)
    print(json.dumps({"outputs": outputs, "tests": tests}, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (
        ValueError, RuntimeError, KeyError, OSError, urllib.error.URLError, subprocess.SubprocessError,
    ) as error:
        print(f"Deployment stopped: {error}", file=sys.stderr)
        sys.exit(1)
