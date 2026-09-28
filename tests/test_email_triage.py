import asyncio
import time
from types import SimpleNamespace

import pytest
from microsoft_agents.activity import Activity
from microsoft_agents.hosting.core import MemoryStorage

import email_triage
from email_triage import (
    EmailTriageController,
    PendingProposal,
    TriageCategory,
    TriageDecision,
    TriagePriority,
    TriageStore,
    apply_policy,
    html_to_text,
    parse_approval_command,
    text_to_email_html,
)
from tools import workiq_tools

APPROVER_OID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture(autouse=True)
def _triage_settings(monkeypatch):
    monkeypatch.setattr(email_triage.settings, "email_triage_approvers", APPROVER_OID)
    monkeypatch.setattr(email_triage.settings, "email_triage_escalation_address", "manager@contoso.com")
    monkeypatch.setattr(email_triage.settings, "email_triage_internal_domains", "")
    monkeypatch.setattr(email_triage.settings, "email_triage_auto_tag", True)
    monkeypatch.setattr(email_triage.settings, "email_triage_approval_ttl_hours", 72)
    monkeypatch.setattr(email_triage.settings, "enable_purview", False)


def _decision(**overrides) -> TriageDecision:
    values = {
        "category": TriageCategory.QUESTION,
        "priority": TriagePriority.NORMAL,
        "summary": "Asks for the status of the migration.",
        "needs_reply": True,
        "reply_text": "Thanks, the migration is on track.\n\nLangchainOA",
        "escalate": False,
        "escalation_reason": "",
        "risk_flags": [],
    }
    values.update(overrides)
    return TriageDecision(**values)


def _email_activity(sender: str = "sender@contoso.com", email_id: str = "AAMkAD-email-1") -> Activity:
    return Activity.model_validate(
        {
            "id": "incoming-activity-1",
            "type": "message",
            "channelId": "agents",
            "serviceUrl": "https://smba.example/connector",
            "recipient": {
                "id": "agent@contoso.com",
                "name": "LangchainOA",
                "agenticUserId": "agentic-user",
                "agenticAppId": "agentic-app",
                "tenantId": "tenant-1",
                "role": "agenticUser",
            },
            "conversation": {"id": "email-conversation-1", "tenantId": "tenant-1"},
            "from": {"id": sender, "name": "Sender Name", "role": "user"},
            "name": "emailNotification",
            "entities": [
                {"id": "email", "type": "productInfo"},
                {
                    "type": "emailNotification",
                    "id": email_id,
                    "conversationId": "thread-1",
                    "htmlBody": "<body><style>p{}</style><div>Hi,</div><div>What is the status?</div></body>",
                },
            ],
        }
    )


def _teams_activity(text: str, aad_object_id: str = APPROVER_OID) -> Activity:
    return Activity.model_validate(
        {
            "id": "teams-activity-1",
            "type": "message",
            "channelId": "msteams",
            "serviceUrl": "https://smba.example/teams",
            "text": text,
            "recipient": {"id": "agent@contoso.com", "role": "agenticUser", "agenticAppId": "agentic-app"},
            "conversation": {"id": "teams-chat-1"},
            "from": {"id": "29:approver", "aadObjectId": aad_object_id, "name": "Approver"},
        }
    )


class _FakeContext:
    def __init__(self, activity: Activity) -> None:
        self.activity = activity
        self.identity = SimpleNamespace(name="claims")
        self.sent: list = []

    async def send_activity(self, activity):
        self.sent.append(activity)


class _FakeAdapter:
    def __init__(self) -> None:
        self.proactive: list[tuple[Activity, list]] = []

    async def continue_conversation_with_claims(self, _claims, continuation, callback):
        context = _FakeContext(continuation)
        await callback(context)
        self.proactive.append((continuation, context.sent))


