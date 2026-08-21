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