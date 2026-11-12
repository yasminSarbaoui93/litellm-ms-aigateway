from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import urllib.error
import urllib.parse
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / "env" / ".env"
STATE_FILE = ROOT / "env" / "deployment-state.json"
DEFAULT_IMAGE = (
    "ghcr.io/berriai/litellm"
    "@sha256:11175975a3093e05e92faee2fe2fe3412a3ae2fcaa849eb0fe25d5f0d8bcb2e9"
)
NAME_PATTERN = re.compile(r"[A-Za-z0-9_().-]{1,90}")


def local_settings():
    values = {}
    if ENV_FILE.exists():
        for number, line in enumerate(ENV_FILE.read_text().splitlines(), 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            name, separator, value = line.partition("=")
            if not separator or not re.fullmatch(r"[A-Z][A-Z0-9_]*", name):
                raise ValueError(f"Invalid env/.env syntax on line {number}; use NAME=value.")
            if name in values:
                raise ValueError(f"Duplicate env/.env variable on line {number}.")
            if value.startswith(("'", '"')):
                if len(value) < 2 or value[-1] != value[0]:
                    raise ValueError(f"Unclosed quote in env/.env on line {number}.")
                value = value[1:-1]
            values[name] = value
    values.update({name: value for name, value in os.environ.items() if value or name not in values})
    return values


def _require(values, name):
    value = values.get(name, "").strip()
    if not value or "<" in value or ">" in value:
        raise ValueError(f"Set {name} in env/.env (copy from env/.env.example).")
    return value


def _resource_name(value, label):
    if not NAME_PATTERN.fullmatch(value) or value.endswith(".") or "<" in value or ">" in value:
        raise ValueError(f"{label} must be a valid Azure resource name, not a path or placeholder.")
    return value


@dataclass(frozen=True)
class Config:
    subscription_id: str
    resource_group: str
    gateway_name: str
    foundry_account: str
    foundry_resource_group: str
    foundry_api_base: str
    foundry_api_version: str
    model_name: str
    location: str
    postgres_location: str
    name_prefix: str
    container_image: str
    container_app_name: str
    managed_env_name: str
    log_analytics_name: str
    postgres_server_name: str

    @property
    def gateway_alias(self):
        return f"{self.model_name}-litellm"

    @property
    def database_host(self):
        return f"{self.postgres_server_name}.postgres.database.azure.com"

    @property
    def foundry_chat_url(self):
        base = self.foundry_api_base.rstrip("/")
        return f"{base}/openai/deployments/{self.model_name}/chat/completions?api-version={self.foundry_api_version}"

    @property
    def container_app_id(self):
        return (
            f"/subscriptions/{self.subscription_id}/resourceGroups/{self.resource_group}"
            f"/providers/Microsoft.App/containerApps/{self.container_app_name}"
        )

    @property
    def arm_app_url(self):
        return "https://management.azure.com" + self.container_app_id

    def deployment_name(self, phase):
        return f"{self.name_prefix}-{phase}"[:64]

    def bicep_parameters(self):
        return {
            name: {"value": value}
            for name, value in {
                "environmentName": self.name_prefix,
                "location": self.location,
                "resourceGroupName": self.resource_group,
                "containerImage": self.container_image,
                "gatewayName": self.gateway_name,
                "containerAppName": self.container_app_name,
                "managedEnvironmentName": self.managed_env_name,
                "logAnalyticsName": self.log_analytics_name,
                "foundryResourceGroupName": self.foundry_resource_group,
                "foundryAccountName": self.foundry_account,
                "foundryApiBase": self.foundry_api_base,
                "foundryApiVersion": self.foundry_api_version,
                "modelName": self.model_name,
                "postgresServerName": self.postgres_server_name,
                "postgresLocation": self.postgres_location,
            }.items()
        }


def load_config(settings=None):
    values = local_settings() if settings is None else settings
    sub_id = str(uuid.UUID(_require(values, "AZURE_SUBSCRIPTION_ID")))
    rg = _resource_name(_require(values, "AZURE_RESOURCE_GROUP"), "AZURE_RESOURCE_GROUP")
    gateway = _resource_name(_require(values, "AI_GATEWAY_NAME"), "AI_GATEWAY_NAME")
    foundry = _resource_name(_require(values, "FOUNDRY_ACCOUNT_NAME"), "FOUNDRY_ACCOUNT_NAME")
    foundry_rg = _resource_name(
        values.get("FOUNDRY_RESOURCE_GROUP") or rg, "FOUNDRY_RESOURCE_GROUP",
    )
    prefix = values.get("NAME_PREFIX") or "litellm-demo"
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?", prefix):
        raise ValueError("NAME_PREFIX must be 1-40 lowercase alphanumeric characters or hyphens.")
    model = _resource_name(values.get("FOUNDRY_MODEL_NAME") or "gpt-4o", "FOUNDRY_MODEL_NAME")
    api_version = values.get("FOUNDRY_API_VERSION") or "2024-10-21"
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}(-preview)?", api_version):
        raise ValueError("FOUNDRY_API_VERSION must be a date-based Azure OpenAI API version.")
    api_base = values.get("FOUNDRY_API_BASE") or f"https://{foundry}.openai.azure.com/"
    parsed = urllib.parse.urlsplit(api_base)
    if (
        parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
        or not re.fullmatch(r"[A-Za-z0-9.-]+", parsed.hostname)
        or parsed.path not in {"", "/"} or parsed.query or parsed.fragment or parsed.port
    ):
        raise ValueError("FOUNDRY_API_BASE must be the HTTPS inference origin (not a Foundry project URL).")
    image = values.get("LITELLM_IMAGE") or DEFAULT_IMAGE
    if "@sha256:" not in image:
        raise ValueError("LITELLM_IMAGE must be pinned to an immutable @sha256 digest.")
    return Config(
        subscription_id=sub_id,
        resource_group=rg,
        gateway_name=gateway,
        foundry_account=foundry,
        foundry_resource_group=foundry_rg,
        foundry_api_base=api_base,
        foundry_api_version=api_version,
        model_name=model,
        location=_resource_name(values.get("AZURE_LOCATION") or "swedencentral", "AZURE_LOCATION"),
        postgres_location=_resource_name(values.get("POSTGRES_LOCATION") or "northeurope", "POSTGRES_LOCATION"),
        name_prefix=prefix,
        container_image=image,
        container_app_name=_resource_name(values.get("CONTAINER_APP_NAME") or f"ca-{prefix}", "CONTAINER_APP_NAME"),
        managed_env_name=_resource_name(values.get("MANAGED_ENV_NAME") or f"cae-{prefix}", "MANAGED_ENV_NAME"),
        log_analytics_name=_resource_name(values.get("LOG_ANALYTICS_NAME") or f"log-{prefix}", "LOG_ANALYTICS_NAME"),
        postgres_server_name=_resource_name(values.get("POSTGRES_SERVER_NAME") or f"psql-{prefix}", "POSTGRES_SERVER_NAME"),
    )


