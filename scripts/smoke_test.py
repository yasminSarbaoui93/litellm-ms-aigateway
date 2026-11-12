import argparse
import base64
from dataclasses import dataclass, field
import hashlib
import hmac
import json
import subprocess
import sys
import time
import urllib.error

from common import (
    HttpFailure,
    access_token,
    load_config,
    local_settings,
    master_key,
    read_state,
    request_json,
)


def validate_completion(data, marker):
    if not isinstance(data, dict) or data.get("error"):
        raise ValueError("Completion returned an error or an invalid response object.")
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("Completion is missing choices.")
    if not isinstance(choices[0], dict):
        raise ValueError("Completion choice is not an object.")
    message = choices[0].get("message")
    if not isinstance(message, dict) or not isinstance(message.get("content"), str):
        raise ValueError("Completion is missing text content.")
    if message["content"].strip() != marker:
        raise ValueError(f"Completion did not return the expected marker {marker}.")
    usage = data.get("usage")
    if not isinstance(usage, dict) or type(usage.get("total_tokens")) is not int or usage["total_tokens"] <= 0:
        raise ValueError("Completion is missing token usage.")
    model = data.get("model")
    if not isinstance(model, str) or not model:
        raise ValueError("Completion is missing the model identifier.")
    return {"model": model, "totalTokens": usage["total_tokens"], "marker": marker}


def validate_stream(text, marker):
    fragments = []
    done = False
    for line in text.splitlines():
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            done = True
            continue
        chunk = json.loads(payload)
        if chunk.get("error"):
            raise ValueError("Streaming endpoint returned an error.")
        for choice in chunk.get("choices", []):
            content = choice.get("delta", {}).get("content")
            if content:
                fragments.append(content)
    if not done or "".join(fragments).strip() != marker:
        raise ValueError("Incomplete or unexpected streaming completion.")


def request_with_retry(url, *, retry_seconds=0, **kwargs):
    deadline = time.monotonic() + retry_seconds
    while True:
        try:
            return request_json(url, **kwargs)
        except HttpFailure as error:
            if error.status not in {401, 403, 429, 502, 503, 504} or time.monotonic() >= deadline:
                raise
            print(f"Waiting for startup/RBAC propagation (HTTP {error.status})...", file=sys.stderr)
        except urllib.error.URLError:
            if time.monotonic() >= deadline:
                raise
            print("Waiting for endpoint connectivity...", file=sys.stderr)
        time.sleep(min(10, max(0, deadline - time.monotonic())))


def completion(url, model, token, marker, *, retry_seconds=0):
    body = {
        "model": model,
        "messages": [{"role": "user", "content": "Reply with exactly: " + marker}],
        "max_tokens": 24,
        "temperature": 0,
    }
    data = request_with_retry(url, token=token, body=body, retry_seconds=retry_seconds)
    result = validate_completion(data, marker)
    body["stream"] = True
    validate_stream(request_with_retry(
        url, token=token, body=body, stream=True, retry_seconds=retry_seconds,
    ), marker)
    result["streaming"] = "passed"
    return result


def reject_anonymous(url, model):
    try:
        request_json(url, body={"model": model, "messages": [{"role": "user", "content": "Hello"}]})
    except HttpFailure as error:
        if error.status in {401, 403}:
            return
        raise
    raise ValueError("Unauthenticated inference unexpectedly succeeded.")


def test_litellm(url, key, *, model="gpt-4o", retry_seconds=0):
    url = url.rstrip("/")
    request_with_retry(url + "/health/liveliness", retry_seconds=retry_seconds)
    endpoint = url + "/v1/chat/completions"
    result = completion(endpoint, model, key, "LITELLM_OK", retry_seconds=retry_seconds)
    reject_anonymous(endpoint, model)
    result["anonymousAccess"] = "rejected"
    return result


def test_gateway(url, settings, *, alias="gpt-4o-litellm", retry_seconds=0):
    token = settings.get("AI_GATEWAY_API_KEY") or access_token("https://ai.gateway.azure.com", settings)
    endpoint = url.rstrip("/") + "/chat/completions"
    result = completion(endpoint, alias, token, "GATEWAY_OK", retry_seconds=retry_seconds)
    reject_anonymous(endpoint, alias)
    result["anonymousAccess"] = "rejected"
    return result


def test_model_hub(url, *, model="gpt-4o"):
    data = request_json(url.rstrip("/") + "/public/v1/model_hub")
    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
        raise ValueError("Public Model Hub did not return the expected data array.")
    models = []
    for row in data["data"]:
        if not isinstance(row, dict) or not isinstance(row.get("model_group"), str):
            raise ValueError("Public Model Hub returned an invalid model entry.")
        models.append(row["model_group"])
    if model not in models:
        raise ValueError(f"Foundry-backed {model} is missing from the public Model Hub.")
    return {"models": models, "anonymousMetadata": "available"}


@dataclass(frozen=True)
class AdminSession:
    user_id: str
    key: str = field(repr=False)


