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
    monkeypatch.setattr(email_triage.settings, "email_triage_research", False)
    monkeypatch.setattr(email_triage.settings, "email_triage_research_external", False)


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
        arguments = (
            build_arguments(tool) if callable(build_arguments)
            else workiq_tools.build_mail_arguments(tool, build_arguments)
        )
        mail_calls.append((operation, arguments))
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
        workiq_tools.resolve_mail_operation("archive", tools)


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
    assert send_call["toRecipients"] == [{"emailAddress": {"address": "manager@contoso.com"}}]
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


def test_grounded_reply_replaces_draft_and_lists_sources(monkeypatch) -> None:
    adapter, mail_calls = _FakeAdapter(), []
    controller = _controller(adapter, mail_calls, monkeypatch)
    monkeypatch.setattr(email_triage.settings, "email_triage_research", True)
    research_inputs: list[str] = []

    async def fake_classify(*_args):
        return _decision()

    async def fake_research(_name, _address, body, decision, customer=None):
        from tools import workiq_tools as wt

        research_inputs.append(body)
        assert wt._access_tokens.get() == {"mail": "token"}
        return email_triage.ResearchedReply(
            reply_text="The migration finished on Friday.\n\nLangchainOA",
            sources=["Migration plan.docx (SharePoint)"],
            unresolved=["Exact cut-over time"],
        )

    monkeypatch.setattr(email_triage, "classify_email", fake_classify)
    monkeypatch.setattr(email_triage, "research_reply", fake_research)

    async def scenario():
        await controller.handle_approver_message(_FakeContext(_teams_activity("hi")))
        await controller.handle_email(_FakeContext(_email_activity()), _notification(_email_activity()))
        return (await controller.store.pending())[0]

    proposal = asyncio.run(scenario())

    assert research_inputs == ["Hi,\n\nWhat is the status?"]
    assert proposal.decision.reply_text.startswith("The migration finished on Friday.")
    assert proposal.reply_sources == ["Migration plan.docx (SharePoint)"]
    card = adapter.proactive[-1][1][0]
    assert "*Sources:* Migration plan.docx (SharePoint)" in card
    assert "*Not answered:* Exact cut-over time" in card


def test_research_failure_keeps_draft_with_a_warning(monkeypatch) -> None:
    adapter, mail_calls = _FakeAdapter(), []
    controller = _controller(adapter, mail_calls, monkeypatch)
    monkeypatch.setattr(email_triage.settings, "email_triage_research", True)

    async def fake_classify(*_args):
        return _decision()

    async def failing_research(*_args):
        raise RuntimeError("tool outage")

    monkeypatch.setattr(email_triage, "classify_email", fake_classify)
    monkeypatch.setattr(email_triage, "research_reply", failing_research)

    async def scenario():
        await controller.handle_email(_FakeContext(_email_activity()), _notification(_email_activity()))
        return (await controller.store.pending())[0]

    proposal = asyncio.run(scenario())

    assert proposal.decision.reply_text == _decision().reply_text
    assert "not verified" in proposal.research_note


@pytest.mark.parametrize(
    ("overrides", "external", "research_external", "expected"),
    [
        ({}, False, False, True),
        ({}, True, False, False),
        ({}, True, True, True),
        ({"category": TriageCategory.FYI}, False, False, False),
        ({"needs_reply": False, "reply_text": ""}, False, False, False),
        ({"risk_flags": ["suspicious_link"]}, False, False, False),
        ({"risk_flags": ["external_sender"]}, True, True, True),
    ],
)
def test_should_research_policy(monkeypatch, overrides, external, research_external, expected) -> None:
    monkeypatch.setattr(email_triage.settings, "email_triage_research", True)
    monkeypatch.setattr(email_triage.settings, "email_triage_research_external", research_external)

    wanted, _note = email_triage.should_research(_decision(**overrides), external)

    assert wanted is expected


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


# --- Customer mode ----------------------------------------------------------------