def _controller(adapter: _FakeAdapter, mail_calls: list, monkeypatch) -> EmailTriageController:
    async def fake_invoke(operation, build_arguments):
        tool = {"name": f"mcp_MailTools_graph_mail_{operation}", "inputSchema": {"properties": {}}}
        mail_calls.append((operation, build_arguments(tool)))
        return {"tool": tool["name"], "result": {}}

    async def tokens(_context):
        return {"mail": "token"}

    async def purview_token(_context):
        return None

    monkeypatch.setattr(workiq_tools, "invoke_mail_operation", fake_invoke)
    return EmailTriageController(
        adapter=adapter,
        storage=MemoryStorage(),
        exchange_workiq_tokens=tokens,
        exchange_purview_token=purview_token,
        conversation_key=lambda value: f"conv-{value}",
    )


def _notification(activity: Activity):
    from microsoft_agents_a365.notifications import AgentNotificationActivity

    return AgentNotificationActivity(activity)


# --- Pure helpers -----------------------------------------------------------------


def test_html_to_text_drops_styles_and_keeps_lines() -> None:
    text = html_to_text("<head><title>x</title></head><style>a{}</style><p>Hello&nbsp;there</p><div>Line 2</div>")

    assert text == "Hello there\n\nLine 2"


def test_reply_html_is_escaped() -> None:
    assert text_to_email_html("a <b>\nc\n\nd") == "<p>a &lt;b&gt;<br>c</p><p>d</p>"


def test_policy_blocks_replies_to_spam_and_injection() -> None:
    spam = apply_policy(_decision(category=TriageCategory.SPAM_OR_PHISHING), external=True)
    injected = apply_policy(_decision(risk_flags=["prompt_injection", "made_up_flag"]), external=False)

    assert (spam.needs_reply, spam.reply_text) == (False, "")
    assert "external_sender" in spam.risk_flags
    assert injected.needs_reply is False
    assert injected.escalate is True
    assert injected.risk_flags == ["prompt_injection"]


def test_policy_forces_escalation_category_and_drops_empty_reply() -> None:
    decision = apply_policy(
        _decision(category=TriageCategory.ESCALATION, reply_text="  ", escalate=False), external=False
    )

    assert decision.escalate is True
    assert decision.needs_reply is False


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("approve ABC234", ("approve", "ABC234", "")),
        ("  Reject abc234 ", ("reject", "ABC234", "")),
        ("edit ABC234: Thanks, done.\nBye", ("edit", "ABC234", "Thanks, done.\nBye")),
    ],
)
def test_parse_approval_commands(text, expected) -> None:
    command = parse_approval_command(text)

    assert (command.verb, command.code, command.text) == expected


@pytest.mark.parametrize("text", ["approve", "approve ABC12", "please approve ABC234", "approve ABC1O0"])
def test_parse_rejects_non_commands(text) -> None:
    assert parse_approval_command(text) is None


def test_mail_operation_resolution_uses_exact_suffixes() -> None:
    tools = [
        {"name": "mcp_MailTools_graph_mail_replyAll"},
        {"name": "mcp_MailTools_graph_mail_reply"},
        {"name": "mcp_MailTools_graph_mail_sendDraft"},
        {"name": "mcp_MailTools_graph_mail_sendMail"},
        {"name": "mcp_MailTools_graph_mail_updateMessage"},
    ]

    assert workiq_tools.resolve_mail_operation("reply", tools)["name"].endswith("_reply")
    assert workiq_tools.resolve_mail_operation("send", tools)["name"].endswith("_sendMail")
    assert workiq_tools.resolve_mail_operation("tag", tools)["name"].endswith("_updateMessage")
    assert workiq_tools.resolve_mail_operation("reply", tools[2:]) is None
    with pytest.raises(workiq_tools.WorkIqError):
        workiq_tools.resolve_mail_operation("delete", tools)


def test_write_operations_are_not_exposed_to_the_model() -> None:
    from tools import ALL_TOOLS

    assert {tool.name for tool in ALL_TOOLS}.isdisjoint({"invoke_mail_operation", "resolve_mail_operation"})


# --- Store ------------------------------------------------------------------------


def test_store_dedupes_and_claims_once() -> None:
    async def scenario():
        store = TriageStore(MemoryStorage())
        assert await store.mark_seen("email-1") is True
        assert await store.mark_seen("email-1") is False

        proposal = PendingProposal(code="ABC234", email_id="e", email_reference={}, decision=_decision())
        await store.save(proposal)
        first = await store.claim("abc234")
        second = await store.claim("ABC234")
        return first, second

    first, second = asyncio.run(scenario())

    assert first is not None and first.status == "processing"
    assert second is None