def read_state():
    return json.loads(STATE_FILE.read_text()) if STATE_FILE.is_file() else {}


def write_state(updates):
    STATE_FILE.parent.mkdir(exist_ok=True)
    if STATE_FILE.is_symlink():
        raise ValueError("Refusing to write deployment state through a symlink.")
    state = {**read_state(), **updates, "updatedUtc": datetime.now(timezone.utc).isoformat()}
    fd = os.open(STATE_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as file:
        json.dump(state, file, indent=2)
        file.write("\n")


def master_key(settings, *, generate=False):
    key = settings.get("LITELLM_MASTER_KEY", "")
    if not key:
        if not generate:
            raise ValueError("Set LITELLM_MASTER_KEY in the environment or env/.env.")
        key = "sk-" + secrets.token_urlsafe(32)
        persist_local_settings({"LITELLM_MASTER_KEY": key})
    if not re.fullmatch(r"sk-[A-Za-z0-9_-]{32,}", key):
        raise ValueError("LITELLM_MASTER_KEY must be sk- followed by at least 32 URL-safe characters.")
    return key


def persist_local_settings(updates):
    for name, value in updates.items():
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", name) or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
            raise ValueError("Generated local settings must use URL-safe, single-line values.")
    ENV_FILE.parent.mkdir(exist_ok=True)
    if ENV_FILE.is_symlink():
        raise ValueError("Refusing to store deployment credentials through a symlink.")
    lines = []
    if ENV_FILE.exists():
        lines = [
            line for line in ENV_FILE.read_text().splitlines()
            if line.strip().partition("=")[0] not in updates
        ]
    fd = os.open(ENV_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as file:
        file.write("\n".join([*lines, *(f"{name}={value}" for name, value in updates.items()), ""]))


def redact(text, sensitive=()):
    for value in sensitive:
        if value:
            text = text.replace(value, "[REDACTED]")
    return text


def azure_json(arguments, *, sensitive=(), timeout=120):
    result = subprocess.run(
        ["az", *arguments, "--only-show-errors", "--output", "json"],
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(redact(result.stderr.strip(), sensitive) or "Azure CLI command failed.")
    return json.loads(result.stdout)


def access_token(resource, settings=None):
    cfg = load_config(settings)
    result = azure_json([
        "account", "get-access-token", "--subscription", cfg.subscription_id,
        "--resource", resource,
    ])
    token = result.get("accessToken")
    if not isinstance(token, str) or not token:
        raise ValueError("Azure CLI did not return an access token.")
    return token


class HttpFailure(RuntimeError):
    def __init__(self, status, url):
        self.status = status
        super().__init__(f"HTTP {status} from {url.split('?')[0]}; inspect resource logs for details.")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request_json(url, *, token=None, body=None, stream=False):
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise ValueError("Use an HTTPS endpoint without embedded credentials.")
    headers = {"Accept": "text/event-stream" if stream else "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    payload = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        payload = json.dumps(body).encode()
    request = urllib.request.Request(url, data=payload, headers=headers)
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=120) as response:
            data = response.read().decode()
            return data if stream else json.loads(data)
    except urllib.error.HTTPError as error:
        raise HttpFailure(error.code, url) from None
