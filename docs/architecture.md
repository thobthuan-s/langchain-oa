# Architecture

How LangchainOA is put together, and why each piece is there. Read this before
changing the turn pipeline or the identity plumbing.

---

## 1. Components

LangchainOA is an Agent 365 **AI Teammate**: it has its own directory identity
and is addressed like a colleague, rather than being an app a user launches.

```mermaid
flowchart LR
    U[User in Microsoft 365] --> M[Agent 365 / Activity Protocol]
    M --> H["host.py<br/>aiohttp on Container Apps"]

    H --> L["langchain_agent.py<br/>LangGraph agent"]
    L --> AOAI[Azure OpenAI<br/>keyless]

    L --> WT["tools/workiq_tools.py"]
    L --> AT["tools/azure_tools.py"]
    WT --> WIQ[Work IQ MCP<br/>SharePoint / Mail / Calendar]
    AT --> ARM[Azure Resource Manager<br/>Azure Monitor]

    H -. spans .-> OBS[Agent 365 Observability]
    H -. content .-> PV[Microsoft Purview]

    OBS --> DEF[Defender / Purview audit]
```

Two things are deliberately separate:

- **Observability** answers *what did the agent do*. Always on in production.
- **Purview** answers *what content passed through*. Optional, and a different
  Graph API entirely.

---

## 2. Startup

Order matters in exactly one place: observability must be configured **before**
LangChain is imported, or the instrumentation cannot patch it.

```mermaid
sequenceDiagram
    participant P as main.py
    participant O as observability.py
    participant H as host.py
    participant SDK as Agents SDK

    P->>O: configure_observability()
    Note over O: Distro patches LangChain<br/>before first import
    P->>H: LangchainOaHost()
    H->>SDK: MsalConnectionManager, CloudAdapter, Authorization
    H->>H: register message / conversationUpdate handlers
    H->>H: build auth config, choose middleware
    H-->>P: serve /api/messages, /api/health
```

`host.py` imports `run_agent` **inside** the handler, not at module scope, to
preserve that ordering.

---

## 3. One turn

Every token is minted per turn from the incoming activity. Nothing long-lived is
stored.

```mermaid
sequenceDiagram
    participant M as Microsoft 365
    participant H as host.py
    participant PV as Purview
    participant L as LangChain
    participant W as Work IQ MCP
    participant A as Azure OpenAI

    M->>H: message activity
    H->>H: exchange observability token
    H->>H: exchange Work IQ tokens (one per server)
    H->>H: exchange Purview Graph token

    H->>PV: processContent(uploadText) — the prompt
    alt prompt blocked and enforcement on
        PV-->>M: policy message, turn ends
    else allowed
        H->>L: run inside baggage scope
        L->>A: chat completion
        L->>W: read-only tool calls
        L-->>H: response
        H->>PV: processContent(downloadText) — the response
        H-->>M: reply
    end
```

If Purview is disabled or unavailable, those steps are skipped and the turn
proceeds. Purview failures never break chat.

---

## 4. Identity

Three distinct objects. Confusing them is the most common cause of silent
failure.

```mermaid
flowchart TD
    BP["Agent Blueprint<br/>the approved template"]
    AI["Agent Identity<br/>the deployed instance"]
    AU["Agent User<br/>directory account, licensed"]

    BP -->|parent of| AI
    AI -->|parent of| AU

    AI -->|OBO per audience| G[Microsoft Graph]
    AI --> S[SharePoint MCP]
    AI --> ML[Mail MCP]
    AI --> C[Calendar MCP]
    AI --> OB[Observability API]
```

Rules that are easy to get wrong:

- Runtime identity comes from the **activity**, never from config. `host.py`
  reads `recipient.agenticAppId` / `get_agentic_instance_id()`.
- Work IQ V2 servers each have a **different OAuth audience**. One token cannot
  be reused across SharePoint, Mail, and Calendar.
- Purview policies must be scoped to the **Agent Identity**, not the Blueprint.
- The Blueprint ID belongs in `microsoft.a365.agent.blueprint.id`. It must not
  become the runtime `gen_ai.agent.id`.

---

## 5. Telemetry

The Distro's LangChain instrumentation emits the spans Agent 365 expects. This
agent adds identity through baggage rather than creating its own root span.

```mermaid
flowchart TD
    B["Baggage scope<br/>tenant, agent, blueprint,<br/>agentic user, conversation"]
    B --> IA["invoke_agent<br/>the turn"]
    IA --> CH["chat<br/>model call"]
    IA --> ET["execute_tool<br/>tool call"]
```

