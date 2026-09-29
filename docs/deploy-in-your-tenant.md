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

Everything below is environment-specific. Deploying needs variable changes
only; switching the Blueprint to keyless `FederatedCredentials` is one script
(Step 4). Changing framework or model-provider shape is a separate migration.

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

The validated command for this agent:

```bash
a365 setup all --agent-name <your-agent-name> --aiteammate --m365 --verbose
```

| Flag | Effect |
|---|---|
| `--agent-name` | Runs config-free. Derives blueprint and identity display names, auto-detects the tenant from `az account show`, resolves the client app by looking up `Agent 365 CLI` |
| `--aiteammate` | Provisions blueprint and permissions for an agent that gets its own user account. Overrides the `aiTeammate` field in a config file |
| `--m365` | Registers the messaging endpoint through the MCP platform. Opt-in; omit for non-M365 agents |

Use a config file instead of `--agent-name` when you need values the flags cannot
derive — agent user principal name, manager, usage location, or a specific
blueprint display name:

```bash
cp a365.config.template.json a365.config.json
# fill in: clientAppId, subscriptionId, tenantId, managerEmail,
#          agentUserPrincipalName, messagingEndpoint, blueprint display name
a365 setup all --verbose
```

This creates the Blueprint, configures its permissions, and writes the Activity
Protocol connection settings. The child Agent Identity and agent user are
created later, when the AI Teammate instance is approved and provisioned.

