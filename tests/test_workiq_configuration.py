import importlib

from tools import workiq_tools


def test_live_manifest_builds_one_scope_per_server() -> None:
    scopes = workiq_tools.workiq_server_scopes()

    assert scopes == {
        "sharepoint": "292cff14-c0e8-4116-9e3b-99934ae05766/Tools.ListInvoke.All",
        "mail": "16b1878d-62c7-4009-aa25-68989d63bbad/Tools.ListInvoke.All",
        "calendar": "910333d2-47e9-43ca-981f-6df2f4531ef4/Tools.ListInvoke.All",
    }
    assert len(set(scopes.values())) == 3


def test_local_tokens_prefer_server_specific_values(monkeypatch) -> None:
    monkeypatch.setenv("BEARER_TOKEN_MCP_SHAREPOINTREMOTESERVER", "sharepoint-token")
    monkeypatch.setenv("BEARER_TOKEN_MCP_MAILTOOLS", "mail-token")
    monkeypatch.setenv("BEARER_TOKEN_MCP_CALENDARTOOLS", "calendar-token")
    importlib.reload(workiq_tools)

    tokens = workiq_tools._local_access_tokens()

    assert tokens["sharepoint"] == "sharepoint-token"
    assert tokens["mail"] == "mail-token"
    assert tokens["calendar"] == "calendar-token"

def test_sse_response_returns_the_message_matching_the_request_id() -> None:
    import httpx

    body = (
        'event: message\ndata: {"jsonrpc":"2.0","method":"notifications/progress","params":{}}\n\n'
        'event: message\ndata: {"jsonrpc":"2.0","id":7,\ndata: "result":{"ok":true}}\n\n'
    )
    response = httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    assert workiq_tools._decode_response(response, 7) == {"jsonrpc": "2.0", "id": 7, "result": {"ok": True}}


def test_sse_without_matching_id_is_an_error() -> None:
    import httpx
    import pytest

    body = 'data: {"jsonrpc":"2.0","id":1,"result":{}}\n\n'
    response = httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    with pytest.raises(workiq_tools.WorkIqError):
        workiq_tools._decode_response(response, 2)


def test_production_never_falls_back_to_local_tokens(monkeypatch) -> None:
    monkeypatch.setenv("BEARER_TOKEN_MCP_MAILTOOLS", "developer-token")
    monkeypatch.setattr(workiq_tools.settings, "python_environment", "Production")

    resets = workiq_tools.set_workiq_context(None, "t", "c", "e")
    try:
        assert workiq_tools._access_tokens.get() == {}
    finally:
        workiq_tools.reset_workiq_context(resets)

    monkeypatch.setattr(workiq_tools.settings, "python_environment", "")
    resets = workiq_tools.set_workiq_context(None, "t", "c", "e")
    try:
        assert workiq_tools._access_tokens.get()["mail"] == "developer-token"
    finally:
        workiq_tools.reset_workiq_context(resets)
