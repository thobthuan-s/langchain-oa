"""Microsoft OpenTelemetry Distro setup for LangChain, Azure OpenAI, and Agent 365 export."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator

from config import settings
from token_cache import get_cached_agentic_token

logger = logging.getLogger(__name__)

try:
    from microsoft.opentelemetry import use_microsoft_opentelemetry

    try:
        from microsoft.opentelemetry.a365.core import BaggageBuilder
    except ImportError:
        from microsoft.opentelemetry.a365.core.middleware.baggage_builder import BaggageBuilder
    from microsoft.opentelemetry.a365.core import (
        AgentDetails,
        CallerDetails,
        Channel,
        InvokeAgentScope,
        InvokeAgentScopeDetails,
        Request,
        ServiceEndpoint,
        UserDetails,
    )
    from microsoft.opentelemetry.a365.core.span_details import SpanDetails
    from microsoft.opentelemetry.a365.core.utils import extract_context_from_headers
    from opentelemetry import trace as otel_trace
    _AVAILABLE = True
except Exception as import_error:  # pragma: no cover - optional dependency
    BaggageBuilder = None  # type: ignore[assignment,misc]
    InvokeAgentScope = None  # type: ignore[assignment,misc]
    use_microsoft_opentelemetry = None  # type: ignore[assignment]
    _AVAILABLE = False
    _IMPORT_ERROR = import_error
else:
    _IMPORT_ERROR = None

_configured = False

# Placeholder the attribute reference prescribes when the caller's IP is unknown;
# Teams and email activities do not carry the end user's IP address.
UNKNOWN_CLIENT_IP = "0.0.0.0"

# W3C trace headers of the HTTP request currently being processed.
_incoming_trace_headers: ContextVar[dict[str, str]] = ContextVar("langchainoa_incoming_trace", default={})


def set_incoming_trace_headers(headers: Any) -> Any:
    """Remember the request's ``traceparent``/``tracestate`` for this turn; returns a reset token."""

    captured = {
        name: str(headers.get(name))
        for name in ("traceparent", "tracestate")
        if headers is not None and headers.get(name)
    }
    return _incoming_trace_headers.set(captured)


def reset_incoming_trace_headers(token: Any) -> None:
    _incoming_trace_headers.reset(token)


def incoming_trace_context() -> Any:
    """Return the span context of the incoming ``traceparent``, or None when absent or invalid."""

    headers = _incoming_trace_headers.get()
    if not (_AVAILABLE and headers.get("traceparent")):
        return None
    span_context = otel_trace.get_current_span(extract_context_from_headers(headers)).get_span_context()
    return span_context if span_context.is_valid else None


def telemetry_conversation_id(turn_context: Any, fallback: str) -> str:
    """Use the raw activity conversation ID, as the distro's ``populate_baggage`` helper does.

    The Agent 365 gateway records the same raw value, so gateway and agent rows
    share a conversation key in Defender advanced hunting.
    """

    conversation = getattr(getattr(turn_context, "activity", None), "conversation", None)
    return _value(conversation, "id") or fallback


def telemetry_channel(activity: Any) -> str:
    """Map the activity channel to the canonical Agent 365 channel names (``msteams``, ``outlook``)."""

    channel_id = getattr(activity, "channel_id", None) if activity is not None else None
    channel = str(getattr(channel_id, "channel", None) or channel_id or "").lower()
    sub_channel = str(getattr(channel_id, "sub_channel", None) or "").lower()
    if not sub_channel and ":" in channel:
        channel, sub_channel = channel.split(":", 1)
    if channel == "agents" and sub_channel == "email":
        return "outlook"
    return channel or "msteams"


def execution_type(activity: Any) -> str:
    """``EventToAgent`` for platform notifications (for example email), else ``HumanToAgent``."""

    channel_id = getattr(activity, "channel_id", None) if activity is not None else None
    channel = str(getattr(channel_id, "channel", None) or channel_id or "").lower()
    return "EventToAgent" if channel.startswith("agents") else "HumanToAgent"


def caller_fields(sender: Any) -> dict[str, str | None]:
    """``user.id`` must be an Entra object ID; external senders (email) only get ``user.email``."""

    sender_id = _value(sender, "id")
    return {
        "user_id": _value(sender, "aad_object_id", "aadObjectId") or None,
        "user_email": sender_id if "@" in sender_id else None,
        "user_name": _value(sender, "name") or None,
    }


