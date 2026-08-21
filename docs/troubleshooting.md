# Troubleshooting

Failures actually hit while building and running this agent, with the evidence
that identified each one. Most cost hours to diagnose; none are obvious from the
error message alone.

---

## Agent replies "Sorry, I couldn't complete that request"

That message is the handler's catch-all. The real error is always in the logs.

```bash
az monitor log-analytics query --workspace <workspace-id> --analytics-query \
  'ContainerAppConsoleLogs_CL | where TimeGenerated > ago(1h)
   | where Log_s has "Traceback" or Log_s has "ERROR"
   | project TimeGenerated, Log_s | order by TimeGenerated desc | take 40' \
  --query "[].Log_s" -o tsv
```

Prefer Log Analytics over `az containerapp logs show`. The streaming command
drops output when piped or redirected.

---

## `KeyError: 'gen_ai.request.model'`

**Symptom.** Every turn fails. The traceback ends inside
`opentelemetry/instrumentation/openai_v2/patch.py`, before any request reaches
Azure OpenAI.

**Cause.** That instrumentation builds its span name by indexing
`gen_ai.request.model`. `AzureChatOpenAI` routes by *deployment* and never sets
that attribute, and the lookup happens before the protected block, so it is not
caught.

**Fix.** Disable the OpenAI layer; keep LangChain:

```python
instrumentation_options={
    "langchain": {"enabled": True},
    "openai": {"enabled": False},
}
```

Nothing is lost. LangChain emits the `chat` span itself.

---

## Two `invoke_agent` spans per turn

**Symptom.** Sessions appear doubled.

**Cause.** LangChain's instrumentation already emits `invoke_agent` for the
agent graph. Adding a manual `InvokeAgentScope` creates a second root.

Verify with an in-memory exporter:

```text
invoke_agent count: 1   total spans: 2
  chat          chat <model>
  invoke_agent  invoke_agent <agent>
```

**Fix.** Do not create the scope. Supply identity through `BaggageBuilder`
instead — `agentic_user_id`, `agentic_user_email`, `invoke_agent_server` — so
the automatic span carries full attribution.

---

## No telemetry in Agent 365

Work through these in order.

| Check | Expectation |
|---|---|
| Both flags set | `ENABLE_A365_OBSERVABILITY` and `ENABLE_A365_OBSERVABILITY_EXPORTER` |
| Token resolver returns a token | Otherwise export is skipped silently |
| Spans carry tenant **and** agent id | Missing either and the exporter drops them |
| Operation name is eligible | Only `invoke_agent`, `chat`, `execute_tool`, `output_messages`, `apply_guardrail` |
| `403` agent-ID mismatch | The agent id must be the **instance**, not the Blueprint |
| Licence present | Agent 365 Frontier or M365 E7 |

Baggage must be applied *before* the instrumented library creates spans.

---

## `AADSTS82002` on token exchange

Blueprints created by `a365 setup all` are `AgenticApp` type and reject the
classic on-behalf-of flow. Use the agentic auth handler
(`AgenticUserAuthorization`), which is what the SDK's `exchange_token` uses.

The same handler mints tokens for arbitrary scopes — Work IQ, observability, and
Purview all ride it. No Bot OAuth connection is required.

---

## Work IQ returns 401

**Cause.** Reusing one token across servers. Work IQ V2 gives SharePoint, Mail,
and Calendar **different OAuth audiences**.

**Fix.** Exchange one token per server scope and key them by server. Read the
audiences from `ToolingManifest.json` rather than hard-coding them, and
regenerate that file per tenant with `a365 develop add-mcp-servers`.

---

## Work IQ works locally but not in Microsoft 365, or the reverse

Local smoke tests have no `recipient.agenticUserId`, so the token chain cannot
complete. That is expected. Skip Work IQ and observability locally instead of
chasing the resulting 401 and 400 noise.

---

## A `.docx` comes back as base64

The agent can find the file but not read it.

Three contributing causes:

1. Search results are truncated before the item URL or id, so the model never
   has the identifier needed for the follow-up read.
