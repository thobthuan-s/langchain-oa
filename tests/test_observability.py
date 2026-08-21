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