def configure_observability() -> None:
    """Enable LangChain and Azure OpenAI tracing plus optional Agent 365 export."""

    global _configured
    if _configured or not settings.enable_a365_observability:
        return
    if not _AVAILABLE:
        logger.warning("Microsoft OpenTelemetry Distro unavailable: %s", _IMPORT_ERROR)
        return

    use_microsoft_opentelemetry(
        enable_a365=True,
        a365_token_resolver=get_cached_agentic_token,
        a365_use_s2s_endpoint=False,
        a365_enable_observability_exporter=settings.enable_a365_observability_exporter,
        enable_sensitive_data=settings.enable_a365_sensitive_data,
        instrumentation_options={
            "langchain": {"enabled": True},
            "openai": {"enabled": False},
            "agent_framework": {"enabled": False},
            "anthropic": {"enabled": False},
            "openai_agents": {"enabled": False},
            "semantic_kernel": {"enabled": False},
            "django": {"enabled": False},
            "fastapi": {"enabled": False},
            "flask": {"enabled": False},
            "httpx": {"enabled": False},
            "psycopg2": {"enabled": False},
            "requests": {"enabled": False},
            "urllib": {"enabled": False},
            "urllib3": {"enabled": False},
        },
    )
    _configured = True
    logger.info("Microsoft OpenTelemetry configured for LangchainOA")


class TurnTelemetry:
    """Handle yielded for one turn; records the final response when allowed."""

    def __init__(self, scope: Any = None) -> None:
        self._scope = scope

    def record_response(self, text: str) -> None:
        if self._scope is not None and text and settings.enable_a365_sensitive_data:
            self._scope.record_response(text)


@contextmanager
def observability_context(
    turn_context: Any,
    conversation_id: str,
    input_text: str | None = None,
) -> Iterator[TurnTelemetry]:
    """Attach Agent 365 identity baggage and open the turn's ``invoke_agent`` span.

    Agent 365 builds a run from its root ``invoke_agent`` span: Defender's
    agent-activity views and the Microsoft 365 admin center ignore runs without
    one. The LangChain ``chat`` and ``execute_tool`` spans created inside this
    block become its children, and the baggage supplies their identity
    attributes. Message content is recorded only with ENABLE_A365_SENSITIVE_DATA.
    """

    if not (_configured and BaggageBuilder):
        yield TurnTelemetry()
        return

    activity = getattr(turn_context, "activity", None)
    sender = getattr(activity, "from_property", None) or getattr(activity, "from_", None)
    recipient = getattr(activity, "recipient", None)

    tenant_id, agent_id = runtime_identity(turn_context)
    tenant_id = (
        tenant_id
        or settings.observability_tenant_id
        or settings.tenant_id
    )
    blueprint_id = settings.observability_blueprint_id or settings.blueprint_app_id
    hostname, port = _server_parts(_value(activity, "service_url", "serviceUrl"))
    conversation_id = telemetry_conversation_id(turn_context, conversation_id)
    caller = caller_fields(sender)

    builder = (
        BaggageBuilder()
        .operation_source("SDK")
        .tenant_id(tenant_id)
        .agent_id(agent_id)
        .agent_name("LangchainOA")
        .agent_description("Operations, knowledge, and email-triage AI Teammate")
        .agent_blueprint_id(blueprint_id)
        .agentic_user_id(_value(recipient, "agentic_user_id", "agenticUserId"))
        .agentic_user_email(_value(recipient, "agentic_user_upn", "agenticUserUpn"))
        .conversation_id(conversation_id)
        .session_id(conversation_id)
        .channel_name(telemetry_channel(activity))
        .invoke_agent_server(hostname, port)
        .user_id(caller["user_id"])
        .user_email(caller["user_email"])
        .user_name(caller["user_name"])
        .user_client_ip(UNKNOWN_CLIENT_IP)
        # The builder omits port 443, but the attribute reference makes server.port mandatory.
        .set_pairs({"server.port": str(port)})
    )
    with builder.build():
        scope = _start_invoke_agent(turn_context, conversation_id, input_text, tenant_id, agent_id, blueprint_id)
        if scope is None:
            yield TurnTelemetry()
            return
        with scope:
            yield TurnTelemetry(scope)


