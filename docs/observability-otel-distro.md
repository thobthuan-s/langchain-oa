# Agent 365 observability with the Microsoft OpenTelemetry Distro

Guidelines for instrumenting an Agent 365 agent with the Microsoft OpenTelemetry
Distro (Python), how each one is implemented in this repository, and what the
Agent 365 logs can and can't link end to end.

Status legend: ✅ implemented · ⚠️ partial or constrained · ❌ not implemented.

## Sources

Public sources only:

| Tag | Source |
|---|---|
| **[DISTRO]** | [Microsoft OpenTelemetry Distro](https://learn.microsoft.com/microsoft-agent-365/developer/microsoft-opentelemetry) (Microsoft Learn) |
| **[DISTRO-GH]** | [Agent 365 Observability guide](https://github.com/microsoft/opentelemetry-distro-python/blob/main/A365_DOCUMENTATION.md) in `microsoft/opentelemetry-distro-python` |
| **[ATTR]** | [Agent 365 observability attribute reference](https://learn.microsoft.com/microsoft-agent-365/developer/observability-attribute-reference) |
| **[CONCEPTS]** | [Agent 365 observability concepts](https://learn.microsoft.com/microsoft-agent-365/developer/observability-concepts) |
| **[OTEL]** | [Direct OTel integration: message contracts](https://learn.microsoft.com/microsoft-agent-365/developer/direct-open-telemetry-integration#message-contracts) |
| **[SRC]** | Installed `microsoft-opentelemetry` package source (`microsoft/opentelemetry/a365/...`) |

## Summary

| # | Guideline | Status |
|---|---|---|
| G1 | Initialize the distro once at startup | ✅ |
| G2 | Provide a token resolver and refresh the token on every turn | ✅ |
| G3 | Set baggage: without tenant and agent IDs, spans are dropped | ✅ |
| G4 | Give every run a root `invoke_agent` span | ✅ |
| G5 | Emit one `invoke_agent` per run | ⚠️ |
| G6 | Send every mandatory attribute | ⚠️ |
| G7 | Pick canonical values for conversation, channel, caller, and agent type | ✅ |
| G8 | Propagate W3C trace context | ⚠️ |
| G9 | Keep content capture off unless approved | ✅ |
| G10 | Keep durable delivery on, in a protected directory | ⚠️ |
| G11 | Know which instrumentations A365-only mode turns off | ✅ |
| G12 | Consider the hosting middleware | ❌ |
| G13 | Validate locally before deploying | ✅ |
| G14 | Verify ingestion, not just HTTP 200 | ✅ |

---

## G1. Initialize the distro once at startup ✅

Call `use_microsoft_opentelemetry()` once. `enable_a365` turns on span enrichment;
`a365_enable_observability_exporter` sends spans to Agent 365. [DISTRO-GH]

```python
from microsoft.opentelemetry import use_microsoft_opentelemetry

use_microsoft_opentelemetry(
    enable_a365=True,
    a365_enable_observability_exporter=True,
    a365_token_resolver=get_cached_agentic_token,
    enable_sensitive_data=False,
)
```

**Here:** `configure_observability()` in [observability.py](../observability.py),
controlled by `ENABLE_A365_OBSERVABILITY` and `ENABLE_A365_OBSERVABILITY_EXPORTER`.

## G2. Provide a token resolver and refresh it on every turn ✅

The resolver is a synchronous `(agent_id, tenant_id) -> token` callable. Keep it
side-effect free and refresh the cache in the async request handler with the
observability scope. [DISTRO-GH]

```python
from microsoft.opentelemetry.a365.runtime import get_observability_authentication_scope

token = await authorization.exchange_token(
    context,
    scopes=get_observability_authentication_scope(),
    auth_handler_id="AGENTIC",
)
cache_agentic_token(tenant_id, agent_id, token.token)
```

**Here:** [token_cache.py](../token_cache.py), and `_cache_observability_token()`
in [host.py](../host.py).

**Lesson learned:** refresh the token on **every** route, including
notification routes such as email. Before that fix, email-turn uploads failed
with `400 TenantIdInvalid`.

## G3. Set baggage: without tenant and agent IDs, spans are dropped ✅

"Without `tenant_id` and `agent_id`, the exporter silently drops spans."
[DISTRO-GH] The official helper fills baggage from the `TurnContext`:

```python
from microsoft.opentelemetry.a365.core import BaggageBuilder
from microsoft.opentelemetry.a365.hosting.scope_helpers.populate_baggage import populate

builder = BaggageBuilder()
populate(builder, turn_context)
with builder.build():
    ...
```

**Here:** `observability_context()` in [observability.py](../observability.py)
builds the baggage explicitly. It uses the same conversation source as
`populate()` (see G7) and adds the canonical channel, caller, and client IP
handling that `populate()` doesn't provide.

## G4. Give every run a root `invoke_agent` span ✅

Defender's agent-activity views, the Microsoft 365 admin center, and Purview
"all depend on a valid `invoke_agent` span at the root of the run". Runs without
one are visible only in advanced hunting. [CONCEPTS]

```python
from microsoft.opentelemetry.a365.core import InvokeAgentScope

with InvokeAgentScope.start(request=..., scope_details=..., agent_details=...,
                            caller_details=..., span_details=root_span_details()) as scope:
    ...  # model and tool spans nest under this span
```

**Here:** `_start_invoke_agent()` and `root_span_details()` in
[observability.py](../observability.py).

**Lesson learned:** Agents SDK 1.x wraps each handler in `agents.app.*` spans.
The Agent 365 exporter drops them because they have no `gen_ai.operation.name`,
so an `invoke_agent` parented to them points at a parent that never arrives.
The root is started with an explicit no-parent context and a **link** to the SDK
span instead.

## G5. Emit one `invoke_agent` per run ⚠️

`invoke_agent` is "the 'root' of an agent run" [OTEL], and "the admin center
ingests `invoke_agent` rows only". [CONCEPTS]

**Here:** each turn has two `invoke_agent` spans: our root, plus the LangChain
agent span created by the distro's LangChain instrumentation, which nests under
the root. Whether the admin center counts both hasn't been verified.

**Options:**

- Leave as is. The LangChain span is a child, not a second root.
- Build model and tool spans manually with `InferenceScope` and
  `ExecuteToolScope`, and disable the LangChain instrumentation
  (`instrumentation_options={"langchain": {"enabled": False}}`). This removes the
  duplicate at the cost of writing and maintaining the spans yourself.

Simply dropping the LangChain span isn't an option: its model and tool spans
would then point at a missing parent.

## G6. Send every mandatory attribute ⚠️

Mandatory attributes from [ATTR], and where they come from here:

| Attribute | Applies to | Here |
|---|---|---|
| `gen_ai.operation.name` | All | ✅ Scopes and instrumentation |
| `microsoft.tenant.id`, `gen_ai.agent.id`, `gen_ai.agent.name`, `microsoft.a365.agent.blueprint.id` | All | ✅ Baggage and `AgentDetails` |
| `gen_ai.conversation.id`, `microsoft.channel.name` | All | ✅ Baggage (G7) |
| `client.address` | IA, ET, CH | ✅ Placeholder `0.0.0.0` (G7) |
| `server.address` | IA, ET, CH | ✅ On `invoke_agent` spans |
| `server.port` | IA, ET, CH | ⚠️ `invoke_agent` only (see note) |
| `user.id` | IA | ✅ Entra object ID for Teams; omitted for external email senders (G7) |
| `microsoft.agent.user.id` | IA, ET, CH (AI teammates) | ✅ Agentic user ID |
| `gen_ai.input.messages`, `gen_ai.output.messages` | IA, CH | ⚠️ Only when `ENABLE_A365_SENSITIVE_DATA=true` (G9) |
| `gen_ai.tool.name`, `gen_ai.tool.type`, `gen_ai.tool.call.id` | ET | ✅ LangChain instrumentation |
| `gen_ai.tool.call.arguments`, `gen_ai.tool.call.result` | ET | ⚠️ Only when `ENABLE_A365_SENSITIVE_DATA=true` (G9) |
| `gen_ai.request.model`, `gen_ai.provider.name` | CH | ✅ LangChain instrumentation |

**Note on `server.port`:** [ATTR] marks it mandatory on model and tool spans, but
the distro applies baggage `server.address` and `server.port` to `invoke_agent`
spans only. Spans it classifies by instrumentation scope "receive the common
baggage attributes, but never the `invoke_agent`-only ones (caller agent
details, `server.address`, `server.port`)". [DISTRO-GH] `BaggageBuilder.invoke_agent_server()`
also omits port 443 [SRC], so this repository sets `server.port` explicitly on
the root.

## G7. Pick canonical values ✅

| Field | Guidance | Here |
|---|---|---|
| `gen_ai.conversation.id` | Primary join key for a run [ATTR] | Raw `activity.conversation.id`, the same source as `populate_baggage.get_conversation_pairs()` [SRC]. The Agent 365 gateway rows carry the same value. |
| `microsoft.channel.name` | Canonical values are `msteams` and `outlook` [ATTR] | `msteams`; email notifications (`agents:email`) map to `outlook` |
| `client.address` | Use a stable placeholder such as `"0.0.0.0"` when the caller has no IP [ATTR] | `0.0.0.0`; Teams and email activities carry no end-user IP |
| `user.id` | Microsoft Entra object ID of the human caller [ATTR] | `aadObjectId` only. External senders go in `user.email`. |
| `gen_ai.agent.type` | Omit for Entra-registered agents; the service auto-classifies [ATTR] | Not set. Defender shows `CustomBuiltAgentsUsingSDK`. |
| `gen_ai.execution.type` | Optional: `HumanToAgent`, `Agent2Agent`, `EventToAgent` [ATTR] | `HumanToAgent` for chat, `EventToAgent` for notifications. Not exposed in `CloudAppEvents`. |

**Here:** `telemetry_conversation_id()`, `telemetry_channel()`, `caller_fields()`,
and `execution_type()` in [observability.py](../observability.py). Chat history
keys still use a hashed conversation ID; only telemetry uses the raw value.

## G8. Propagate W3C trace context ⚠️

"When you propagate context through your agents and services, you ensure that
traces, logs, and metrics are properly correlated across the entire request
lifecycle." [DISTRO] The distro can parent a scope to an incoming trace:

```python
from microsoft.opentelemetry.a365.core.utils import extract_context_from_headers
from microsoft.opentelemetry.a365.core.span_details import SpanDetails

parent = extract_context_from_headers({"traceparent": request.headers["traceparent"]})
InvokeAgentScope.start(..., span_details=SpanDetails(parent_context=parent))
```

**Here:**

- **Inbound:** the request's `traceparent` is captured in `host.py`, always
  recorded as a span link, and logged as
  `Incoming traceparent: trace_id=… parent_span_id=…`. Making the root a child
  of it is gated by `A365_CONTINUE_INCOMING_TRACE` (default `false`).
- **Outbound:** Work IQ MCP requests carry `traceparent`
  (`_inject_trace_context()` in [tools/workiq_tools.py](../tools/workiq_tools.py)).

**Observed in this tenant (September 2026):**

- Inbound Agent 365 requests carried **no** `traceparent` (0 of 5 turns), so the
  agent can't continue the gateway's trace.
- `ExecuteToolByGateway` rows from the Work IQ MCP gateway kept their own trace
  IDs, had no parent, and had no conversation ID, so the MCP gateway doesn't
  continue our trace.

Keep `A365_CONTINUE_INCOMING_TRACE=false` until an incoming `traceparent` is seen
**and** its parent span is confirmed to reach Agent 365. Otherwise the root
would point at a missing parent again (G4).

## G9. Keep content capture off unless approved ✅

`enable_sensitive_data` "enable[s] sensitive data recording (prompts, tool
arguments, results)" [SRC]. Stored offline payloads "may include prompts,
completions, or tool arguments" when it's on. [DISTRO-GH]

| Setting | Effect |
|---|---|
| `ENABLE_A365_SENSITIVE_DATA=false` (default here) | Message content and tool arguments/results aren't on spans |
| `a365_suppress_invoke_agent_input=True` | Strips input messages from `invoke_agent` spans only [DISTRO-GH] |

**Trade-off:** [ATTR] marks messages and tool arguments/results as mandatory,
and Purview policies over agent runs key off "request, and response messages"
from the `invoke_agent` span and its descendants [CONCEPTS]. Turning capture
on closes that gap but stores content in Agent 365 telemetry.

This flag doesn't affect the separate Purview integration in
[purview.py](../purview.py), which sends content to Purview on every turn
regardless.

## G10. Keep durable delivery on, in a protected directory ⚠️

With the exporter enabled, failed payloads are persisted and replayed
(at-least-once delivery): on by default, 2-day retention, 50 MB maximum.
Choose `a365_exporter_storage_directory` so that "only the current user or
service account can read" it. [DISTRO-GH]

**Here:** defaults are used. On Azure Container Apps the default directory is
the container's local disk, which is lost on restart or redeploy, so queued
payloads don't survive those events. To make them durable, mount a volume and
set `a365_exporter_storage_directory`.

## G11. Know which instrumentations A365-only mode turns off ✅

With `enable_a365=True` and without `enable_azure_monitor`, web framework, HTTP
client, and Azure SDK instrumentations (`httpx`, `requests`, `azure_sdk`, and
others) are disabled; GenAI instrumentations (`langchain`, `openai`, and others)
stay on. With `enable_azure_monitor=True`, all stay on. [DISTRO-GH]

**Here:** `configure_observability()` enables `langchain` only and disables the
others explicitly, so Agent 365 receives GenAI spans only.

**For a single end-to-end trace** that includes HTTP and Azure SDK calls,
additionally export to Azure Monitor (Application Insights) with
`enable_azure_monitor=True` and a connection string. Note that with both
enabled, Azure Monitor receives spans in the A365 format. [DISTRO-GH]

## G12. Consider the hosting middleware ❌

`ObservabilityHostingManager` registers baggage middleware and output-logging
middleware. Both default to off. Output logging emits `output_messages` spans.
[DISTRO-GH]

```python
from microsoft.opentelemetry.a365.hosting import (
    ObservabilityHostingManager, ObservabilityHostingOptions,
)

ObservabilityHostingManager.configure(
    adapter.middleware_set,
    ObservabilityHostingOptions(enable_baggage=True, enable_output_logging=True),
)
```

**Here:** not used. Baggage is built explicitly (G3, G7), and output logging
would capture outgoing message content "verbatim as span attributes" [SRC],
which conflicts with G9. Revisit if `output_messages` spans are required.

## G13. Validate locally before deploying ✅

Use the console exporter with the A365 exporter off, and enable debug logging
for the exporter. [DISTRO-GH]

```python
use_microsoft_opentelemetry(enable_a365=True, enable_console=True)
logging.getLogger("microsoft.opentelemetry.a365.core.exporters.agent365_exporter").setLevel(logging.DEBUG)
```

**Here:** `A365_EXPORTER_LOG_LEVEL=DEBUG` logs each upload, the service's
per-span results, and correlation IDs. The unit tests in
[tests/test_observability.py](../tests/test_observability.py) run the real scopes
against an in-memory exporter.

## G14. Verify ingestion, not just HTTP 200 ✅

"A `200 OK` isn't proof of ingestion. Inspect the response's `results`."
Spans are also rejected when no user in the tenant has a Microsoft 365 E7 or
Microsoft Agent 365 license **assigned**. [CONCEPTS]

Agent 365 telemetry lands in **Defender advanced hunting**, `CloudAppEvents`.
In this tenant it wasn't copied to the Sentinel data lake or Log Analytics
`CloudAppEvents`. Ingestion took **20–37 minutes** in our tests (observed, not
documented).

```kql
CloudAppEvents
| where Timestamp > ago(2h)
| where ActionType in ("InvokeAgent", "InferenceCall", "ExecuteToolBySDK", "ExecuteToolByGateway")
| extend R = RawEventData
| where coalesce(tostring(R.TargetAgentId), tostring(R.AgentId)) == "<agent instance appId>"
| summarize count() by ActionType, Source = tostring(R.InvokeSource)
```

Field notes:

- On `InvokeAgent` rows the agent is `TargetAgentId`/`TargetAgentName`;
  `AgentId` there is all zeros.
- `InferenceCall` rows carry `TraceId` and `ConversationId` inside
  `CopilotEventData`.

---

## End-to-end linkage in Agent 365 logs

| Hop | Links by ID? | Key |
|---|---|---|
| Within a turn (root → LangChain agent → model and tool calls) | ✅ | `TraceId`, `OpId`/`ParentId` |
| Turns in a conversation | ✅ | `ConversationId`/`SessionIdentity` |
| Agent 365 gateway (`InvokeAgentByGateway`) → agent | ⚠️ Conversation only | Shared raw `ConversationId`. There are several gateway rows per turn and no `traceparent`. |
| Agent → Work IQ MCP server (`ExecuteToolByGateway`) | ❌ Time only | No conversation ID, no parent, separate trace |
| Agent → Microsoft 365 resource audit (for example `MailItemsAccessed`) | ❌ Time and account only | No trace fields |

Full conversation, including the gateway's rows:

```kql
CloudAppEvents
| where Timestamp > ago(1d)
| extend R = RawEventData
| extend Conv = coalesce(tostring(R.ConversationId), tostring(R.CopilotEventData.ConversationId)),
         TraceId = coalesce(tostring(R.TraceId), tostring(R.CopilotEventData.TraceId))
| where Conv == "<raw activity.conversation.id>"
| project Timestamp, ActionType, Source = tostring(R.InvokeSource), Tool = tostring(R.ToolName),
          OpId = tostring(R.OpId), ParentId = tostring(R.ParentId), TraceId
| order by Timestamp asc
```

## Backlog

| Item | Guideline |
|---|---|
| Verify in the admin center whether the two `invoke_agent` spans per turn count as two | G5 |
| Decide on content capture with the customer | G9 |
| Mount a volume for durable offline storage | G10 |
| Optional Azure Monitor export for a single trace including HTTP and Azure SDK calls | G11 |
| Switch on `A365_CONTINUE_INCOMING_TRACE` if Agent 365 starts sending `traceparent` | G8 |
