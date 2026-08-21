# LangchainOA

A read-only Microsoft Agent 365 **AI Teammate** built with **LangChain** and **Azure OpenAI**.

It searches and reads governed Microsoft 365 data through Work IQ, inspects Azure
resources and monitoring data, and answers questions. It has no tools that send
mail, modify calendars, change documents, or alter Azure resources.

## Documentation

| Guide | Read it for |
|---|---|
| [Capabilities and permissions](docs/capabilities-and-permissions.md) | What the agent can do, every permission it holds, and where to inspect or change them |
| [Architecture](docs/architecture.md) | Diagrams of the components, startup, one turn, identity, telemetry, and routes |
| [Deploy in your tenant](docs/deploy-in-your-tenant.md) | Clone to running agent, and every value you must change |
| [Troubleshooting](docs/troubleshooting.md) | Real failure modes, their causes, and fixes |

## Why this combination

| Layer | Choice | Note |
|---|---|---|
| Surface | Microsoft 365 / Teams | Activity Protocol on `/api/messages` |
| Host | Microsoft 365 Agents SDK | aiohttp |
| Orchestration | LangChain | `create_agent` |
| Model | Azure OpenAI | Keyless via managed identity — no API key |
| M365 data | Work IQ MCP | SharePoint, mail, calendar — read-only |
| Azure data | Azure SDKs | Resource Manager, Monitor, Log Analytics |
| Telemetry | Microsoft OpenTelemetry Distro | Optional Agent 365 export |
| Identity | Entra Agent ID | Blueprint → Agent Identity → agentic user |

There is no model API key anywhere in this agent. The Azure OpenAI client
authenticates with `DefaultAzureCredential`, which resolves to your `az login`
session locally and to the assigned managed identity in Azure.

## Architecture

```text
Microsoft 365 / Teams
        |
        v
Microsoft 365 Agents SDK host  (/api/messages)
        |
        +-- agentic auth handler --> Work IQ delegated token
        |
        v
LangChain agent --> Azure OpenAI (keyless)
        |
        +-- Work IQ MCP ----------> SharePoint / Mail / Calendar   (read-only)
        +-- DefaultAzureCredential -> Azure ARM / Monitor / Logs   (read-only)
        |
        v
Microsoft OpenTelemetry Distro --> Agent 365 observability (optional)
```

## Repository layout

```text
langchain-oa/
├── main.py                     entry point
├── host.py                     Activity Protocol host: routes, token exchange
├── langchain_agent.py          orchestration and conversation history
├── aoai_model.py               keyless Azure OpenAI chat model
├── observability.py            telemetry setup and baggage
├── purview.py                  optional content capture and DLP
├── token_cache.py              observability token cache
├── config.py                   typed settings, no secret defaults
├── agent-prompt.txt            system prompt
│
├── tools/
│   ├── workiq_tools.py         governed Microsoft 365 access
│   └── azure_tools.py          read-only Azure inspection
│
├── tests/                      contract tests for the risky parts
├── infra/                      deploy, settings sync, packaging
├── docs/                       architecture, deployment, troubleshooting
│
├── a365.config.template.json   copy to a365.config.json and fill in
├── .env.template               copy to .env and fill in
├── ToolingManifest.json        Work IQ servers, generated per tenant
├── Dockerfile                  container image
├── requirements.txt            runtime dependencies, pinned
└── requirements-dev.txt        adds pytest
```

### Why each piece exists

| Path | Why it is separate |
|---|---|
| `host.py` | All Microsoft 365 coupling lives here. Swapping the orchestrator does not touch it |
| `langchain_agent.py` | The only file that knows about LangChain. Replace it to use another framework |
| `aoai_model.py` | One place to change model provider or credential |
| `observability.py` / `purview.py` | Governance is additive. Both degrade to no-ops when disabled |
| `config.py` | Every setting is declared and typed. Nothing reads `os.environ` ad hoc |
| `agent-prompt.txt` | Behaviour changes need no rebuild |
| `tools/` | Capability boundary. Deleting a tool removes the capability — the prompt is not a control |
| `infra/` | Deployment is reproducible and reviewable, not a sequence of portal clicks |

### Files you create locally

