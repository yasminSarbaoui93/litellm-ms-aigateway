# LiteLLM on Azure with Azure AI Gateway

Reference deployment connecting a Microsoft Foundry model deployment to a self-hosted LiteLLM proxy on Azure Container Apps, then publishing it behind the dedicated Azure AI Gateway (`Microsoft.ApiManagement/aigateways`):

```text
Caller -> Azure AI Gateway -> LiteLLM (Container Apps) -> Microsoft Foundry
          <model>-litellm     <model>                     azure/<model>
```

Model weights stay in Foundry. LiteLLM authenticates to Foundry via system-assigned Managed Identity (no Foundry API keys required), and AI Gateway exposes a governed OpenAI-compatible alias.

## What gets deployed

Into your **existing** resource group and AI Gateway:

- Azure Container Apps environment, Log Analytics workspace, and LiteLLM Container App (`1 vCPU / 2 GiB`, scale `0–1`).
- `Cognitive Services OpenAI User` role assignment for the Container App on your Foundry account.
- Custom provider (`litellm`), model registration, and `<model>-litellm` alias in your AI Gateway.
- *(Optional)* Entra-only Azure Database for PostgreSQL 16 (`Standard_B1ms`, `32 GiB`) for persistent LiteLLM Admin UI login, virtual keys, and model management.

## Prerequisites

- Python 3.10+, Azure CLI (`az`), and Bicep CLI (`bicep` on `PATH` or `BICEP_CLI_PATH`).
- An existing Azure resource group, dedicated Azure AI Gateway (`Microsoft.ApiManagement/aigateways`), and Microsoft Foundry model deployment (default `gpt-4o`).
- Permissions to deploy resources and role assignments, plus **AI Gateway Models User** on the gateway to invoke models via Microsoft Entra ID.

## 1. Configure

All local settings, generated secrets, and deployment state live in `env/` (gitignored):

```bash
cp env/.env.example env/.env
chmod 600 env/.env
```

Set the 4 required variables in `env/.env`:

```dotenv
AZURE_SUBSCRIPTION_ID=<your-subscription-id>
AZURE_RESOURCE_GROUP=<your-existing-resource-group>
AI_GATEWAY_NAME=<your-existing-ai-gateway>
FOUNDRY_ACCOUNT_NAME=<your-existing-foundry-account>
```

See `env/.env.example` for optional overrides (`FOUNDRY_RESOURCE_GROUP`, `FOUNDRY_MODEL_NAME`, `AZURE_LOCATION`, `POSTGRES_LOCATION`, `NAME_PREFIX`, etc.). Note that `FOUNDRY_API_BASE` defaults to `https://<FOUNDRY_ACCOUNT_NAME>.openai.azure.com/` (the inference origin, not a Foundry project URL).

## 2. Validate & Deploy (Inference Only)

```bash
az login
az account set --subscription <your-subscription-id>

python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt

# Dry-run validation
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python scripts/deploy.py validate
.venv/bin/python scripts/deploy.py what-if

# Provision LiteLLM + AI Gateway registration + end-to-end smoke tests
.venv/bin/python scripts/deploy.py deploy --confirm
```

`LITELLM_MASTER_KEY` is generated automatically on first deploy and saved to `env/.env`. Pass `--gateway-caller-object-id <entra-user-object-id>` if the caller still needs the **AI Gateway Models User** role on the gateway. Use `--gateway-only` to re-register the gateway against an already-running LiteLLM app.

> **Routing note:** AI Gateway appends `/v1/chat/completions` to custom providers, so `infra/gateway.bicep` registers the LiteLLM origin *without* `/v1` and outputs the alias's `gatewayEndpoints[0].baseUrl` for callers.

## 3. Optional: Enable Persistent Admin UI (PostgreSQL)

LiteLLM's Admin UI (`/ui`), virtual keys, and spend tracking require PostgreSQL. The optional database flow uses Entra-only authentication (no static DB passwords), restricts firewall rules to the Container App's outbound IPs, bootstraps a non-admin `litellm_app` role, and runs Prisma migrations inside the running container:

```bash
.venv/bin/python scripts/database_deploy.py probe
.venv/bin/python scripts/database_deploy.py what-if
.venv/bin/python scripts/database_deploy.py deploy --confirm
```

Once enabled:

- Open `<litellmUrl>/ui` and sign in with username `admin` and **`UI_PASSWORD` from `env/.env`** (not `LITELLM_MASTER_KEY`).
- Public model metadata is available at `<litellmUrl>/ui/model_hub_table` (unauthenticated inference remains blocked).
- Keep `LITELLM_MASTER_KEY`, `UI_PASSWORD`, and `LITELLM_SALT_KEY` in `env/.env`; use `database_deploy.py deploy --confirm` for subsequent redeployments.

## 4. Smoke Tests

Run live completion, SSE streaming, and auth-rejection checks per hop (uses URLs from `env/deployment-state.json` automatically, or pass `--litellm-url` / `--gateway-url`):

```bash
.venv/bin/python scripts/smoke_test.py --hop all
.venv/bin/python scripts/smoke_test.py --hop admin-ui
```

Inbound AI Gateway calls use `AI_GATEWAY_API_KEY` (`Authorization: Bearer <key>`) when set, or acquire an Entra token for `https://ai.gateway.azure.com`.

## Notes & Cleanup

- **Demo scope:** Sized minimal (`1 vCPU / 2 GiB`, `0–1` replicas, public HTTPS ingress with auth, burstable non-HA PostgreSQL). Review VNet/private networking, replica sizing (`>= 4 GiB` per LiteLLM production docs), and restricted virtual keys before production use.
- **Cleanup:** Do **not** delete a shared resource group, pre-existing AI Gateway, or Foundry account. Delete only the demo's gateway alias/model/provider (`<model>-litellm`, `<model>`, `litellm`), Foundry role assignment, Container App, Container Apps environment, Log Analytics workspace, and optional PostgreSQL server.

## References

- [LiteLLM Docker Quickstart](https://docs.litellm.ai/docs/proxy/docker_quick_start)
- [LiteLLM Azure OpenAI Provider](https://docs.litellm.ai/docs/providers/azure)
- [Azure AI Gateway Overview](https://learn.microsoft.com/azure/ai-gateway/overview)
- [Connect Models to Microsoft Foundry Agent Service](https://learn.microsoft.com/azure/foundry/agents/how-to/connected-models)
