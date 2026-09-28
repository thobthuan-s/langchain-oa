import asyncio

import pytest

from tools import workiq_tools
from tools.workiq_tools import is_read_only_workiq_tool


@pytest.mark.parametrize(
    "name",
    [
        "mcp_MailTools_graph_mail_getMessage",
        "mcp_MailTools_graph_mail_searchMessages",
        "mcp_MailTools_graph_mail_listSent",
        "getMailboxSettings",
        "findFileOrFolder",
        "readSmallTextFile",
        "query_federated_knowledge",
        "listCalendarView",
    ],
)
def test_read_tools_are_allowed(name) -> None:
    assert is_read_only_workiq_tool(name) is True


@pytest.mark.parametrize(
    "name",
    [
        "mcp_MailTools_graph_mail_sendMail",
        "mcp_MailTools_graph_mail_updateMessage",
        "mcp_MailTools_graph_mail_reply",
        "mcp_MailTools_graph_mail_replyAll",
        "mcp_MailTools_graph_mail_deleteMessage",
        "checkOutFile",
        "checkInFile",
        "findAndMarkRead",
        "getMessageAndFlag",
        "getUserSettingsAndUpdate",
        "setPresence",
        "",
    ],
)
def test_write_tools_are_blocked(name) -> None:
    assert is_read_only_workiq_tool(name) is False


def test_annotations_can_only_tighten_the_policy() -> None:
    assert is_read_only_workiq_tool("getMessage", {"annotations": {"readOnlyHint": False}}) is False
    assert is_read_only_workiq_tool("getMessage", {"annotations": {"destructiveHint": True}}) is False
    assert is_read_only_workiq_tool("sendMail", {"annotations": {"readOnlyHint": True}}) is False
    assert is_read_only_workiq_tool("getMessage", {"annotations": {"readOnlyHint": True}}) is True


def test_explicit_allowlist_restricts_calls(monkeypatch) -> None:
    monkeypatch.setattr(workiq_tools.settings, "workiq_allowed_tools", "getMessage, listSent")

    assert is_read_only_workiq_tool("getMessage") is True
    assert is_read_only_workiq_tool("searchMessages") is False


def test_call_rejects_tools_whose_annotations_declare_writes(monkeypatch) -> None:
    async def fake_list(_server):
        return [{"name": "getAndArchive", "annotations": {}}, {"name": "getThing", "annotations": {"readOnlyHint": False}}]

    async def fake_call(*_args):
        raise AssertionError("must not be called")

    monkeypatch.setattr(workiq_tools, "_list_tools", fake_list)
    monkeypatch.setattr(workiq_tools, "_call_tool", fake_call)

    result = asyncio.run(
        workiq_tools.call_readonly_workiq_tool.ainvoke({"server": "mail", "tool_name": "getThing", "arguments": {}})
    )

    assert result["status"] == "blocked"