def test_expired_proposals_cannot_be_claimed() -> None:
    async def scenario():
        store = TriageStore(MemoryStorage())
        old = PendingProposal(
            code="ABC234",
            email_id="e",
            email_reference={},
            decision=_decision(),
            created_at=time.time() - 73 * 3600,
        )
        await store.save(old)
        return await store.claim("ABC234"), await store.pending()

    claimed, pending = asyncio.run(scenario())

    assert claimed is None
    assert pending == []


# --- Controller -------------------------------------------------------------------


def test_email_is_triaged_tagged_and_sent_to_approver(monkeypatch) -> None:
    adapter, mail_calls = _FakeAdapter(), []
    controller = _controller(adapter, mail_calls, monkeypatch)
    seen_bodies: list[str] = []

    async def fake_classify(_name, _address, _external, body):
        seen_bodies.append(body)
        return _decision()

    monkeypatch.setattr(email_triage, "classify_email", fake_classify)

    async def scenario():
        approver_turn = _FakeContext(_teams_activity("hello"))
        assert await controller.handle_approver_message(approver_turn) is False
        await controller.handle_email(_FakeContext(_email_activity()), _notification(_email_activity()))
        return await controller.store.pending()

    pending = asyncio.run(scenario())

    assert seen_bodies == ["Hi,\n\nWhat is the status?"]
    assert mail_calls == [
        ("tag", {"id": "AAMkAD-email-1", "categories": ["Triage/question", "Triage/normal"]})
    ]
    assert len(pending) == 1 and pending[0].delivered is True
    continuation, sent = adapter.proactive[0]
    assert continuation.conversation.id == "teams-chat-1"
    assert f"approve {pending[0].code}" in sent[0]
    assert "Reply to the sender" in sent[0]


def test_approval_replies_in_email_thread_and_escalates(monkeypatch) -> None:
    adapter, mail_calls = _FakeAdapter(), []
    controller = _controller(adapter, mail_calls, monkeypatch)

    async def fake_classify(*_args):
        return _decision(escalate=True, escalation_reason="Needs a manager decision.")

    monkeypatch.setattr(email_triage, "classify_email", fake_classify)

    async def scenario():
        await controller.handle_approver_message(_FakeContext(_teams_activity("hi")))
        await controller.handle_email(_FakeContext(_email_activity()), _notification(_email_activity()))
        code = (await controller.store.pending())[0].code
        approve_turn = _FakeContext(_teams_activity(f"approve {code}"))
        assert await controller.handle_approver_message(approve_turn) is True
        again_turn = _FakeContext(_teams_activity(f"approve {code}"))
        await controller.handle_approver_message(again_turn)
        return code, approve_turn.sent, again_turn.sent, await controller.store.get(code)

    code, approve_sent, again_sent, stored = asyncio.run(scenario())

    email_continuation, email_sent = adapter.proactive[-1]
    assert email_continuation.conversation.id == "email-conversation-1"
    assert email_continuation.id == "incoming-activity-1"
    entity = email_sent[0].entities[0]
    assert entity.type == "emailResponse"
    assert entity.html_body == "<p>Thanks, the migration is on track.</p><p>LangchainOA</p>"
    send_call = [args for operation, args in mail_calls if operation == "send"][0]
    assert send_call["message"]["toRecipients"] == [{"emailAddress": {"address": "manager@contoso.com"}}]
    assert stored.status == "approved"
    assert "Reply sent in the email thread." in approve_sent[0]
    assert "Escalated to manager@contoso.com." in approve_sent[0]
    assert f"No pending proposal `{code}`" in again_sent[0]


def test_edit_sends_the_approver_text(monkeypatch) -> None:
    adapter, mail_calls = _FakeAdapter(), []
    controller = _controller(adapter, mail_calls, monkeypatch)

    async def fake_classify(*_args):
        return _decision()

    monkeypatch.setattr(email_triage, "classify_email", fake_classify)

    async def scenario():
        await controller.handle_email(_FakeContext(_email_activity()), _notification(_email_activity()))
        code = (await controller.store.pending())[0].code
        await controller.handle_approver_message(_FakeContext(_teams_activity(f"edit {code}: Use <this> text")))

    asyncio.run(scenario())

    _continuation, email_sent = adapter.proactive[-1]
    assert email_sent[0].entities[0].html_body == "<p>Use &lt;this&gt; text</p>"


