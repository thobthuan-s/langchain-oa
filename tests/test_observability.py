from types import SimpleNamespace

import observability
from token_cache import cache_agentic_token, clear_token_cache, get_cached_agentic_token


def test_token_cache_uses_exporter_callback_argument_order() -> None:
    clear_token_cache()
    cache_agentic_token("tenant-1", "agent-1", "token-1")

    assert get_cached_agentic_token("agent-1", "tenant-1") == "token-1"
    assert get_cached_agentic_token("tenant-1", "agent-1") is None


def test_runtime_identity_prefers_incoming_activity() -> None:
    recipient = SimpleNamespace(
        tenant_id="tenant-runtime",
        agentic_app_id="agent-runtime",
    )
    activity = SimpleNamespace(
        recipient=recipient,
        get_agentic_instance_id=lambda: "agent-instance",
    )

    assert observability.runtime_identity(SimpleNamespace(activity=activity)) == (
        "tenant-runtime",
        "agent-instance",
    )


def test_runtime_identity_falls_back_to_recipient_agentic_app_id() -> None:
    recipient = SimpleNamespace(
        tenant_id="tenant-runtime",
        agentic_app_id="agent-runtime",
    )
    activity = SimpleNamespace(recipient=recipient)

    assert observability.runtime_identity(SimpleNamespace(activity=activity)) == (
        "tenant-runtime",
        "agent-runtime",
    )


def test_server_parts_use_activity_host_and_port() -> None:
    assert observability._server_parts("https://smba.trafficmanager.net:443/emea/") == (
        "smba.trafficmanager.net",
        443,
    )


def test_configure_observability_avoids_incompatible_openai_v2_wrapper(monkeypatch) -> None:
    captured = {}

    monkeypatch.setattr(observability, "_configured", False)
    monkeypatch.setattr(observability, "_AVAILABLE", True)
    monkeypatch.setattr(observability.settings, "enable_a365_observability", True)
    monkeypatch.setattr(observability, "use_microsoft_opentelemetry", lambda **kwargs: captured.update(kwargs))

    observability.configure_observability()

    assert captured["instrumentation_options"]["langchain"]["enabled"] is True
    assert captured["instrumentation_options"]["openai"]["enabled"] is False

def _teams_turn():
    return SimpleNamespace(
        activity=SimpleNamespace(
            channel_id="msteams",
            service_url="https://smba.trafficmanager.net/emea/",
            from_property=SimpleNamespace(aad_object_id="user-oid", name="Approver"),
            recipient=SimpleNamespace(
                id="agent@contoso.com",
                tenant_id="tenant-1",
                agentic_user_id="agentic-user-1",
                agentic_app_id="agent-app",
            ),
            get_agentic_instance_id=lambda: "agent-instance",
        )
    )


def test_invoke_agent_details_carry_identity_and_skip_content_by_default(monkeypatch) -> None:
    monkeypatch.setattr(observability.settings, "enable_a365_sensitive_data", False)

    details = observability.invoke_agent_details(
        _teams_turn(), "conv-1", "secret question", "tenant-1", "agent-instance", "blueprint-1"
    )

    agent = details["agent_details"]
    assert (agent.agent_id, agent.tenant_id, agent.agent_blueprint_id) == ("agent-instance", "tenant-1", "blueprint-1")
    assert agent.agentic_user_id == "agentic-user-1"
    assert agent.agentic_user_email == "agent@contoso.com"
    assert details["request"].conversation_id == "conv-1"
    assert details["request"].channel.name == "msteams"
    assert details["request"].content is None
    assert details["caller_details"].user_details.user_id == "user-oid"
    assert details["scope_details"].endpoint.hostname == "smba.trafficmanager.net"


def test_invoke_agent_details_include_content_only_when_sensitive_data_enabled(monkeypatch) -> None:
    monkeypatch.setattr(observability.settings, "enable_a365_sensitive_data", True)

    details = observability.invoke_agent_details(_teams_turn(), "c", "question", "t", "a", "b")

    assert details["request"].content == ["question"]


def test_observability_context_wraps_the_turn_in_an_invoke_agent_scope(monkeypatch) -> None:
    events = []

    class _Scope:
        def __enter__(self):
            events.append("enter")
            return self

        def __exit__(self, *exc):
            events.append("exit")
            return False

        def record_response(self, text):
            events.append(f"response:{text}")

    class _Builder:
        def __getattr__(self, _name):
            return lambda *args, **kwargs: self

        def build(self):
            from contextlib import nullcontext

            return nullcontext()

    monkeypatch.setattr(observability, "_configured", True)
    monkeypatch.setattr(observability, "BaggageBuilder", _Builder)
    monkeypatch.setattr(observability.settings, "enable_a365_sensitive_data", True)
    started = {}

    def fake_start(**kwargs):
        started.update(kwargs)
        return _Scope()

    monkeypatch.setattr(observability.InvokeAgentScope, "start", staticmethod(fake_start))

    with observability.observability_context(_teams_turn(), "conv-1", "hi") as telemetry:
        events.append("work")
        telemetry.record_response("answer")

    assert events == ["enter", "work", "response:answer", "exit"]
    assert started["agent_details"].agent_id == "agent-instance"


def test_observability_context_is_a_no_op_when_not_configured(monkeypatch) -> None:
    monkeypatch.setattr(observability, "_configured", False)

    with observability.observability_context(_teams_turn(), "conv-1", "hi") as telemetry:
        telemetry.record_response("ignored")


def test_invoke_agent_is_a_trace_root_even_inside_an_sdk_span(monkeypatch) -> None:
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    from microsoft.opentelemetry.a365.core.opentelemetry_scope import OpenTelemetryScope

    tracer = provider.get_tracer("sdk")
    monkeypatch.setattr(OpenTelemetryScope, "_tracer", provider.get_tracer("a365"))
    monkeypatch.setattr(OpenTelemetryScope, "_is_telemetry_enabled", classmethod(lambda cls: True))

    with tracer.start_as_current_span("agents.app.route_handler") as sdk_span:
        scope = observability.InvokeAgentScope.start(
            **observability.invoke_agent_details(_teams_turn(), "conv-1", None, "tenant-1", "agent-1", "bp-1"),
            span_details=observability.root_span_details(),
        )
        with scope:
            with tracer.start_as_current_span("chat child"):
                pass

    spans = {span.name: span for span in exporter.get_finished_spans()}
    root = spans["invoke_agent LangchainOA"]
    assert root.parent is None
    assert root.links[0].context.span_id == sdk_span.get_span_context().span_id
    assert spans["chat child"].parent.span_id == root.context.span_id
    assert root.context.trace_id != sdk_span.get_span_context().trace_id
