import json
import shlex
import time
from urllib.parse import quote_plus, urlsplit

from common import ROOT, azure_json, load_config, redact
from smoke_test import request_with_retry

API_VERSION = "2026-07-01"

BRIDGE = """
import json,runpy,sys,tempfile,termios
fd=sys.stdin.fileno()
settings=termios.tcgetattr(fd)
settings[3] &= ~(termios.ECHO|termios.ICANON)
termios.tcsetattr(fd,termios.TCSANOW,settings)
print("LITELLM_DB_READY",flush=True)
body=json.loads(sys.stdin.readline())
if body["payload"]["action"] in ("bootstrap","migrate"):
    from prisma.errors import PrismaError
else:
    PrismaError=RuntimeError
with tempfile.NamedTemporaryFile(mode="w",suffix=".py") as code:
    code.write(body["source"])
    code.flush()
    try:
        runpy.run_path(code.name,init_globals={"PAYLOAD":body["payload"]},run_name="__main__")
    except (ValueError,RuntimeError,OSError,ImportError,PrismaError) as error:
        print("LITELLM_DB_RESULT:"+json.dumps({"ok":False,"errorType":type(error).__name__}),flush=True)
"""


def console_url(log_endpoint, revision, replica, cfg=None):
    cfg = load_config() if cfg is None else cfg
    parsed = urlsplit(log_endpoint)
    if parsed.scheme != "https" or parsed.username or parsed.password or not parsed.hostname:
        raise ValueError("Invalid ARM-provided console endpoint.")
    if not parsed.hostname.endswith((".azurecontainerapps.io", ".azurecontainerapps.dev")):
        raise ValueError("Unexpected Container Apps console domain.")
    return (
        f"wss://{parsed.netloc}/subscriptions/{cfg.subscription_id}/resourceGroups/{cfg.resource_group}"
        f"/containerApps/{cfg.container_app_name}/revisions/{revision}/replicas/{replica}"
        f"/containers/litellm/exec?command={quote_plus('/bin/sh')}"
    )


def run_database_operation(payload, *, cfg=None, timeout=720):
    try:
        import websocket
    except ImportError as error:
        raise RuntimeError("Install requirements.txt in the virtual environment first.") from error
    cfg = load_config() if cfg is None else cfg
    url = cfg.arm_app_url
    app = azure_json(["rest", "--method", "get", "--url", url + "?api-version=" + API_VERSION])
    fqdn = app["properties"]["configuration"]["ingress"]["fqdn"]
    request_with_retry("https://" + fqdn + "/health/liveliness", retry_seconds=120)
    revision = app["properties"]["latestReadyRevisionName"]
    replicas = azure_json([
        "rest", "--method", "get", "--url",
        url + f"/revisions/{revision}/replicas?api-version={API_VERSION}",
    ])["value"]
    for replica in replicas:
        containers = replica["properties"].get("containers", [])
        target = next((container for container in containers if container["name"] == "litellm"), None)
        if target:
            break
    else:
        raise RuntimeError("No running LiteLLM replica; warm the health endpoint before remote execution.")
    endpoint = console_url(target["logStreamEndpoint"], revision, replica["name"], cfg=cfg)
    token = azure_json([
        "rest", "--method", "post", "--url", url + "/getAuthToken?api-version=" + API_VERSION,
    ])["properties"]["token"]
    try:
        channel = websocket.create_connection(
            endpoint, header={"Authorization": "Bearer " + token}, timeout=15,
        )
    except websocket.WebSocketException as error:
        raise RuntimeError(f"Container console connection failed ({type(error).__name__}).") from None
    channel.send_binary(b"\x00\x04" + json.dumps({"Width": 200, "Height": 40}).encode())
    buffer = ""
    started = False
    sent = False
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            try:
                frame = channel.recv()
            except websocket.WebSocketTimeoutException:
                continue
            if not frame:
                break
            if not isinstance(frame, bytes):
                raise RuntimeError("Unexpected console frame type.")
            if frame[0] == 2:
                raise RuntimeError("Container Apps rejected the remote execution session.")
            if frame[0] == 1:
                buffer = (buffer + frame[1:].decode("utf-8", errors="replace"))[-65536:]
                continue
            if len(frame) < 2 or frame[0] != 0:
                continue
            buffer = (buffer + frame[2:].decode("utf-8", errors="replace"))[-65536:]
            if not started:
                command = shlex.join(["python", "-u", "-c", BRIDGE]) + "\n"
                channel.send_binary(b"\x00\x00" + command.encode())
                started = True
            if not sent and any(line.strip() == "LITELLM_DB_READY" for line in buffer.splitlines()):
                source = (ROOT / "scripts/database_remote.py").read_text()
                message = (json.dumps({"source": source, "payload": payload}) + "\n").encode()
                for offset in range(0, len(message), 512):
                    channel.send_binary(b"\x00\x00" + message[offset:offset + 512])
                sent = True
                buffer = ""
            marker = "LITELLM_DB_RESULT:"
            if sent and marker in buffer:
                tail = buffer.split(marker, 1)[1]
                if "\n" not in tail:
                    continue
                result = json.loads(tail.splitlines()[0])
                if result.get("ok") is not True:
                    raise RuntimeError(f"Remote database {payload['action']} failed ({result.get('errorType', 'unknown')}).")
                return result["result"]
        diagnostic = ""
        if payload.get("action") == "probe":
            diagnostic = " Probe diagnostic: " + redact(buffer[-600:], (token,))
        raise RuntimeError("Remote database operation ended without a verified completion result." + diagnostic)
    finally:
        channel.close()
