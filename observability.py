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
    _AVAILABLE = True
except Exception as import_error:  # pragma: no cover - optional dependency
    BaggageBuilder = None  # type: ignore[assignment,misc]
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


@contextmanager
def observability_context(turn_context: Any, conversation_id: str) -> Iterator[None]:
    """Attach non-secret Agent 365 identity and channel baggage to one turn.

    The LangChain instrumentation emits the ``invoke_agent``, ``chat``, and
    ``execute_tool`` spans; this baggage supplies the Agent 365 attributes those
    spans need. Spans created outside this block are dropped for missing identity.
    """

    if not (_configured and BaggageBuilder):
        yield
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
        yield


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