def _customer_controller(adapter, mail_calls, monkeypatch, loader=None) -> EmailTriageController:
    from customer_records import CustomerRecords

    from tests.test_customer_records import _sheets

    controller = _controller(adapter, mail_calls, monkeypatch)

    async def default_loader(token):
        assert token == "graph-token"
        return _sheets()

    async def graph_token(_context):
        return "graph-token"

    controller.customer_records = CustomerRecords(loader or default_loader)
    controller._exchange_graph_token = graph_token
    return controller


def _run_customer_email(controller, sender, decision, research=None):
    async def scenario():
        await controller.handle_approver_message(_FakeContext(_teams_activity("hi")))
        activity = _email_activity(sender=sender, email_id=f"id-{sender}")
        await controller.handle_email(_FakeContext(activity), _notification(activity))
        codes = await controller.store.list_codes()
        return await controller.store.get(codes[-1])

    return asyncio.run(scenario())


def test_verified_customer_research_uses_bound_tools_and_routes_to_owner(monkeypatch) -> None:
    from tests.test_customer_records import DEMO, OWNER

    adapter, mail_calls = _FakeAdapter(), []
    controller = _customer_controller(adapter, mail_calls, monkeypatch)
    monkeypatch.setattr(email_triage.settings, "email_triage_research", True)
    seen = {}

    async def fake_classify(*_args):
        return _decision(escalate=True, escalation_reason="Renewal decision.")

    async def fake_research(_name, _address, _body, _decision, customer=None):
        from customer_records import build_customer_tools

        seen["customer"] = customer
        seen["tools"] = sorted(tool.name for tool in build_customer_tools(customer))
        return email_triage.ResearchedReply(
            reply_text="Your Microsoft 365 E5 term ends on 2026-10-20.",
            sources=["Subscription Microsoft 365 E5 (250 seats, term ends 2026-10-20)"],
            unresolved=["Whether to renew the same quantity"],
        )

    monkeypatch.setattr(email_triage, "classify_email", fake_classify)
    monkeypatch.setattr(email_triage, "research_reply", fake_research)

    proposal = _run_customer_email(controller, DEMO, _decision())

    assert seen["customer"].account_id == "ACC-001"
    assert "get_invoice" in seen["tools"]
    assert proposal.escalation_target == OWNER
    assert proposal.customer_label.startswith("Contoso Ltd (Gold)")
    card = adapter.proactive[-1][1][0]
    assert "- Customer: Contoso Ltd (Gold) · Demo Contact, IT Director, verified, billing authorized" in card
    assert f"Escalate to {OWNER}" in card


def test_unverified_customer_contact_gets_no_draft_and_goes_to_owner(monkeypatch) -> None:
    from tests.test_customer_records import OWNER

    adapter, mail_calls = _FakeAdapter(), []
    controller = _customer_controller(adapter, mail_calls, monkeypatch)
    monkeypatch.setattr(email_triage.settings, "email_triage_research", True)

    async def fake_classify(*_args):
        return _decision()

    async def must_not_research(*_args, **_kwargs):
        raise AssertionError("unverified senders must not be researched")

    monkeypatch.setattr(email_triage, "classify_email", fake_classify)
    monkeypatch.setattr(email_triage, "research_reply", must_not_research)

    proposal = _run_customer_email(controller, "someone@tailspintoys.com", _decision())

    assert proposal.decision.needs_reply is False and proposal.decision.reply_text == ""
    assert proposal.decision.escalate is True
    assert proposal.escalation_target == OWNER
    assert "not a listed contact" in proposal.customer_label


