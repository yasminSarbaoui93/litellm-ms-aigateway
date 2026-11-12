from contextlib import ExitStack
import os
from pathlib import Path
import tempfile
from unittest.mock import patch

import common


def configured_environment():
    values = {
        "AZURE_SUBSCRIPTION_ID": "11111111-1111-4111-8111-111111111111",
        "AZURE_RESOURCE_GROUP": "rg-example",
        "AI_GATEWAY_NAME": "gw-example",
        "FOUNDRY_ACCOUNT_NAME": "foundry-example",
        "NAME_PREFIX": "litellm-test",
    }
    for name in ("BICEP_CLI_PATH", "PATH", "HOME"):
        if os.environ.get(name):
            values[name] = os.environ[name]
    stack = ExitStack()
    temp_dir = Path(stack.enter_context(tempfile.TemporaryDirectory()))
    stack.enter_context(patch.dict(os.environ, values, clear=True))
    stack.enter_context(patch.object(common, "ENV_FILE", temp_dir / "env" / ".env"))
    stack.enter_context(patch.object(common, "STATE_FILE", temp_dir / "env" / "deployment-state.json"))
    return stack


def patch_env_dir(directory):
    folder = Path(directory) / "env"
    folder.mkdir(exist_ok=True)
    stack = ExitStack()
    stack.enter_context(patch.object(common, "ENV_FILE", folder / ".env"))
    stack.enter_context(patch.object(common, "STATE_FILE", folder / "deployment-state.json"))
    return stack, folder / ".env"