These are produced during setup, are specific to your tenant, and are
git-ignored. You will see them locally but never in the repository.

| File | Created by | Contains |
|---|---|---|
| `.env` | You, from `.env.template` | Endpoints and the Blueprint secret |
| `a365.config.json` | You, from the template | Tenant, subscription, endpoint |
| `a365.generated.config.json` | `a365 setup all` | Blueprint and identity IDs, and a secret |
| `manifest/` | `a365 publish` | Upload package with your IDs baked in |
| `.venv/` | `python -m venv` | Local dependencies |

`ToolingManifest.json` is the exception: it is committed because it holds public
server metadata — URL, OAuth audience, scope — and no credentials. Regenerate it
per tenant with `a365 develop add-mcp-servers`, as server availability and
audiences differ.

---

## Prerequisites

1. Python 3.12
2. Azure CLI, signed in
3. An Azure OpenAI resource with a chat deployment
4. The **Cognitive Services OpenAI User** role on that resource for your account
5. Reader on the subscription you want the Azure tools to inspect
6. For Agent 365 features: the `a365` CLI and a tenant onboarded to Agent 365

---

## Blueprint authentication modes

The model calls are always keyless — `DefaultAzureCredential` plus the
**Cognitive Services OpenAI User** role, never an API key. That credential is
convenient for a sample because it resolves `az login` locally and the managed
identity in Azure without branching; in production, pin the identity instead of
letting it be inferred. See
[Credentials](docs/architecture.md#7-credentials) for the alternatives and the
roles each one needs.

Proving that this agent *is* the Blueprint app is a separate decision, set by
`CONNECTIONS__SERVICE_CONNECTION__SETTINGS__AUTHTYPE`:

| Mode | Use for | Tradeoff |
|---|---|---|
| `ClientSecret` (default) | Prototypes and demos | A real secret exists: it can leak, must be rotated, and expires |
| `FederatedCredentials` | Production | Keyless. The Blueprint app trusts a managed identity, so there is no secret to store or rotate |

> **Caveat.** This sample ships `ClientSecret` because it is the fastest path to
> a working agent, and because `FederatedCredentials` is not implemented in the
> pinned `microsoft-agents-* 0.5.3`; it was added in the 1.x line. Treat the
> secret as sensitive: keep it out of source control, and rotate it if it is
> ever printed. For production, upgrade the SDK and switch to
> `FederatedCredentials` with a user-assigned managed identity.

### Verified stack

These are the versions this agent was actually run and validated against, not
just the newest available. Pinning matters here: the Agents SDK 1.x line changes
the supported auth types, and the OpenTelemetry Distro changes instrumentation
behavior.

| Component | Version |
|---|---|
| Python | 3.12 |
| `microsoft-agents-*` (activity, hosting-core, hosting-aiohttp, authentication-msal) | 0.5.3 |
| `microsoft-opentelemetry` | 1.3.5 |
| `microsoft-agents-a365-observability-core` / `-runtime` | 1.0.0 |
| `langchain` / `langchain-openai` | 1.x |
| Model | Azure OpenAI `gpt-5.2`, keyless |

Validated end to end on this stack: Teams turns, Work IQ SharePoint search,
Agent 365 observability export, and Purview prompt/response capture.

`FederatedCredentials` requires the Agents SDK 1.x line and has **not** been
validated here.

---

## Local testing

Run these in order. Each stage adds one capability, so a failure tells you
exactly which piece is misconfigured.

### Step 1 — Install

```bash
cd agents/agent-framework/langchain-oa
python3.12 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### Step 2 — Sign in

```bash
az login
```

This is what makes the keyless model call work. There is no API key to set.

### Step 3 — Configure

```bash
cp .env.template .env
```

Fill in only these four values for the first run:

```dotenv
AZURE_OPENAI_ENDPOINT=https://<your-aoai-resource>.openai.azure.com/
AZURE_OPENAI_DEPLOYMENT=<your-deployment-name>
AZURE_SUBSCRIPTION_ID=<your-subscription-id>
ENABLE_LOCAL_EVAL=true
```

### Step 4 — Start

```bash
python main.py
```

Expected output ends with the aiohttp server listening on port 8080.

### Step 5 — Health check

```bash
curl -s localhost:8080/api/health
```

Expected:

```json
{
  "status": "healthy",
  "agent": "LangchainOA",
  "read_only": true,
  "observability": "disabled"
}
```

### Step 6 — Test the model

```bash
curl -s -X POST localhost:8080/eval/invoke \
  -H 'content-type: application/json' \
  -d '{"message":"Say hello and tell me what you can do."}'
```

If this fails with an authorization error, the signed-in account is missing
**Cognitive Services OpenAI User** on the Azure OpenAI resource.

### Step 7 — Test the Azure tools

```bash
curl -s -X POST localhost:8080/eval/invoke \
  -H 'content-type: application/json' \
  -d '{"message":"List my Azure resource groups."}'
```

This exercises `DefaultAzureCredential` against Azure Resource Manager.

### Step 8 — Test Work IQ (optional)

Work IQ V2 servers use separate OAuth audiences. For local development, acquire
one token per server and put each value in the matching `.env` variable:

```bash
# SharePoint
a365 develop get-token --resource-id 292cff14-c0e8-4116-9e3b-99934ae05766 \
  --scopes Tools.ListInvoke.All -o raw

# Mail
a365 develop get-token --resource-id 16b1878d-62c7-4009-aa25-68989d63bbad \
  --scopes Tools.ListInvoke.All -o raw

# Calendar
a365 develop get-token --resource-id 910333d2-47e9-43ca-981f-6df2f4531ef4 \
  --scopes Tools.ListInvoke.All -o raw
```

Store them as `BEARER_TOKEN_MCP_SHAREPOINTREMOTESERVER`,
`BEARER_TOKEN_MCP_MAILTOOLS`, and `BEARER_TOKEN_MCP_CALENDARTOOLS`, restart the
agent, then:

```bash
curl -s -X POST localhost:8080/eval/invoke \
  -H 'content-type: application/json' \
  -d '{"message":"Search SharePoint for onboarding documents."}'
```

Tokens expire. Re-run `a365 develop get-token` to refresh.

### What does and does not work locally

| Capability | Local |
|---|---|
| Azure OpenAI inference | Yes, via `az login` |
| Azure resource and monitoring tools | Yes, via `az login` |
| Work IQ SharePoint / mail / calendar | Yes, with the matching per-server bearer token |
| Full `/api/messages` with a real user turn | No — requires a Microsoft 365 channel |
| Agent 365 observability instrumentation | Yes; exporter stays idle without a real M365 turn |

`/eval/invoke` exists because a real Activity Protocol turn cannot be simulated
locally. It is gated behind `ENABLE_LOCAL_EVAL` and must stay `false` in any
deployed environment.

---

## Registering with Agent 365

```bash
# 1. Provision the Blueprint and AI Teammate identity
a365 setup all --agent-name <your-agent-name> --aiteammate --m365

# 2. Choose Work IQ servers from the live catalog
a365 develop list-available
a365 develop add-mcp-servers "mcp_SharePointRemoteServer" "mcp_MailTools" "mcp_CalendarTools"

# 3. Package for upload
a365 publish
```

Upload the generated `manifest.zip` in the Microsoft 365 admin center under
**Agents → Upload custom agent**, then approve the instance creation request.

Do not hand-edit `ToolingManifest.json`. The CLI owns it, and the catalog changes
over time.

Enable telemetry when deploying the registered Blueprint; instance approval is
not required for configuration:

```dotenv
ENABLE_A365_OBSERVABILITY=true
ENABLE_A365_OBSERVABILITY_EXPORTER=true
```

Both flags are required. The first registers instrumentation; the second enables
the exporter. Correctly attributed export begins on the first real post-instance
M365 turn, which supplies the runtime agent ID and delegated exporter token.

---

## Deploying

One script provisions everything and deploys. It is idempotent — re-run it to ship
a new build.

```bash
export AZURE_OPENAI_ENDPOINT="https://<your-aoai-resource>.openai.azure.com/"
export AZURE_OPENAI_DEPLOYMENT="<your-deployment-name>"
export AZURE_OPENAI_ACCOUNT_ID="$(az cognitiveservices account show \
  -g <aoai-rg> -n <aoai-name> --query id -o tsv)"

export AGENT_NAME=langchainoa
export LOCATION=<your-region>

bash infra/deploy-azure.sh
```

It creates the resource group, registry, Log Analytics workspace, Container Apps
environment, and container app with a system-assigned identity, then assigns
**Cognitive Services OpenAI User** on the model account and **Reader** on the
subscription. It prints the `messagingEndpoint` to paste into `a365.config.json`.

If your app region does not offer ACR Tasks, point the registry elsewhere — the
two regions do not have to match:

```bash
export ACR_LOCATION=southeastasia
```

Every run tags the image with a timestamp. Updating an unchanged `:latest` tag
does not create a new revision, which is why a unique tag matters.

Tighten `Reader` to a narrower scope, and add **Monitoring Reader** or
**Log Analytics Reader** only where the workload actually needs them.

---

## Reusing this agent in another tenant

Nothing tenant-specific is committed. `a365.config.json`, `.env`, and
`a365.generated.config.json` are all generated locally and excluded from git and
from the container image.

### Packaging it to hand over

`.gitignore` covers git and `.dockerignore` covers the image, but neither applies
to a plain copy or zip. Build the archive explicitly:

```bash
infra/package-share.sh ../langchain-oa.tgz
```

Verify before sending:

```bash
tar -tzf ../langchain-oa.tgz          # expect templates only, no .env
```

### Standing it up on the other side

```bash
# 1. Infrastructure — prints the messaging endpoint when it finishes
bash infra/deploy-azure.sh

# 2. Agent 365 configuration
cp a365.config.template.json a365.config.json
#    fill in: clientAppId, subscriptionId, tenantId, managerEmail,
#             agentUserPrincipalName, messagingEndpoint

# 3. Register, then package
a365 setup all --agent-name <your-agent-name> --aiteammate --m365
a365 publish
```

Then upload `manifest/manifest.zip` in the Microsoft 365 admin center, approve the
instance request, and assign a license.

One per-tenant rule: `agentBlueprintDisplayName` must be unique within the tenant
and 30 characters or fewer. The CLI resolves blueprints by display name, so a
duplicate silently binds to the wrong blueprint.

## Known caveats

| Caveat | Detail |
|---|---|
| Blueprint secret is stored in plaintext on macOS and Linux | The CLI reports `DPAPI encryption not available on this platform` and writes the secret to `a365.generated.config.json`. Rotate it if it is printed or shared, and prefer a managed identity federated to the blueprint. |
| Default blueprint permissions exceed what this agent uses | `a365 setup all` grants a broad Graph scope set including `Mail.Send` and `Files.ReadWrite.All`. This agent is read-only and needs almost none of them. Trim after setup. |
| Conversation history is in-process | Pinned to one replica. Move it to a shared store before scaling out. |
| Work IQ needs delegated per-audience tokens | Before an instance exists, or without the matching local `BEARER_TOKEN_MCP_*`, those tools return a clear error and the rest of the agent still works. |
| ACR Tasks regional gaps | Set `ACR_LOCATION` to a supported region if `az acr build` fails with `NoRegisteredProviderFound`. |

## Security notes

- No model API key exists in this agent by design.
- The current Python Activity host uses the Blueprint service-connection secret
  written by `a365 setup all`; store it as a platform secret and rotate it if exposed.
- Keep `ENABLE_LOCAL_EVAL=false` outside a developer machine.
- Keep `ENABLE_A365_SENSITIVE_DATA=false` unless prompt and response content
  capture has been approved.
- Never commit `.env`, `a365.config.json`, or `a365.generated.config.json`.
- Tools never receive raw tokens as model-visible arguments. Credentials are held
  in request-scoped context, so a prompt cannot exfiltrate them.
- Work IQ tool names are filtered by a read-only allowlist before invocation.
  Anything resembling a write is rejected before the call is made.

---

## Optional extensions

Mail actions are deliberately excluded to keep this baseline read-only.

| Extension | How |
|---|---|
| Mail actions | Add prepare-and-confirm tools that require an explicit confirmation code from the same requester, and store pending actions outside process memory |

Conversation history is in-process. Move it to a shared store before running
more than one replica.
