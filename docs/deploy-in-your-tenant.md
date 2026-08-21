# Deploy in your tenant

End to end, from a clone to an AI Teammate answering in Microsoft 365. Nothing
in this guide requires editing application code — only configuration.

---

## What you need first

| Requirement | Notes |
|---|---|
| Tenant onboarded to Agent 365 | With available Agent 365 licences |
| Azure subscription | Ideally the same tenant, to avoid cross-tenant friction |
| `a365` CLI | Signed in as an admin |
| Azure CLI | `az login` |
| Azure OpenAI resource | With a chat deployment |
| Roles | Agent ID Administrator or Developer for build/deploy; Agent Registry Administrator for registration; an admin role that can grant consent |

The signed-in identity also needs **Cognitive Services OpenAI User** on the
Azure OpenAI account, because this agent never uses an API key.

If the tenant has not been onboarded to Agent 365 before — licences, Entra
roles, CLI install, client app registration and consent — do
[Prerequisites and tenant onboarding](prerequisites.md) first.

---

## Values you must change

Everything below is environment-specific. The verified `ClientSecret` path
needs variable changes only; changing framework, model-provider shape, or
Blueprint authentication mode is a separate migration.

| Value | Where | Notes |
|---|---|---|
| Agent name | `AGENT_NAME` | Drives resource names |
| Tenant, subscription | `a365.config.json` | Copy from `a365.config.template.json` |
| CLI client app id | `a365.config.json` | Your Agent 365 CLI app registration |
| Owner UPN | `a365.config.json` | Becomes the agent's manager |
| Blueprint display name | `a365.config.json` | **Must be unique in the tenant**, 30 characters or fewer |
| Azure OpenAI endpoint + deployment | deploy env vars | |
| Region | `LOCATION` | |
| Registry region | `ACR_LOCATION` | ACR Tasks is not available in every region |
| Messaging endpoint | `a365.config.json` | Only known after the first deploy |

> The Blueprint display name is the one people trip over. The `a365` CLI keys
> global state on it, so a duplicate name resolves to the wrong endpoint and
> configuration.

---

## Step 1 — Infrastructure

```bash
export AGENT_NAME=<your-agent-name>
export LOCATION=<azure-region>
export ACR_LOCATION=<region-with-acr-tasks>
export AZURE_OPENAI_ENDPOINT=https://<resource>.openai.azure.com/
export AZURE_OPENAI_DEPLOYMENT=<deployment-name>
export AZURE_OPENAI_ACCOUNT_ID=$(az cognitiveservices account show \
  -n <resource> -g <rg> --query id -o tsv)

./infra/deploy-azure.sh
```

The script is idempotent. It creates the resource group, registry, Log Analytics
workspace, Container Apps environment, and app; assigns **Cognitive Services
OpenAI User** and **Reader** to the app's managed identity; and prints the
messaging endpoint.

Each build gets a unique image tag. Reusing `:latest` does **not** create a new
revision even when the image changed.

---

## Step 2 — Agent 365 registration

```bash
cp a365.config.template.json a365.config.json
# fill in: clientAppId, subscriptionId, tenantId, managerEmail,
#          agentUserPrincipalName, messagingEndpoint, blueprint display name

a365 setup all --verbose
```

This creates the Blueprint, configures its permissions, and writes the Activity
Protocol connection settings. The child Agent Identity and agent user are
created later, when the AI Teammate instance is approved and provisioned.

> On macOS the generated config may contain the Blueprint secret in plain text.
> Never commit it, and rotate it if it is printed. See the auth-mode section of
> the README for the keyless alternative.

Then choose the Work IQ servers available in **your** tenant, and grant them:

```bash
a365 develop add-mcp-servers      # regenerates ToolingManifest.json
a365 setup permissions mcp
```

Do not reuse another tenant's `ToolingManifest.json`. Server audiences and
availability differ.

---

## Step 3 — Publish and create the instance

```bash
a365 publish                       # produces manifest/manifest.zip
```

Upload `manifest.zip` in the Microsoft 365 admin centre under **Agents →
Upload custom agent**. Then create the instance and assign an Agent 365 licence
to the agent user.

Instance creation requires admin approval, so expect a request-and-approve step
rather than immediate provisioning.

---

## Step 4 — Wire the settings into the app

```bash
./infra/sync-a365-settings.sh
```

This reads the generated configuration and pushes the Activity Protocol,
agentic auth, Blueprint, and observability values into the Container App,
storing the Blueprint credential as a secret reference rather than a literal.
This script implements the verified `ClientSecret` path. Federated credentials
require the Agents SDK 1.x line plus different deployment plumbing; they are not
enabled by changing `AUTHTYPE` alone.

---

## Step 5 — Verify

```bash
curl -s https://<your-app>/api/health | jq
```

Expect `workiq: true` and `observability: export`.

Then confirm the endpoint is actually protected:

```bash
curl -s -o /dev/null -w "%{http_code}\n" -X POST \
  -H "Content-Type: application/json" -d '{"type":"message"}' \
  https://<your-app>/api/messages
```

`401` is correct. Anything else means the Blueprint credentials did not load and
the agent is running without authentication.

Finally, send a message from Microsoft 365 and confirm a reply.

---

## Step 6 — Purview (optional)

Only do this if you want prompt and response **content** captured. Observability
alone records that the agent ran, never what was said.

**a. Turn it on**

```bash
ENABLE_PURVIEW=true PURVIEW_ENFORCE_BLOCKS=false ./infra/deploy-azure.sh
```

**b. Grant the Graph scopes**

Append to the Agent Identity and Blueprint consent grants:

```text
Content.Process.User
ProtectionScopes.Compute.User
ContentActivity.Write
```

If the Blueprint's inheritable Graph permission is `allAllowed`, no inheritable
permission change is needed.

**c. Find the location the runtime reports**

Send one message, then read the log line:

```text
Purview operation=protectionScopes.compute status=200
  activities=['downloadText','uploadText'] locations=['<GUID>']
```

**d. Create the collection policy against that GUID**

```powershell
Connect-IPPSSession

New-FeatureConfiguration -FeatureScenario KnowYourData `
  -Name "DSPM for AI - Capture <agent> AgentIdentity" -Mode Enable `
  -ScenarioConfig '{"Activities":["UploadText","DownloadText"],"EnforcementPlanes":["Application"],"SensitiveTypeIds":["All"],"IsIngestionEnabled":true}' `
  -Locations '[{"Workload":"Applications","Location":"<GUID>","LocationSource":"Entra","LocationType":"Individual","Inclusions":[{"Type":"Tenant","Identity":"All"}]}]'
```

Four things that decide whether this works:

- Scope to the GUID from the log, **not** the Blueprint. This is the single most
  common reason capture and blocking silently do nothing.
- `IsIngestionEnabled: true`, or you get policy matches with no text.
- Manage these policies in PowerShell. The portal shows an individual GUID scope
  as "Not scoped yet", and re-saving there wipes the scope.
- To see the text, the agent user needs an Exchange Online mailbox and the
  viewer needs **Content Explorer Content Viewer**.

Blocking is a separate DLP policy, and only `UploadText` (the prompt) can be
blocked. Responses are capture-only. Set `PURVIEW_ENFORCE_BLOCKS=true` once the
policy exists.

---

## Cleanup

```bash
az group delete --name rg-agent365-<agent>-<location>
```

Then remove the agent instance, agent user, and Blueprint in Agent 365, and
delete any Purview policies you created for it.