> **Consent during `a365 setup all`.** This step grants the Blueprint its own
> permissions — Microsoft Graph, Work IQ, Agent 365 Observability, and the
> messaging/Activity Protocol scopes — which is separate from the one-time CLI
> client app consent in
> [Prerequisites and tenant onboarding](prerequisites.md). Expect one or more
> admin-consent prompts. If you are not a Global Administrator, `a365 setup all`
> still completes and prints the consent URLs for an admin to open; there is no
> separate CLI command for that step.
> ([Quickstart: Connect an existing agent to Agent 365](https://learn.microsoft.com/microsoft-agent-365/developer/get-started))

> On macOS the generated config may contain the Blueprint secret in plain text.
> Never commit it or show it on screen, and rotate it if it is printed. Step 4
> replaces it with a keyless federated credential and deletes it.

Then choose the Work IQ servers available in **your** tenant, and grant them:

```bash
a365 develop add-mcp-servers      # regenerates ToolingManifest.json
a365 setup permissions mcp
```

Do not reuse another tenant's `ToolingManifest.json`. Server availability and
metadata differ per tenant even though the underlying Microsoft service IDs are
constant.

---

## Step 3 — Publish, upload, and create the instance

```bash
a365 publish                       # produces manifest/manifest.zip
```

`a365 publish` updates `manifest.json` with the Blueprint ID, packages it with
the icons into `manifest.zip`, and prints upload instructions.
([Publish agent to Microsoft admin center](https://learn.microsoft.com/microsoft-agent-365/developer/publish))

**Upload** — requires Global Administrator:

1. Go to [admin.microsoft.com](https://admin.microsoft.com)
2. **Agents → All agents**
3. **Upload custom agent**
4. Upload `manifest/manifest.zip`

Allow 5–10 minutes for the agent to appear in the admin centre and in Teams.

**Create the instance** — this is a separate step from upload, and it is a
request-and-approve flow, not immediate provisioning: a user requests an
instance from Teams, the request routes to the tenant admin, and the admin
approves it from **Microsoft 365 admin centre → Agents → Requests**. Only after
approval does Teams create the agent instance and the agent user.
([Create agent instance](https://learn.microsoft.com/microsoft-agent-365/developer/create-instance))

Then assign an Agent 365 licence to the new agent user.

---

## Step 4 — Wire the settings into the app

```bash
./infra/sync-a365-settings.sh
```

This reads the generated configuration and pushes the Activity Protocol,
agentic auth, Blueprint, and observability values into the Container App,
storing the Blueprint credential as a secret reference rather than a literal.

Then remove the secret from the deployment entirely:

```bash
LOCATION=<region> RESOURCE_GROUP=<rg> APP_NAME=<app> ./infra/enable-federated-credentials.sh
```

This creates a user-assigned managed identity, attaches it to the Container App,
adds a federated identity credential on the Blueprint app that trusts it, and
switches the app to `AUTHTYPE=FederatedCredentials` with no stored secret. Send
the agent a Teams message to confirm it replies, then delete the Blueprint
secret and clear it from `.env` and `a365.generated.config.json`:

```bash
az ad app credential list --id <blueprint-app-id>
az ad app credential delete --id <blueprint-app-id> --key-id <key-id>
```

Set `CONNECTIONS__SERVICE_CONNECTION__SETTINGS__AUTHTYPE=FederatedCredentials`
and `...__FEDERATEDCLIENTID=<identity client id>` in `.env` so later runs of
`sync-a365-settings.sh` keep the app keyless.

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

> **Group-scoped policies may already cover this.** Some tenants have a
> tenant-wide capture policy — for example Microsoft's built-in "Capture
> interactions for enterprise AI apps" — that evaluates every agent without a
> per-agent policy. Confirm from the runtime log in step **c** below before
> assuming you need step **d**: if `protectionScopes.compute` returns a
> non-empty `executionMode` for your agent's location, a policy is already
> applying, and you may only need to verify the ingestion result in Activity
> Explorer rather than create a new policy. Only continue to step **d** when no
> policy applies, or when the applying policy does not have ingestion enabled.

**a. Turn it on**

```bash
ENABLE_PURVIEW=true PURVIEW_ENFORCE_BLOCKS=false ./infra/deploy-azure.sh
```

**b. Grant the Graph scopes**

Purview needs three delegated Microsoft Graph scopes that `a365 setup all` does
not grant:

```text
Content.Process.User
ProtectionScopes.Compute.User
ContentActivity.Write
```

> **You cannot do this in the portal for the Agent Identity.** The Blueprint is
> an app registration plus a service principal, so it has an **API permissions**
> blade. The Agent Identity is a **service principal only** — there is no app
> registration behind it, so it has no such blade. Grant its scopes through
> Microsoft Graph. Both objects appear under **Enterprise applications**, but
> only the Blueprint can be edited there.

First check whether the Blueprint already allows Graph scopes to flow to its
children. For an agent identity blueprint the application object ID equals its
app ID:

```bash
BLUEPRINT_APP_ID=<from a365.generated.config.json: agentBlueprintId>

az rest --method GET \
  --url "https://graph.microsoft.com/beta/applications/$BLUEPRINT_APP_ID/microsoft.graph.agentIdentityBlueprint/inheritablePermissions" \
  --query "value[?resourceAppId=='00000003-0000-0000-c000-000000000000'].inheritableScopes.kind" -o tsv
```

`allAllowed` means no inheritable-permission change is needed; anything else and
you must add these scopes to the inheritable set first.

Then append the scopes to the existing Graph consent grant. Do this for the
Blueprint service principal, and for the Agent Identity if it holds its own
grant:

```bash
GRAPH_SP=$(az ad sp show --id 00000003-0000-0000-c000-000000000000 --query id -o tsv)
CLIENT_SP=<Blueprint service principal object ID, or Agent Identity object ID>

GRANT_ID=$(az rest --method GET \
  --url "https://graph.microsoft.com/v1.0/oauth2PermissionGrants?\$filter=clientId eq '$CLIENT_SP'" \
  --query "value[?resourceId=='$GRAPH_SP'].id | [0]" -o tsv)

CURRENT=$(az rest --method GET \
  --url "https://graph.microsoft.com/v1.0/oauth2PermissionGrants/$GRANT_ID" \
  --query scope -o tsv)

NEW=$(python3 -c "import sys;c=sys.argv[1].split();a=['Content.Process.User','ProtectionScopes.Compute.User','ContentActivity.Write'];print(' '.join(c+[s for s in a if s not in c]))" "$CURRENT")

az rest --method PATCH \
  --url "https://graph.microsoft.com/v1.0/oauth2PermissionGrants/$GRANT_ID" \
  --headers "Content-Type=application/json" \
  --body "{\"scope\":\"$NEW\"}"
```

Appending rather than replacing matters — overwriting the `scope` string drops
the permissions the agent already depends on.

Verify:

```bash
az rest --method GET \
  --url "https://graph.microsoft.com/v1.0/oauth2PermissionGrants/$GRANT_ID" \
  --query scope -o tsv | tr ' ' '\n' | grep -E 'Content.Process.User|ProtectionScopes.Compute.User|ContentActivity.Write'
```

These are admin-consented delegated scopes, so you need an admin role that can
grant consent. Note that inherited permissions are **not** visible on the Agent
Identity in the portal or through `oauth2PermissionGrants` — they only appear in
the runtime token, which is why step **c** reads the log rather than the portal.

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