def test_unknown_sender_in_customer_mode_is_not_researched(monkeypatch) -> None:
    adapter, mail_calls = _FakeAdapter(), []
    controller = _customer_controller(adapter, mail_calls, monkeypatch)
    monkeypatch.setattr(email_triage.settings, "email_triage_research", True)
    monkeypatch.setattr(email_triage.settings, "email_triage_research_external", True)

    async def fake_classify(*_args):
        return _decision()

    async def must_not_research(*_args, **_kwargs):
        raise AssertionError("unknown senders must not be researched")

    monkeypatch.setattr(email_triage, "classify_email", fake_classify)
    monkeypatch.setattr(email_triage, "research_reply", must_not_research)

    proposal = _run_customer_email(controller, "stranger@unknown.example", _decision())

    assert proposal.customer_label == "Not found in customer records."
    assert "not in the customer records" in proposal.research_note
    assert proposal.escalation_target == ""


def test_customer_records_outage_is_reported_not_raised(monkeypatch) -> None:
    adapter, mail_calls = _FakeAdapter(), []

    async def failing_loader(_token):
        raise RuntimeError("Graph down")

    controller = _customer_controller(adapter, mail_calls, monkeypatch, loader=failing_loader)

    async def fake_classify(*_args):
        return _decision()

    monkeypatch.setattr(email_triage, "classify_email", fake_classify)

    proposal = _run_customer_email(controller, "demo@partner-test.com", _decision())

    assert proposal.customer_label == "Customer records unavailable; sender not checked."


def test_commercial_requests_are_escalated_but_still_acknowledged(monkeypatch) -> None:
    monkeypatch.setattr(email_triage.settings, "email_triage_research", True)
    decision = apply_policy(_decision(risk_flags=["commercial_commitment"], escalate=False), external=True)

    assert decision.escalate is True
    assert decision.needs_reply is True
    assert "Commercial request" in decision.escalation_reason
    wanted, _ = email_triage.should_research(decision, True, customer=_verified_stub(), customer_mode=True)
    assert wanted is True


def _verified_stub():
    from types import SimpleNamespace

    return SimpleNamespace(verified=True)


def test_outlook_first_contact_banner_is_removed() -> None:
    text = html_to_text(
        "<div>You don't often get email from a@hotmail.com. Learn why this is important</div>"
        "<p>Hi, when does our E5 renew?</p>"
    )

    assert email_triage.strip_safety_banners(text) == "Hi, when does our E5 renew?"


def test_approver_and_proposal_survive_a_restart(monkeypatch) -> None:
    storage = MemoryStorage()
    adapter, mail_calls = _FakeAdapter(), []
    first = _controller(adapter, mail_calls, monkeypatch)
    first.store = TriageStore(storage)

    async def fake_classify(*_args):
        return _decision()

    monkeypatch.setattr(email_triage, "classify_email", fake_classify)

    async def before_restart():
        await first.handle_approver_message(_FakeContext(_teams_activity("hi")))

    asyncio.run(before_restart())

    # New process: fresh controller and adapter, same durable storage, no new "hi".
    adapter_after = _FakeAdapter()
    second = _controller(adapter_after, mail_calls, monkeypatch)
    second.store = TriageStore(storage)

    async def after_restart():
        await second.handle_email(_FakeContext(_email_activity()), _notification(_email_activity()))
        await second.handle_email(_FakeContext(_email_activity()), _notification(_email_activity()))
        return await second.store.pending()

    pending = asyncio.run(after_restart())

    assert len(pending) == 1 and pending[0].delivered is True
    continuation, sent = adapter_after.proactive[0]
    assert continuation.conversation.id == "teams-chat-1"
    assert "New email triaged" in sent[0]


def test_state_storage_defaults_to_memory_and_uses_blob_when_configured(monkeypatch) -> None:
    import host
    from microsoft_agents.storage.blob import BlobStorage

    monkeypatch.setattr(host.settings, "state_storage_blob_url", "")
    assert isinstance(host.create_state_storage(), MemoryStorage)

    monkeypatch.setattr(host.settings, "state_storage_blob_url", "https://example.blob.core.windows.net")
    monkeypatch.setattr(host.settings, "state_storage_container", "agent-state")
    storage = host.create_state_storage()
    assert isinstance(storage, BlobStorage)
    assert storage.config.container_name == "agent-state"
    assert storage.config.credential is not None