The exporter drops any span that is missing tenant or agent identity, or whose
`gen_ai.operation.name` is not one of `invoke_agent`, `chat`, `execute_tool`,
`output_messages`, `apply_guardrail`.

Two deliberate choices:

- **No manual `InvokeAgentScope`.** LangChain already emits `invoke_agent`.
  Adding one produced two roots per turn and double-counted sessions.
- **OpenAI instrumentation disabled.** Its wrapper indexes
  `gen_ai.request.model`, which `AzureChatOpenAI` does not set, raising
  `KeyError` before the model was ever called. LangChain still emits the `chat`
  span, so nothing is lost.

---

## 6. Routes and authentication

```mermaid
flowchart LR
    R1["POST /api/messages"] --> J{JWT middleware}
    J -->|valid| T[Turn pipeline]
    J -->|invalid| E[401]

    R2["GET /api/health"] --> OK[200, anonymous]
    R3["POST /eval/invoke"] --> D{ENABLE_LOCAL_EVAL}
    D -->|true| T
    D -->|false| NF[404, not registered]
```

| Route | Auth | Notes |
|---|---|---|
| `POST /api/messages` | Bot Framework JWT | Production entry point |
| `GET /api/health` | Anonymous | Liveness and feature flags |
| `POST /eval/invoke` | None | Local only. Never enable in production |

If the Blueprint credentials are incomplete the host logs a warning and runs
without JWT middleware — intended for local development only. In production,
confirm an unauthenticated `POST /api/messages` returns `401`.

---

## 7. Credentials

Every Azure call in this agent is keyless. There is no API key for the model and
no connection string for the tools — only Entra tokens plus RBAC.

The sample uses `DefaultAzureCredential` for both the model and the Azure tools:

```python
azure_ad_token_provider=get_bearer_token_provider(
    DefaultAzureCredential(),
    "https://cognitiveservices.azure.com/.default",
)
```

It tries a chain of sources and uses the first that works:

```mermaid
flowchart LR
    E[Environment vars] --> W[Workload identity]
    W --> M[Managed identity]
  M --> D[Developer credentials<br/>VS Code / az / PowerShell / azd]
```

Exact developer credentials depend on the installed Azure Identity version.
Interactive browser authentication is not part of the default chain unless it
is explicitly enabled.

That is why the same code runs locally under `az login` and in Container Apps
under the managed identity, with no branching. For a sample, that is the point.

### Where it bites in production

| Risk | Consequence |
|---|---|
| More than one identity on the host | It may select the wrong one |
| Probing failed sources | Slower startup, notably IMDS |
| Succeeds as your developer account locally | Hides a missing role assignment until production |

### Alternatives

| Credential | Use for |
|---|---|
| `ManagedIdentityCredential(client_id=...)` | Production in Azure. Explicit and fastest |
| `WorkloadIdentityCredential` | AKS federated identity |
| `ChainedTokenCredential(...)` | Deterministic local plus cloud |
| `AzureCliCredential` | Local development only |
| `ClientSecretCredential` / `CertificateCredential` | An app registration acting as itself |
| `EnvironmentCredential` | CI and CD |

You can also keep `DefaultAzureCredential` and simply remove the ambiguity:

```python
DefaultAzureCredential(
    managed_identity_client_id=os.environ["UAMI_CLIENT_ID"],
    exclude_interactive_browser_credential=True,
    exclude_shared_token_cache_credential=True,
)
```

**Recommendation.** Keep the default for samples and development. In production,
pin the identity — either pass `managed_identity_client_id` or use
`ManagedIdentityCredential` directly — so identity selection is never inferred.
This is the same principle behind preferring federated credentials over a client
secret for the Blueprint: explicit identity beats implicit.

Roles the chosen identity needs:

| Scope | Role |
|---|---|
| Azure OpenAI account | Cognitive Services OpenAI User |
| Subscription | Reader in the sample |
| Narrower production alternative | Reader on selected resource groups plus Log Analytics Reader on selected workspaces |

---

## 8. Why this shape

| Decision | Reason |
|---|---|
| Microsoft 365 Agents SDK for hosting | Activity Protocol, agentic auth, and channel plumbing are provided |
| LangChain for orchestration | Swappable; the Agent 365 layer does not depend on it |
| Keyless Azure OpenAI | No API key to store or rotate |
| Read-only tools | Safe to demo against real tenant data |
| Baggage over manual scopes | One root span per turn, richer attributes, less code |
| Purview off by default | It needs tenant policy work; the agent must run without it |
