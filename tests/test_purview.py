import asyncio

import httpx
import pytest

import purview


@pytest.fixture(autouse=True)
def _clear_scope_cache():
    purview._scope_cache.clear()
    yield
    purview._scope_cache.clear()


def _turn() -> purview.PurviewTurn:
    return purview.PurviewTurn(
        token="header.eyJvaWQiOiJ1c2VyLW9pZCJ9.sig",
        correlation_id="corr-1",
        client_request_id="req-1",
        user_id="user-oid",
        conversation_id="conv-1",
        agent_id="agent-instance-id",
    )


def test_disabled_purview_returns_no_turn() -> None:
    purview.settings.enable_purview = False

    assert asyncio.run(begin("prompt")) is None


async def begin(prompt: str):
    return await purview.begin_purview_turn("token", prompt, "conv-1", "agent-instance-id")


def test_turn_requires_runtime_agent_id() -> None:
    purview.settings.enable_purview = True
    try:
        result = asyncio.run(purview.begin_purview_turn("token", "prompt", "conv-1", ""))
    finally:
        purview.settings.enable_purview = False

    assert result is None


def test_graph_failure_degrades_instead_of_raising(monkeypatch) -> None:
    async def failing_post(*_args, **_kwargs):
        raise httpx.ConnectError("network down")

    monkeypatch.setattr(purview, "_post_graph", failing_post)

    decision = asyncio.run(purview._process_text(_turn(), "text", "uploadText", 0, "Prompt"))

    assert decision.available is False
    assert decision.blocked is False


def test_scope_location_uses_runtime_agent_id(monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def fake_post(url, token, payload, client_request_id, **_kwargs):
        captured["url"] = url
        captured["payload"] = payload
        return httpx.Response(
            200,
            json={"value": [{"activities": "uploadText,downloadText", "executionMode": "evaluateInline"}]},
        )

    monkeypatch.setattr(purview, "_post_graph", fake_post)

    _etag, modes = asyncio.run(purview._get_scope(_turn()))

    assert modes == {"uploadText": "evaluateInline", "downloadText": "evaluateInline"}
    assert captured["payload"]["locations"][0]["value"] == "agent-instance-id"


def test_no_applicable_scope_uses_content_activities(monkeypatch) -> None:
    calls: list[str] = []

    async def fake_post(url, token, payload, client_request_id, **_kwargs):
        calls.append(url)
        if url.endswith("/protectionScopes/compute"):
            return httpx.Response(200, json={"value": []})
        return httpx.Response(201, json={})

    monkeypatch.setattr(purview, "_post_graph", fake_post)

    decision = asyncio.run(purview._process_text(_turn(), "text", "uploadText", 0, "Prompt"))

    assert decision.operation == "contentActivities"
    assert decision.available is True


def test_applicable_scope_uses_process_content_and_detects_block(monkeypatch) -> None:
    async def fake_post(url, token, payload, client_request_id, **_kwargs):
        if url.endswith("/protectionScopes/compute"):
            return httpx.Response(
                200,
                json={"value": [{"activities": "uploadText", "executionMode": "evaluateInline"}]},
            )
        return httpx.Response(
            200,
            json={"policyActions": [{"action": "restrictAccess", "restrictionAction": "block"}]},
        )

    monkeypatch.setattr(purview, "_post_graph", fake_post)

    decision = asyncio.run(purview._process_text(_turn(), "text", "uploadText", 0, "Prompt"))

    assert decision.operation == "processContent"
    assert decision.blocked is True


def test_block_is_only_enforced_when_configured() -> None:
    blocked = purview.PurviewDecision(available=True, blocked=True)

    purview.settings.purview_enforce_blocks = False
    assert purview.should_enforce_block(blocked) is False

    purview.settings.purview_enforce_blocks = True
    try:
        assert purview.should_enforce_block(blocked) is True
    finally:
        purview.settings.purview_enforce_blocks = False