def test_sensitive_flag_blocks_research_only_outside_verified_customers(monkeypatch) -> None:
    from types import SimpleNamespace

    monkeypatch.setattr(email_triage.settings, "email_triage_research", True)
    monkeypatch.setattr(email_triage.settings, "email_triage_research_external", True)
    decision = _decision(risk_flags=["external_sender", "sensitive_data"])

    verified, _ = email_triage.should_research(decision, True, SimpleNamespace(verified=True), True)
    unverified, _ = email_triage.should_research(decision, True, SimpleNamespace(verified=False), True)
    general, _ = email_triage.should_research(decision, True)
    injected, _ = email_triage.should_research(
        _decision(risk_flags=["sensitive_data", "prompt_injection"]), True, SimpleNamespace(verified=True), True
    )

    assert (verified, unverified, general, injected) == (True, False, False, False)


def test_card_ends_the_reply_quote_before_notes_and_escalation() -> None:
    proposal = PendingProposal(
        code="ABC234",
        email_id="e",
        email_reference={},
        decision=_decision(escalate=True, escalation_reason="Renewal decision."),
        reply_sources=["Subscription Microsoft 365 E5"],
        research_note="Some note.",
    )

    card = email_triage.render_proposal(proposal)
    quote_lines = [line for line in card.splitlines() if line.startswith(">")]
    after_quote = card.split(quote_lines[-1], 1)[1]

    assert after_quote.startswith("\n\n- *Sources:* Subscription Microsoft 365 E5")
    assert "- *Note:* Some note." in after_quote
    assert "\n\n2. Escalate to" in after_quote
    assert all("Note" not in line and "Escalate" not in line for line in quote_lines)


def test_email_route_caches_the_observability_token_before_triage(monkeypatch) -> None:
    import host

    monkeypatch.setattr(host.settings, "enable_email_triage", True)
    app_host = host.LangchainOaHost()
    calls: list[str] = []

    async def fake_cache(_context):
        calls.append("cache-token")

    async def fake_handle_email(_context, _notification):
        calls.append("triage")

    monkeypatch.setattr(app_host, "_cache_observability_token", fake_cache)
    monkeypatch.setattr(app_host.email_triage, "handle_email", fake_handle_email)
    context = SimpleNamespace(activity=_email_activity())
    route = next(route for route in app_host.agent_app._route_list if route.selector(context))

    asyncio.run(route.handler(context, None))

    assert calls == ["cache-token", "triage"]


def test_escalation_falls_back_to_draft_then_send(monkeypatch) -> None:
    from tools.workiq_tools import MailToolNotFound

    adapter, calls = _FakeAdapter(), []
    controller = _controller(adapter, calls, monkeypatch)

    async def fake_run(_context, operation, build_arguments):
        tool = {"name": f"mcp_MailTools_graph_mail_{operation}", "inputSchema": {"properties": {}}}
        calls.append((operation, workiq_tools.build_mail_arguments(tool, build_arguments)))
        if operation == "send":
            raise MailToolNotFound("no send tool")
        if operation == "draft":
            draft = '{"id": "AAMkAGDraftMessageId0000000001", "subject": "x"}'
            return {"tool": "createMessage", "result": {"content": [{"type": "text", "text": draft}]}}
        return {"tool": operation, "result": {}}

    monkeypatch.setattr(controller, "_run_mail_operation", fake_run)
    proposal = PendingProposal(
        code="ABC234", email_id="e", email_reference={}, escalation_target="owner@partner-test.com",
        decision=_decision(escalate=True, escalation_reason="Needs owner."),
    )

    result = asyncio.run(controller._escalate(_FakeContext(_email_activity()), proposal))

    assert result == "Escalated to owner@partner-test.com."
    assert [operation for operation, _ in calls] == ["send", "draft", "send_draft"]
    assert calls[-1][1] == {"id": "AAMkAGDraftMessageId0000000001"}