def test_reject_and_non_approver_send_nothing(monkeypatch) -> None:
    adapter, mail_calls = _FakeAdapter(), []
    controller = _controller(adapter, mail_calls, monkeypatch)

    async def fake_classify(*_args):
        return _decision()

    monkeypatch.setattr(email_triage, "classify_email", fake_classify)

    async def scenario():
        await controller.handle_email(_FakeContext(_email_activity()), _notification(_email_activity()))
        code = (await controller.store.pending())[0].code
        stranger = _FakeContext(_teams_activity(f"approve {code}", aad_object_id="someone-else"))
        assert await controller.handle_approver_message(stranger) is True
        await controller.handle_approver_message(_FakeContext(_teams_activity(f"reject {code}")))
        return stranger.sent, await controller.store.get(code)

    stranger_sent, stored = asyncio.run(scenario())

    assert stranger_sent == ["Only configured approvers can act on triaged email."]
    assert stored.status == "rejected"
    assert adapter.proactive == []
    assert [operation for operation, _ in mail_calls] == ["tag"]


def test_duplicate_and_self_sent_emails_are_ignored(monkeypatch) -> None:
    adapter, mail_calls = _FakeAdapter(), []
    controller = _controller(adapter, mail_calls, monkeypatch)
    calls: list[str] = []

    async def fake_classify(*_args):
        calls.append("classified")
        return _decision()

    monkeypatch.setattr(email_triage, "classify_email", fake_classify)

    async def scenario():
        for activity in (_email_activity(), _email_activity(), _email_activity(sender="agent@contoso.com", email_id="x")):
            await controller.handle_email(_FakeContext(activity), _notification(activity))

    asyncio.run(scenario())

    assert calls == ["classified"]


def test_queued_proposal_is_delivered_when_approver_chats(monkeypatch) -> None:
    adapter, mail_calls = _FakeAdapter(), []
    controller = _controller(adapter, mail_calls, monkeypatch)

    async def fake_classify(*_args):
        return _decision()

    monkeypatch.setattr(email_triage, "classify_email", fake_classify)

    async def scenario():
        await controller.handle_email(_FakeContext(_email_activity()), _notification(_email_activity()))
        turn = _FakeContext(_teams_activity("good morning"))
        handled = await controller.handle_approver_message(turn)
        return handled, turn.sent, await controller.store.undelivered()

    handled, sent, undelivered = asyncio.run(scenario())

    assert handled is False
    assert "New email triaged" in sent[0]
    assert undelivered == []


def test_classification_failure_still_informs_the_approver(monkeypatch) -> None:
    adapter, mail_calls = _FakeAdapter(), []
    controller = _controller(adapter, mail_calls, monkeypatch)

    async def failing_classify(*_args):
        raise RuntimeError("model unavailable")

    monkeypatch.setattr(email_triage, "classify_email", failing_classify)

    async def scenario():
        await controller.handle_approver_message(_FakeContext(_teams_activity("hi")))
        await controller.handle_email(_FakeContext(_email_activity()), _notification(_email_activity()))

    asyncio.run(scenario())

    _continuation, sent = adapter.proactive[-1]
    assert "Automatic triage failed" in sent[0]
    assert "Nothing to approve" in sent[0]


def test_host_routes_email_notifications_to_triage(monkeypatch) -> None:
    import host

    monkeypatch.setattr(host.settings, "enable_email_triage", True)
    app_host = host.LangchainOaHost()
    email_context = SimpleNamespace(activity=_email_activity())
    teams_context = SimpleNamespace(activity=_teams_activity("hello"))

    email_route = next(route for route in app_host.agent_app._route_list if route.selector(email_context))
    teams_route = next(route for route in app_host.agent_app._route_list if route.selector(teams_context))

    assert email_route is not teams_route
    assert email_route.rank == 0