def invoke_agent_details(
    turn_context: Any,
    conversation_id: str,
    input_text: str | None,
    tenant_id: str,
    agent_id: str,
    blueprint_id: str,
) -> dict[str, Any]:
    """Build the arguments for ``InvokeAgentScope.start`` from the incoming activity."""

    activity = getattr(turn_context, "activity", None)
    sender = getattr(activity, "from_property", None) or getattr(activity, "from_", None)
    recipient = getattr(activity, "recipient", None)
    hostname, port = _server_parts(_value(activity, "service_url", "serviceUrl"))
    recipient_id = _value(recipient, "id")
    caller = caller_fields(sender)
    return {
        "request": Request(
            content=[input_text] if input_text and settings.enable_a365_sensitive_data else None,
            session_id=conversation_id,
            conversation_id=conversation_id,
            channel=Channel(name=telemetry_channel(activity)),
        ),
        "scope_details": InvokeAgentScopeDetails(endpoint=ServiceEndpoint(hostname=hostname, port=port)),
        "agent_details": AgentDetails(
            agent_id=agent_id,
            agent_name="LangchainOA",
            agent_description="Operations, knowledge, and email-triage AI Teammate",
            agentic_user_id=_value(recipient, "agentic_user_id", "agenticUserId") or None,
            agentic_user_email=_value(recipient, "agentic_user_upn", "agenticUserUpn")
            or (recipient_id if "@" in recipient_id else None),
            agent_blueprint_id=blueprint_id or None,
            tenant_id=tenant_id,
        ),
        "caller_details": CallerDetails(
            user_details=UserDetails(
                user_id=caller["user_id"],
                user_email=caller["user_email"],
                user_name=caller["user_name"],
                user_client_ip=UNKNOWN_CLIENT_IP,
            )
        ),
    }


def root_span_details() -> Any:
    """Choose the parent of the turn's ``invoke_agent`` span.

    Agents SDK 1.x opens ``agents.app.run`` and ``agents.app.route_handler`` spans
    around every handler. The Agent 365 exporter drops them because they carry no
    gen_ai operation, so ``invoke_agent`` is never parented to them; a link keeps
    that correlation instead.

    When the request carries a W3C ``traceparent`` it is always linked, and with
    A365_CONTINUE_INCOMING_TRACE the span becomes its child so the upstream trace
    continues. That switch stays off until the upstream parent is confirmed to be
    exported to Agent 365; otherwise the run would again point at a missing parent.
    """

    links = []
    current = otel_trace.get_current_span().get_span_context()
    if current.is_valid:
        links.append(otel_trace.Link(current))
    incoming = incoming_trace_context()
    if incoming is not None:
        links.append(otel_trace.Link(incoming, {"link.source": "incoming_traceparent"}))
        logger.info(
            "Incoming traceparent: trace_id=%s parent_span_id=%s continue=%s",
            format(incoming.trace_id, "032x"),
            format(incoming.span_id, "016x"),
            settings.a365_continue_incoming_trace,
        )
    else:
        logger.info("Incoming traceparent: none")

    if incoming is not None and settings.a365_continue_incoming_trace:
        parent_context = otel_trace.set_span_in_context(otel_trace.NonRecordingSpan(incoming))
    else:
        # An empty Context is falsy and would fall back to the active span.
        parent_context = otel_trace.set_span_in_context(otel_trace.INVALID_SPAN)
    return SpanDetails(parent_context=parent_context, span_links=links or None)


def _start_invoke_agent(
    turn_context: Any,
    conversation_id: str,
    input_text: str | None,
    tenant_id: str,
    agent_id: str,
    blueprint_id: str,
) -> Any:
    if not (InvokeAgentScope and tenant_id and agent_id):
        return None
    try:
        scope = InvokeAgentScope.start(
            **invoke_agent_details(turn_context, conversation_id, input_text, tenant_id, agent_id, blueprint_id),
            span_details=root_span_details(),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("invoke_agent span unavailable: %s", exc)
        return None
    activity = getattr(turn_context, "activity", None)
    try:
        _, port = _server_parts(_value(activity, "service_url", "serviceUrl"))
        scope.set_tag_maybe("server.port", str(port))
        scope.set_tag_maybe("gen_ai.execution.type", execution_type(activity))
    except Exception as exc:  # noqa: BLE001
        logger.warning("invoke_agent optional attributes not set: %s", exc)
    return scope


def runtime_identity(turn_context: Any) -> tuple[str, str]:
    """Resolve the tenant and runtime agent identity from an incoming activity."""

    activity = getattr(turn_context, "activity", None)
    recipient = getattr(activity, "recipient", None)
    tenant_id = _value(recipient, "tenant_id", "tenantId")

    agent_id = ""
    get_instance_id = getattr(activity, "get_agentic_instance_id", None)
    if callable(get_instance_id):
        agent_id = str(get_instance_id() or "")
    if not agent_id:
        agent_id = _value(recipient, "agentic_app_id", "agenticAppId")
    return tenant_id, agent_id


def _server_parts(service_url: str) -> tuple[str, int]:
    if not service_url:
        return "agent.invalid", 443
    from urllib.parse import urlparse

    parsed = urlparse(service_url)
    return (
        parsed.hostname or "agent.invalid",
        parsed.port or (443 if parsed.scheme == "https" else 80),
    )


def _value(source: Any, *names: str) -> str:
    if source is None:
        return ""
    for name in names:
        value = source.get(name) if isinstance(source, dict) else getattr(source, name, None)
        if value:
            return str(value)
    return ""