def decode_admin_session(token, key):
    if not isinstance(token, str):
        raise ValueError("UI login returned no session token.")
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("UI login returned an invalid session token.")
    header, payload, signature = parts
    decode = lambda value: base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    expected = hmac.new(key.encode(), (header + "." + payload).encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(decode(signature), expected):
        raise ValueError("UI session token signature is invalid.")
    if json.loads(decode(header)).get("alg") != "HS256":
        raise ValueError("Unexpected UI session signing algorithm.")
    claims = json.loads(decode(payload))
    if not isinstance(claims, dict):
        raise ValueError("Invalid UI session claims.")
    expires = claims.get("exp")
    if claims.get("user_role") != "proxy_admin" or type(expires) not in (int, float) or expires <= time.time():
        raise ValueError("UI session is expired or is not a proxy administrator.")
    if not isinstance(claims.get("user_id"), str) or not isinstance(claims.get("key"), str) or not claims["key"]:
        raise ValueError("UI session lacks a backed admin identity and key.")
    if claims["key"] == key:
        raise ValueError("UI login returned the master key rather than a database-backed session.")
    return AdminSession(user_id=claims["user_id"], key=claims["key"])


def verify_admin_access(url, session, *, model="gpt-4o"):
    data = request_json(url.rstrip("/") + "/model/info", token=session.key)
    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
        raise ValueError("Authenticated admin model listing failed.")
    if not any(row.get("model_name") == model for row in data["data"] if isinstance(row, dict)):
        raise ValueError(f"Foundry-backed {model} is missing from the authenticated Admin UI.")


def wait_for_database(url, *, retry_seconds=0):
    deadline = time.monotonic() + retry_seconds
    while True:
        readiness = request_with_retry(
            url.rstrip("/") + "/health/readiness",
            retry_seconds=max(0, deadline - time.monotonic()),
        )
        if isinstance(readiness, dict) and readiness.get("db") == "connected":
            return
        if time.monotonic() >= deadline:
            raise ValueError("Admin UI requires a connected database; a successful login page is insufficient.")
        print("Waiting for the database-connected revision...", file=sys.stderr)
        time.sleep(3)


def admin_login(url, settings, *, model="gpt-4o", retry_seconds=0):
    wait_for_database(url, retry_seconds=retry_seconds)
    password = settings.get("UI_PASSWORD")
    if not password:
        raise ValueError("UI_PASSWORD is required for the Admin UI login test.")
    response = request_json(url.rstrip("/") + "/v2/login", body={
        "username": "admin", "password": password,
    })
    if not isinstance(response, dict):
        raise ValueError("Admin UI login returned an invalid response.")
    session = decode_admin_session(response.get("token"), master_key(settings))
    verify_admin_access(url, session, model=model)
    return session


def test_admin_ui(url, settings, *, model="gpt-4o", retry_seconds=0):
    session = admin_login(url, settings, model=model, retry_seconds=retry_seconds)
    return {"database": "connected", "login": "passed", "adminRequest": "passed", "userId": session.user_id}


def main():
    parser = argparse.ArgumentParser(description="Live per-hop inference tests (consumes model tokens).")
    parser.add_argument("--hop", choices=["foundry", "litellm", "gateway", "model-hub", "admin-ui", "all"], default="all")
    parser.add_argument("--litellm-url", help="Defaults to litellmUrl from env/deployment-state.json.")
    parser.add_argument("--gateway-url", help="Defaults to gatewayUrl from env/deployment-state.json.")
    parser.add_argument("--retry-seconds", type=int, default=300)
    args = parser.parse_args()
    if args.retry_seconds < 0:
        parser.error("--retry-seconds must be nonnegative.")
    settings = local_settings()
    cfg = load_config(settings)
    saved_outputs = read_state().get("outputs", {})
    litellm_url = args.litellm_url or saved_outputs.get("litellmUrl")
    gateway_url = args.gateway_url or saved_outputs.get("gatewayUrl")
    if args.hop in {"litellm", "model-hub", "admin-ui", "all"} and not litellm_url:
        parser.error("--litellm-url is required when env/deployment-state.json has no litellmUrl.")
    if args.hop in {"gateway", "all"} and not gateway_url:
        parser.error("--gateway-url is required when env/deployment-state.json has no gatewayUrl.")
    results = {}
    if args.hop == "admin-ui":
        results["adminUi"] = test_admin_ui(litellm_url, settings, model=cfg.model_name, retry_seconds=args.retry_seconds)
    if args.hop in {"model-hub", "all"}:
        results["modelHub"] = test_model_hub(litellm_url, model=cfg.model_name)
    if args.hop in {"foundry", "all"}:
        results["foundry"] = completion(
            cfg.foundry_chat_url, cfg.model_name,
            access_token("https://cognitiveservices.azure.com/", settings), "FOUNDRY_OK",
        )
    if args.hop in {"litellm", "all"}:
        results["litellm"] = test_litellm(
            litellm_url, master_key(settings), model=cfg.model_name, retry_seconds=args.retry_seconds,
        )
    if args.hop in {"gateway", "all"}:
        results["gateway"] = test_gateway(
            gateway_url, settings, alias=cfg.gateway_alias, retry_seconds=args.retry_seconds,
        )
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError, OSError, urllib.error.URLError, subprocess.SubprocessError) as error:
        print(f"Smoke test failed: {error}", file=sys.stderr)
        sys.exit(1)