2. Extraction is keyed to specific tool names. A differently named read tool
   returns raw content.
3. `downloadFile` does not match the read-only allowlist prefixes.

Detection itself works: a `.docx` is a ZIP, so payloads starting `PK\x03\x04`
or base64 `UEsDB` can be unzipped and `word/document.xml` parsed.

---

## `GET /` returns 401 in the logs

**Symptom.** Recurring log lines with no visible trigger:

```text
aiohttp.access:100.100.0.x [..] "GET / HTTP/1.1" 401 212 "-" "python-requests/2.32.4"
```

**What it is.** `/` is not a registered route — only `/api/messages`,
`/api/health`, and optionally `/eval/invoke` are. The JWT middleware runs before
routing resolves a 404, and rejects any path outside its anonymous allowlist, so
an unauthenticated request to `/` gets `401` instead of `404`.

The source address range (`100.100.0.0/16`) and the regular interval are
consistent with an internal Azure Container Apps platform probe rather than
external traffic. This has not been confirmed against Azure's own
documentation of that address range — treat it as a likely explanation, not a
verified one.

**Is it a problem?** No. It demonstrates the auth boundary is fail-closed:
anything other than the explicit anonymous paths is rejected regardless of
whether the route exists. Do not add `/` to the anonymous path list to silence
it; that would remove real protection to hide a log line.

---

## Purview captures nothing

**Symptom.** `processContent` returns `200`, but no text appears in Activity
Explorer.

Ranked causes:

1. **Policy scoped to the wrong app.** It must match the GUID from the runtime
   log, which is the Agent Identity — not the Blueprint. This has silently
   broken capture and blocking on multiple agents.
2. **`IsIngestionEnabled` is false.** You get matches, no text.
3. **No mailbox.** The agent user needs Exchange Online.
4. **Missing role.** Viewers need Content Explorer Content Viewer.
5. **Portal re-save.** Editing an individual-scope policy in the portal wipes
   the scope. Use PowerShell.

Confirm the location from the log rather than assuming:

```text
Purview operation=protectionScopes.compute status=200
  activities=['downloadText','uploadText'] locations=['<GUID>']
```

---

## Purview returns Graph 500

Sending content to `contentActivities` when a protection scope **does** apply.

Routing rule: if `executionMode` is present — including `evaluateOffline` — call
`processContent`. Use `contentActivities` only when no scope applies.

---

## Deploy succeeds but nothing changes

`az containerapp update --image foo:latest` does not create a revision when the
tag string is unchanged, even if the digest moved.

```bash
TAG=$(date +%Y%m%d%H%M%S)
az acr build --image "agent:$TAG" ...
az containerapp update --image "...:$TAG" ...
```

---

## `/api/messages` does not return 401

The Blueprint credentials did not load, so no auth configuration was built and
the JWT middleware was never registered. The agent is unauthenticated.

Check for this at startup:

```text
Activity Protocol credentials are incomplete; only local evaluation can be used
```

With `AUTHTYPE=ClientSecret`, all of `CLIENTID`, `TENANTID`, and `CLIENTSECRET`
must be present. `FederatedCredentials` requires the Agents SDK 1.x line — it
does not exist in `0.5.3` and will fail at token time there.

---

## Container app unreachable after an outage

If storage or a dependency was unavailable for a long period, a restart is often
not enough. Force worker reallocation:

```bash
az appservice plan update -g <rg> -n <plan> --number-of-workers 2
az appservice plan update -g <rg> -n <plan> --number-of-workers 1
```

---

## Quick reference

| Symptom | First thing to check |
|---|---|
| Every turn fails | Traceback in Log Analytics |
| Doubled sessions | Manual `InvokeAgentScope` present |
| No telemetry | Both flags, then tenant/agent id on spans |
| Work IQ 401 | One token per server audience |
| Purview silent | Policy scope GUID from the runtime log |
| Deploy no-op | Unique image tag |
| No 401 on messages | Blueprint credentials incomplete |