def test_mail_resolution_accepts_alternate_send_names() -> None:
    from tools import workiq_tools as wt

    assert wt.resolve_mail_operation("send", [{"name": "mcp_MailTools_graph_mail_sendEmail"}])["name"].endswith("sendEmail")
    assert wt.resolve_mail_operation("draft", [{"name": "mcp_MailTools_graph_mail_createMessage"}]) is not None
    assert wt.resolve_mail_operation("send", [{"name": "mcp_MailTools_graph_mail_sendDraft"}]) is None


def test_answered_customer_question_drops_the_classifier_escalation() -> None:
    from types import SimpleNamespace

    verified = SimpleNamespace(verified=True)
    answered = email_triage.ResearchedReply(reply_text="Your term ends on 20 October 2026.")
    open_items = email_triage.ResearchedReply(reply_text="...", unresolved=["Exact price"])
    decision = _decision(escalate=True, escalation_reason="Needs admin access.")

    assert email_triage.settle_escalation_after_research(decision, answered, verified).escalate is False
    assert email_triage.settle_escalation_after_research(decision, open_items, verified).escalate is True
    commercial = _decision(escalate=True, risk_flags=["commercial_commitment"])
    assert email_triage.settle_escalation_after_research(commercial, answered, verified).escalate is True
    assert email_triage.settle_escalation_after_research(decision, answered, SimpleNamespace(verified=False)).escalate is True



# --- Live Work IQ Mail catalog (names observed 2026-09-29) -------------------------

LIVE_MAIL_TOOLS = [
    {"name": name} for name in (
        "AddDraftAttachments", "CreateDraftMessage", "DeleteAttachment", "DeleteMessage", "DownloadAttachment",
        "FlagEmail", "ForwardMessage", "ForwardMessageWithFullThread", "GetAttachments", "GetMessage",
        "ReplyAllToMessage", "ReplyAllWithFullThread", "ReplyToMessage", "ReplyWithFullThread", "SearchMessages",
        "SearchMessagesQueryParameters", "SendDraftMessage", "SendEmailWithAttachments", "UpdateDraft",
        "UpdateMessage", "UploadAttachment", "UploadLargeAttachment",
    )
]


@pytest.mark.parametrize(
    ("operation", "expected"),
    [
        ("send", "SendEmailWithAttachments"),
        ("reply", "ReplyToMessage"),
        ("tag", "UpdateMessage"),
        ("draft", "CreateDraftMessage"),
        ("send_draft", "SendDraftMessage"),
        ("delete", "DeleteMessage"),
    ],
)
def test_live_catalog_resolves_every_operation(operation, expected) -> None:
    assert workiq_tools.resolve_mail_operation(operation, LIVE_MAIL_TOOLS)["name"] == expected


def test_live_catalog_never_resolves_reply_to_reply_all() -> None:
    tools = [{"name": "ReplyAllToMessage"}, {"name": "ReplyWithFullThread"}]

    assert workiq_tools.resolve_mail_operation("reply", tools) is None


def test_builder_follows_flat_schema_with_string_recipients_and_required_check() -> None:
    tool = {
        "name": "SendEmailWithAttachments",
        "inputSchema": {
            "type": "object",
            "properties": {
                "to": {"type": "array", "items": {"type": "string"}},
                "subject": {"type": "string"},
                "body": {"type": "string"},
                "contentType": {"type": "string"},
                "attachments": {"type": "array"},
            },
            "required": ["to", "subject", "body"],
        },
    }

    arguments = workiq_tools.build_mail_arguments(
        tool, {"to": ["owner@contoso.com"], "subject": "S", "body_html": "<p>B</p>", "attachments": []}
    )

    assert arguments == {
        "to": ["owner@contoso.com"], "subject": "S", "body": "<p>B</p>", "contentType": "HTML", "attachments": [],
    }


