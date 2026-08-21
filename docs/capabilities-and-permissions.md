# Capabilities and permissions

What this agent can do, exactly which permissions make that possible, and where
to inspect or change them.

---

## What this agent is

LangchainOA is a Microsoft Agent 365 **AI Teammate**. It has its own directory
identity and licence, appears in Microsoft 365 like a colleague, and is
addressed by mentioning it rather than by launching an app.

It is a **read-only operations and knowledge assistant**. It can find and
summarise governed Microsoft 365 content, and report on Azure resources and
monitoring data. It ships with no tool that sends mail, changes a calendar,
edits a document, or modifies an Azure resource.

Three properties are deliberate:

- **Read-only by construction.** Write capability is absent from the tool set,
  not merely discouraged in the prompt.
- **Governed data access.** Microsoft 365 content is reached through Work IQ
  MCP, so tenant policy and auditing apply.
- **Observable and governable.** Every turn emits Agent 365 telemetry, and
  content can optionally be evaluated by Microsoft Purview.

---

## Tools

| Tool | Does |
|---|---|
| `search_sharepoint` | Search SharePoint documents and folders |
| `list_workiq_tools` | List the read-only tools a Work IQ server offers |
| `call_readonly_workiq_tool` | Call one discovered read-only Work IQ tool |
| `list_resource_groups` | List Azure resource groups |
| `list_resources` | List resources, optionally filtered |
| `get_resource` | Read one resource's details |
| `query_metrics` | Query Azure Monitor metrics |
| `query_logs` | Run a bounded Log Analytics KQL query |

Work IQ tool catalogues change over time, so the agent discovers tools at
runtime rather than hard-coding names. Discovered tools are filtered by a
read-only policy before the model may call them:

```python
_ALLOWED_PREFIXES = ("get", "list", "find", "read", "search", "query",
                     "check", "browse")
_BLOCKED_TERMS    = ("add", "cancel", "create", "delete", "forward", "grant",
                     "invite", "move", "remove", "rename", "reply", "send",
                     "set", "share", "update", "upload", "write")
```

A tool such as `sendMail` is rejected by that filter even if the tenant exposes
it and the model asks for it.

---

## Permission planes

Permissions come from four independent places. A gap in any one produces a
different failure.

```mermaid
flowchart TD
    AI[Agent Identity]
    AI -->|delegated OBO| G[Microsoft Graph]
    AI -->|per-audience| W[Work IQ MCP servers]
    AI -->|app role| O[Agent 365 Observability]
    AI -->|app role| B[Messaging / Activity Protocol]

    MI[Container App managed identity] -->|Azure RBAC| AZ[Azure OpenAI, ARM, Monitor]
```

### 1. Microsoft Graph — delegated, via the Agent Identity

Granted by `a365 setup all`, and used for directory context and Purview.

| Scope | Needed for |
|---|---|
| `User.Read.All` | Resolve people referenced in a request |
| `Sites.Read.All` | SharePoint content |
| `Chat.ReadWrite`, `ChannelMessage.Read.All`, `ChannelMessage.Send` | Teams conversation |
| `Mail.ReadWrite`, `Mail.Send`, `Files.ReadWrite.All` | Granted by the default template |
| `Content.Process.User`, `ProtectionScopes.Compute.User`, `ContentActivity.Write` | Purview only |

> **Least privilege.** The default template grants write scopes — `Mail.Send`,
> `Mail.ReadWrite`, `Files.ReadWrite.All` — that this agent never uses. The code
> cannot write, but the *identity* is permitted to. Trim them for anything
> beyond a demo. See below for how.

### 2. Work IQ MCP — one audience per server

Each V2 server is a separate OAuth audience. A token minted for one is rejected
by the others, so the agent exchanges one token per server, per turn.

| Server | Scope |
|---|---|
| `mcp_SharePointRemoteServer` | `Tools.ListInvoke.All` |
| `mcp_MailTools` | `Tools.ListInvoke.All` |
| `mcp_CalendarTools` | `Tools.ListInvoke.All` |

Audiences live in `ToolingManifest.json` and are read at runtime, never
hard-coded. Regenerate that file per tenant.

### 3. Agent 365 platform

| Permission | Needed for |
|---|---|
| `Agent365.Observability.OtelWrite` | Export spans |
| `McpServersMetadata.Read.All` | Discover Work IQ servers |
| `AgentData.ReadWrite` | Activity Protocol replies |
| `Connectivity.Connections.Read` | Power Platform connectivity |

### 4. Azure RBAC — the Container App's managed identity

Separate from Entra permissions. Assigned by `infra/deploy-azure.sh`.

| Scope | Role |
|---|---|
| Azure OpenAI account | Cognitive Services OpenAI User |
| Subscription or resource groups | Reader |
| Log Analytics workspace | Log Analytics Reader |

---

## Where to look

**Microsoft 365 admin centre** — the agent, its owner, and licence:
`Agents → your agent`

**Entra admin centre** — identity and consent:

- `Applications → App registrations` → your Blueprint → **API permissions**
- `Applications → Enterprise applications` → the Agent Identity → **Permissions**
- `Identity → Users` → the agent user, shown with *Is Agent = Yes*

**Azure portal** — RBAC: the Container App's **Identity** blade, then
**Azure role assignments**.

**Command line** — what is actually granted to the Agent Identity:

```bash
az rest --method GET \
  --url "https://graph.microsoft.com/v1.0/oauth2PermissionGrants?\$filter=clientId eq '<AGENT_IDENTITY_APP_ID>'" \
  --query 'value[].{resourceId:resourceId,scope:scope}' -o json
```

Azure roles held by the app's managed identity:

```bash
az role assignment list --assignee-object-id <PRINCIPAL_ID> \
  --all --query '[].{role:roleDefinitionName,scope:scope}' -o table
```

Runtime is the most reliable source. The logs show each exchange as it happens:

```text
Retrieving agentic user token for scopes: [...]
```

---

## Where to add or change

| Change | How |
|---|---|
| Work IQ servers | `a365 develop add-mcp-servers`, then `a365 setup permissions mcp` |
| Graph scopes | Add to the Blueprint's API permissions and consent, then to the Agent Identity grant |
| Observability | `a365 setup permissions bot`, or add `Agent365.Observability.OtelWrite` on the Blueprint |
| Azure roles | `az role assignment create` against the app's managed identity |
| Remove a capability | Delete the tool. The prompt alone is not a control |

Adding a scope is two steps, and missing the second is a common failure: the
Blueprint must permit it (inheritable permissions), **and** the grant must exist
on the identity that requests the token.

To trim the unused write scopes, patch the grant to the set this agent actually
needs:

```text
User.Read.All  Sites.Read.All  Chat.ReadWrite
ChannelMessage.Read.All  ChannelMessage.Send
Content.Process.User  ProtectionScopes.Compute.User  ContentActivity.Write
```

Re-running `a365 setup all` reapplies the default template, so re-trim after any
CLI re-run.

---

## Adding a capability safely

1. Add the tool, keeping the read-only naming convention so the filter accepts it
2. Grant only the scope that tool requires
3. State the capability in `agent-prompt.txt` so the model knows it exists
4. Confirm the token exchange succeeds in the logs
5. If the tool returns content, confirm Purview still evaluates the turn

If a capability must write, treat it as a different agent shape: add explicit
confirmation, put the write behind a separate approval step, and re-check the
Purview and observability story before enabling it.
