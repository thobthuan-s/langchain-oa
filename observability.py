"""Microsoft OpenTelemetry Distro setup for LangChain, Azure OpenAI, and Agent 365 export."""

from __future__ import annotations

import logging
from contextlib import contextmanager
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

    builder = (
        BaggageBuilder()
        .operation_source("SDK")
        .tenant_id(tenant_id)
        .agent_id(agent_id)
        .agent_name("LangchainOA")
        .agent_description("Read-only operations and knowledge AI Teammate")
        .agent_blueprint_id(blueprint_id)
        .agentic_user_id(_value(recipient, "agentic_user_id", "agenticUserId"))
        .agentic_user_email(_value(recipient, "agentic_user_upn", "agenticUserUpn"))
        .conversation_id(conversation_id)
        .session_id(conversation_id)
        .channel_name(_value(activity, "channel_id", "channelId") or "msteams")
        .invoke_agent_server(hostname, port)
        .user_id(_value(sender, "aad_object_id", "aadObjectId", "id"))
        .user_name(_value(sender, "name"))
        .set_pairs({"gen_ai.agent.type": "Agent365AiTeammate"})
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
    return {
        "request": Request(
            content=[input_text] if input_text and settings.enable_a365_sensitive_data else None,
            session_id=conversation_id,
            conversation_id=conversation_id,
            channel=Channel(name=_value(activity, "channel_id", "channelId") or "msteams"),
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
                user_id=_value(sender, "aad_object_id", "aadObjectId", "id") or None,
                user_name=_value(sender, "name") or None,
            )
        ),
    }


def root_span_details() -> Any:
    """Make ``invoke_agent`` a trace root, linked to the SDK span that is active now.

    Agents SDK 1.x opens ``agents.app.run`` and ``agents.app.route_handler`` spans
    around every handler. The Agent 365 exporter drops them because they carry no
    gen_ai operation, so an ``invoke_agent`` parented to them points at a span that
    never arrives and the run has no root. A link keeps the correlation.
    """

    current = otel_trace.get_current_span().get_span_context()
    return SpanDetails(
        # An empty Context is falsy and would fall back to the active span.
        parent_context=otel_trace.set_span_in_context(otel_trace.INVALID_SPAN),
        span_links=[otel_trace.Link(current)] if current.is_valid else None,
    )


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
        return InvokeAgentScope.start(
            **invoke_agent_details(turn_context, conversation_id, input_text, tenant_id, agent_id, blueprint_id),
            span_details=root_span_details(),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("invoke_agent span unavailable: %s", exc)
        return None


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