def test_builder_shapes_graph_style_objects_and_nested_message() -> None:
    tool = {
        "name": "SendEmailWithAttachments",
        "inputSchema": {
            "properties": {
                "message": {
                    "type": "object",
                    "properties": {
                        "subject": {"type": "string"},
                        "body": {"type": "object", "properties": {"contentType": {}, "content": {}}},
                        "toRecipients": {
                            "type": "array",
                            "items": {"type": "object", "properties": {"emailAddress": {"type": "object"}}},
                        },
                    },
                    "required": ["subject", "toRecipients"],
                },
                "saveToSentItems": {"type": "boolean"},
            },
            "required": ["message"],
        },
    }

    arguments = workiq_tools.build_mail_arguments(
        tool, {"to": ["owner@contoso.com"], "subject": "S", "body_html": "<p>B</p>"}
    )

    assert arguments == {
        "message": {
            "subject": "S",
            "body": {"contentType": "HTML", "content": "<p>B</p>"},
            "toRecipients": [{"emailAddress": {"address": "owner@contoso.com"}}],
        }
    }


def test_builder_refuses_before_calling_when_required_input_is_unknown() -> None:
    tool = {"name": "ReplyToMessage", "inputSchema": {"properties": {"threadToken": {"type": "string"}}, "required": ["threadToken"]}}

    with pytest.raises(workiq_tools.MailArgumentsError, match="threadToken"):
        workiq_tools.build_mail_arguments(tool, {"message_id": "AAMk1", "comment": "hi"})


def test_invoke_uses_live_tool_and_schema(monkeypatch) -> None:
    calls = []
    tool = {
        "name": "SendEmailWithAttachments",
        "inputSchema": {"properties": {"toRecipients": {"type": "array", "items": {"type": "string"}}, "subject": {}, "body": {}}},
    }

    async def fake_list(_server):
        return [tool, {"name": "SendDraftMessage"}]

    async def fake_call(server, name, arguments):
        calls.append((name, arguments))
        return {"content": [{"type": "text", "text": "sent"}]}

    monkeypatch.setattr(workiq_tools, "_list_tools", fake_list)
    monkeypatch.setattr(workiq_tools, "_call_tool", fake_call)

    asyncio.run(workiq_tools.invoke_mail_operation("send", {"to": ["a@b.com"], "subject": "S", "body_html": "B"}))

    assert calls == [("SendEmailWithAttachments", {"toRecipients": ["a@b.com"], "subject": "S", "body": "B"})]


def test_failed_draft_send_deletes_the_draft(monkeypatch) -> None:
    from tools.workiq_tools import MailToolNotFound

    adapter, calls = _FakeAdapter(), []
    controller = _controller(adapter, calls, monkeypatch)

    async def fake_run(_context, operation, values):
        calls.append((operation, values))
        if operation == "send":
            raise MailToolNotFound("no send tool")
        if operation == "draft":
            return {"result": {"content": [{"type": "text", "text": "Draft created: AAMkAGDraft" + "x" * 70}]}}
        if operation == "send_draft":
            raise RuntimeError("send failed")
        return {"result": {}}

    monkeypatch.setattr(controller, "_run_mail_operation", fake_run)
    proposal = PendingProposal(
        code="ABC234", email_id="e", email_reference={}, escalation_target="owner@partner-test.com",
        decision=_decision(escalate=True),
    )

    with pytest.raises(RuntimeError, match="send failed"):
        asyncio.run(controller._escalate(_FakeContext(_email_activity()), proposal))

    operations = [operation for operation, _ in calls]
    assert operations == ["send", "draft", "send_draft", "delete"]
    assert calls[-1][1]["message_id"].startswith("AAMkAGDraft")


def test_describe_shape_never_includes_values() -> None:
    shape = email_triage.describe_shape({"content": [{"type": "text", "text": "secret body"}], "isError": False})

    assert "secret body" not in str(shape)
    assert shape == {"content": [{"type": "str", "text": "str"}, "len=1"], "isError": "bool"}
