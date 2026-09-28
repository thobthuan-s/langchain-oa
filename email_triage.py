"""Email triage for LangchainOA: classify incoming mail, propose actions, execute after approval.

Flow:
1. Agent 365 delivers an email notification for mail sent to the agent's mailbox.
2. The model classifies the email into a structured ``TriageDecision``. It has no tools.
3. A deterministic policy constrains the decision. Tags are applied immediately.
4. Replies and escalations are held as a proposal and posted to the approver in Teams.
5. The approver answers ``approve``, ``reject``, or ``edit`` with the one-time code.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import logging
import re
import secrets
import time
from collections.abc import Awaitable, Callable
from enum import Enum
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from config import settings

logger = logging.getLogger(__name__)

TRIAGE_PROMPT_PATH = Path(__file__).parent / "triage-prompt.txt"
RESEARCH_PROMPT_PATH = Path(__file__).parent / "triage-research-prompt.txt"
CUSTOMER_PROMPT_PATH = Path(__file__).parent / "triage-customer-prompt.txt"
_RESEARCH_CATEGORIES = frozenset({"question", "action_required", "meeting_request"})
_RESEARCH_OK_FLAGS = frozenset({"external_sender", "commercial_commitment"})
TAG_PREFIX = "Triage"
PENDING_COMMAND = "/pending"
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_CODE_LENGTH = 6
_MAX_SUMMARY_CHARS = 600
_MAX_REPLY_CHARS = 4000
_MAX_REASON_CHARS = 300
_EXCERPT_CHARS = 600
_SEEN_TTL_SECONDS = 7 * 24 * 3600
_COMMAND_RE = re.compile(
    rf"^\s*(approve|reject|edit)\s+([{_CODE_ALPHABET}]{{{_CODE_LENGTH}}})\b\s*:?\s*(.*)$",
    re.IGNORECASE | re.DOTALL,
)


class TriageCategory(str, Enum):
    ACTION_REQUIRED = "action_required"
    QUESTION = "question"
    MEETING_REQUEST = "meeting_request"
    FYI = "fyi"
    ESCALATION = "escalation"
    SPAM_OR_PHISHING = "spam_or_phishing"


class TriagePriority(str, Enum):
    HIGH = "high"
    NORMAL = "normal"
    LOW = "low"


ALLOWED_RISK_FLAGS = frozenset(
    {
        "prompt_injection",
        "external_sender",
        "suspicious_link",
        "credential_request",
        "payment_request",
        "sensitive_data",
        "auto_reply",
        "commercial_commitment",
    }
)
_NO_REPLY_FLAGS = frozenset({"prompt_injection", "credential_request", "payment_request", "auto_reply"})


class TriageDecision(BaseModel):
    """Structured model output for one email."""

    category: TriageCategory
    priority: TriagePriority
    summary: str = Field(description="One or two sentences describing the email.")
    needs_reply: bool
    reply_text: str = Field(default="", description="Plain-text reply, empty when needs_reply is false.")
    escalate: bool
    escalation_reason: str = ""
    risk_flags: list[str] = Field(default_factory=list)


class ResearchedReply(BaseModel):
    """Structured output of the tool-grounded reply research step."""

    reply_text: str = Field(description="Plain-text reply grounded in retrieved facts.")
    sources: list[str] = Field(default_factory=list, description="Documents or resources relied on.")
    unresolved: list[str] = Field(default_factory=list, description="Questions that could not be answered.")


ProposalStatus = Literal["pending", "processing", "approved", "rejected", "failed", "closed"]


class PendingProposal(BaseModel):
    """A triaged email and the outbound actions waiting for approval."""

    code: str
    status: ProposalStatus = "pending"
    created_at: float = Field(default_factory=time.time)
    email_id: str
    email_reference: dict[str, Any]
    sender_address: str = ""
    sender_name: str = ""
    external: bool = False
    excerpt: str = ""
    decision: TriageDecision
    tags: list[str] = Field(default_factory=list)
    tags_applied: bool = False
    tag_error: str = ""
    customer_label: str = ""
    escalation_target: str = ""
    delivered: bool = False
    purview_blocked: bool = False
    reply_sources: list[str] = Field(default_factory=list)
    reply_unresolved: list[str] = Field(default_factory=list)
    research_note: str = ""
    result: str = ""

    @property
    def has_outbound_actions(self) -> bool:
        return bool(self.decision.needs_reply or self.decision.escalate)


class ApprovalCommand(BaseModel):
    verb: Literal["approve", "reject", "edit"]
    code: str
    text: str = ""


# --- Pure helpers -----------------------------------------------------------------


class _TextExtractor(HTMLParser):
    _SKIP = {"script", "style", "head", "title"}
    _BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "hr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self.parts.append(data)


def html_to_text(value: str | None, max_chars: int | None = None) -> str:
    """Convert an email HTML body to bounded plain text."""

    if not value:
        return ""
    parser = _TextExtractor()
    parser.feed(value)
    parser.close()
    lines = [re.sub(r"[ \t\r\f\v\u00a0]+", " ", line).strip() for line in "".join(parser.parts).split("\n")]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    limit = max_chars if max_chars is not None else settings.email_triage_max_body_chars
    return text[:limit]


def text_to_email_html(text: str) -> str:
    """Render approved plain text as escaped HTML paragraphs."""

    paragraphs = [block.strip() for block in re.split(r"\n\s*\n", text.strip()) if block.strip()]
    return "".join(
        f"<p>{'<br>'.join(html.escape(line) for line in block.splitlines())}</p>" for block in paragraphs
    )


def email_domain(address: str) -> str:
    return address.rsplit("@", 1)[-1].strip().lower() if "@" in address else ""


def is_external_sender(sender_address: str, agent_address: str) -> bool:
    """Treat the agent's own domain plus configured domains as internal."""

    internal = {d.strip().lower() for d in settings.email_triage_internal_domains.split(",") if d.strip()}
    agent_domain = email_domain(agent_address)
    if agent_domain:
        internal.add(agent_domain)
    domain = email_domain(sender_address)
    return not domain or domain not in internal


def apply_policy(decision: TriageDecision, external: bool) -> TriageDecision:
    """Constrain model output with deterministic rules before anything is proposed."""

    flags = sorted({flag.strip().lower() for flag in decision.risk_flags} & ALLOWED_RISK_FLAGS)
    if external and "external_sender" not in flags:
        flags.append("external_sender")

    needs_reply = decision.needs_reply
    reply_text = decision.reply_text.strip()[:_MAX_REPLY_CHARS]
    if decision.category == TriageCategory.SPAM_OR_PHISHING or _NO_REPLY_FLAGS & set(flags):
        needs_reply = False
    if not reply_text:
        needs_reply = False
    if not needs_reply:
        reply_text = ""

    escalate = decision.escalate or decision.category == TriageCategory.ESCALATION
    if "prompt_injection" in flags and decision.category != TriageCategory.SPAM_OR_PHISHING:
        escalate = True
    reason = decision.escalation_reason.strip()
    if "commercial_commitment" in flags:
        escalate = True
        reason = reason or "Commercial request (pricing, credit, cancellation, or contract change) needs a human."
    return decision.model_copy(
        update={
            "summary": decision.summary.strip()[:_MAX_SUMMARY_CHARS],
            "needs_reply": needs_reply,
            "reply_text": reply_text,
            "escalate": escalate,
            "escalation_reason": reason[:_MAX_REASON_CHARS] if escalate else "",
            "risk_flags": sorted(flags),
        }
    )


def apply_customer_policy(decision: TriageDecision, customer: Any, customer_mode: bool) -> TriageDecision:
    """A known customer domain with an unlisted sender gets no reply and goes to the account owner."""

    if not customer_mode or customer is None or customer.verified:
        return decision
    return decision.model_copy(
        update={
            "needs_reply": False,
            "reply_text": "",
            "escalate": True,
            "escalation_reason": "Sender uses a customer domain but is not a listed contact.",
        }
    )


def triage_tags(decision: TriageDecision) -> list[str]:
    return [f"{TAG_PREFIX}/{decision.category.value}", f"{TAG_PREFIX}/{decision.priority.value}"]


def new_code() -> str:
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LENGTH))


def parse_approval_command(text: str | None) -> ApprovalCommand | None:
    match = _COMMAND_RE.match(text or "")
    if not match:
        return None
    return ApprovalCommand(verb=match.group(1).lower(), code=match.group(2).upper(), text=match.group(3).strip())


def approver_ids() -> set[str]:
    return {value.strip().lower() for value in settings.email_triage_approvers.split(",") if value.strip()}


def approver_key(account: Any) -> str:
    """Return the configured approver identifier that matches this Teams account, if any."""

    allowed = approver_ids()
    for candidate in (getattr(account, "aad_object_id", None), getattr(account, "id", None)):
        if candidate and str(candidate).lower() in allowed:
            return str(candidate).lower()
    return ""


def render_proposal(proposal: PendingProposal) -> str:
    decision = proposal.decision
    sender = proposal.sender_name or proposal.sender_address or "unknown sender"
    if proposal.sender_name and proposal.sender_address:
        sender = f"{proposal.sender_name} <{proposal.sender_address}>"
    lines = [
        f"**New email triaged** — code `{proposal.code}`" if proposal.has_outbound_actions else "**New email triaged**",
        f"- From: {sender}{' (external)' if proposal.external else ''}",
        f"- Category: {decision.category.value} · Priority: {decision.priority.value}",
    ]
    if proposal.customer_label:
        lines.insert(2, f"- Customer: {proposal.customer_label}")
    if decision.risk_flags:
        lines.append(f"- Risk flags: {', '.join(decision.risk_flags)}")
    lines.append(f"- Summary: {decision.summary or '(none)'}")
    if proposal.tags_applied:
        lines.append(f"- Tagged: {', '.join(proposal.tags)}")
    elif proposal.tag_error:
        lines.append(f"- Tagging failed: {proposal.tag_error}")
    if proposal.excerpt:
        lines.append(f"\n> {_quote(proposal.excerpt)}")

    if not proposal.has_outbound_actions:
        lines.append("\nNo reply or escalation proposed. Nothing to approve.")
        return "\n".join(lines)

    lines.append("\n**Proposed actions**")
    step = 1
    if decision.needs_reply:
        lines.append(f"{step}. Reply to the sender:\n\n> {_quote(decision.reply_text)}")
        if proposal.reply_sources:
            lines.append("   Sources: " + "; ".join(proposal.reply_sources))
        if proposal.reply_unresolved:
            lines.append("   Not answered: " + "; ".join(proposal.reply_unresolved))
        if proposal.research_note:
            lines.append(f"   Note: {proposal.research_note}")
        step += 1
    if decision.escalate:
        target = (
            proposal.escalation_target
            or settings.email_triage_escalation_address
            or "(no EMAIL_TRIAGE_ESCALATION_ADDRESS configured)"
        )
        lines.append(f"{step}. Escalate to {target}: {decision.escalation_reason or 'human decision needed'}")
    lines.append(
        f"\nAnswer `approve {proposal.code}`, `reject {proposal.code}`, "
        f"or `edit {proposal.code}: <your reply text>`."
    )
    return "\n".join(lines)


def _quote(text: str) -> str:
    return text.strip().replace("\n", "\n> ")


# --- Storage ----------------------------------------------------------------------


class _JsonItem:
    """Minimal StoreItem so any Agents SDK Storage (memory, blob, Cosmos) can be used."""

    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data

    def store_item_to_json(self) -> dict[str, Any]:
        return self.data

    @staticmethod
    def from_json_to_store_item(json_data: dict[str, Any]) -> "_JsonItem":
        return _JsonItem(dict(json_data))


class TriageStore:
    """Proposal, dedupe, and approver-reference state on top of an Agents SDK Storage."""

    _INDEX_KEY = "emailtriage-index"
    _APPROVERS_KEY = "emailtriage-approver-references"

    def __init__(self, storage: Any) -> None:
        self._storage = storage
        self._lock = asyncio.Lock()

    async def mark_seen(self, email_id: str) -> bool:
        """Record an email id; return False when it was already processed recently."""

        key = f"emailtriage-seen-{_hash(email_id)}"
        async with self._lock:
            existing = await self._read(key)
            if existing and time.time() - float(existing.get("at", 0)) < _SEEN_TTL_SECONDS:
                return False
            await self._write(key, {"at": time.time()})
            return True

    async def save(self, proposal: PendingProposal) -> None:
        async with self._lock:
            await self._write(self._proposal_key(proposal.code), proposal.model_dump(mode="json"))
            index = (await self._read(self._INDEX_KEY)) or {"codes": []}
            if proposal.code not in index["codes"]:
                index["codes"] = [*index["codes"], proposal.code][-500:]
                await self._write(self._INDEX_KEY, index)

    async def get(self, code: str) -> PendingProposal | None:
        data = await self._read(self._proposal_key(code))
        return PendingProposal.model_validate(data) if data else None

    async def claim(self, code: str) -> PendingProposal | None:
        """Move a live proposal from pending to processing exactly once."""

        async with self._lock:
            data = await self._read(self._proposal_key(code.upper()))
            if not data:
                return None
            proposal = PendingProposal.model_validate(data)
            if proposal.status != "pending" or _expired(proposal):
                return None
            proposal.status = "processing"
            await self._write(self._proposal_key(proposal.code), proposal.model_dump(mode="json"))
            return proposal

    async def mark_delivered(self, code: str) -> None:
        async with self._lock:
            data = await self._read(self._proposal_key(code))
            if data and not data.get("delivered"):
                data["delivered"] = True
                await self._write(self._proposal_key(code), data)

    async def list_codes(self) -> list[str]:
        index = await self._read(self._INDEX_KEY)
        return list(index.get("codes", [])) if index else []

    async def pending(self) -> list[PendingProposal]:
        result = []
        for code in await self.list_codes():
            proposal = await self.get(code)
            if proposal and proposal.status == "pending" and not _expired(proposal):
                result.append(proposal)
        return result

    async def undelivered(self) -> list[PendingProposal]:
        result = []
        for code in await self.list_codes():
            proposal = await self.get(code)
            if proposal and not proposal.delivered and proposal.status in {"pending", "closed"}:
                result.append(proposal)
        return result

    async def save_approver_reference(self, key: str, reference: dict[str, Any]) -> None:
        async with self._lock:
            data = (await self._read(self._APPROVERS_KEY)) or {}
            data[key] = reference
            await self._write(self._APPROVERS_KEY, data)

    async def approver_references(self) -> dict[str, dict[str, Any]]:
        data = await self._read(self._APPROVERS_KEY) or {}
        allowed = approver_ids()
        return {key: value for key, value in data.items() if key in allowed}

    @staticmethod
    def _proposal_key(code: str) -> str:
        return f"emailtriage-proposal-{code.upper()}"

    async def _read(self, key: str) -> dict[str, Any] | None:
        items = await self._storage.read([key], target_cls=_JsonItem)
        item = items.get(key)
        return item.data if item else None

    async def _write(self, key: str, data: dict[str, Any]) -> None:
        await self._storage.write({key: _JsonItem(data)})


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:40]


def _expired(proposal: PendingProposal) -> bool:
    return time.time() - proposal.created_at > settings.email_triage_approval_ttl_hours * 3600


# --- Model ------------------------------------------------------------------------

_classifier: Any = None


async def classify_email(sender_name: str, sender_address: str, external: bool, body: str) -> TriageDecision:
    """Classify one email with structured output and no tools."""

    from langchain_core.messages import HumanMessage, SystemMessage

    global _classifier
    if _classifier is None:
        from aoai_model import create_chat_model

        _classifier = create_chat_model().with_structured_output(TriageDecision)

    content = (
        f"Sender: {sender_name} <{sender_address}>\n"
        f"Sender is {'external' if external else 'internal'} to the organization.\n\n"
        f"<email_body>\n{body}\n</email_body>"
    )
    result = await _classifier.ainvoke(
        [SystemMessage(content=TRIAGE_PROMPT_PATH.read_text(encoding="utf-8")), HumanMessage(content=content)]
    )
    if isinstance(result, TriageDecision):
        return result
    return TriageDecision.model_validate(result)


_researcher: Any = None
_research_model: Any = None


def should_research(
    decision: TriageDecision,
    external: bool,
    customer: Any = None,
    customer_mode: bool = False,
) -> tuple[bool, str]:
    """Decide whether to ground the draft with tools, and why not when skipped.

    In customer mode only verified contacts are researched, and only with tools
    bound to their account, so the external-sender switch does not apply.
    """

    if not (settings.email_triage_research and decision.needs_reply):
        return False, ""
    if decision.category.value not in _RESEARCH_CATEGORIES:
        return False, ""
    if decision.risk_flags and set(decision.risk_flags) - _RESEARCH_OK_FLAGS:
        return False, "Draft not researched because the email has risk flags."
    if customer_mode:
        if customer is None:
            return False, "Draft not researched: sender is not in the customer records."
        if not customer.verified:
            return False, "Draft not researched: sender is not a listed contact."
        return True, ""
    if external and not settings.email_triage_research_external:
        return False, "Draft not researched: external sender (EMAIL_TRIAGE_RESEARCH_EXTERNAL=false)."
    return True, ""


async def research_reply(
    sender_name: str,
    sender_address: str,
    body: str,
    decision: TriageDecision,
    customer: Any = None,
) -> ResearchedReply:
    """Draft a reply with structured output.

    Without a customer, the general read-only tool set is used. With a customer,
    a fresh agent gets only the lookup tools bound to that customer's account.
    """

    from langchain.agents import create_agent
    from langchain_core.messages import HumanMessage

    from aoai_model import create_chat_model

    global _researcher, _research_model
    if _research_model is None:
        _research_model = create_chat_model()
    if customer is not None:
        from customer_records import build_customer_tools

        agent = create_agent(
            model=_research_model,
            tools=build_customer_tools(customer),
            system_prompt=CUSTOMER_PROMPT_PATH.read_text(encoding="utf-8"),
            response_format=ResearchedReply,
            name="LangchainOA-customer-research",
        )
    else:
        if _researcher is None:
            from tools import ALL_TOOLS

            _researcher = create_agent(
                model=_research_model,
                tools=ALL_TOOLS,
                system_prompt=RESEARCH_PROMPT_PATH.read_text(encoding="utf-8"),
                response_format=ResearchedReply,
                name="LangchainOA-triage-research",
            )
        agent = _researcher
    customer_line = f"Customer: {customer.describe()}\n" if customer is not None else ""
    content = (
        f"{customer_line}Triage summary: {decision.summary}\n"
        f"Initial draft (not yet verified):\n{decision.reply_text}\n\n"
        f"Sender: {sender_name} <{sender_address}>\n\n"
        f"<email_body>\n{body}\n</email_body>"
    )
    result = await agent.ainvoke(
        {"messages": [HumanMessage(content=content)]},
        {"recursion_limit": 12},
    )
    structured = result.get("structured_response") if isinstance(result, dict) else None
    if isinstance(structured, ResearchedReply):
        return structured
    if structured is None:
        raise ValueError("Research step returned no structured reply")
    return ResearchedReply.model_validate(structured)


# --- Controller -------------------------------------------------------------------

TokenExchange = Callable[[Any], Awaitable[dict[str, str]]]
PurviewExchange = Callable[[Any], Awaitable[str | None]]


class EmailTriageController:
    """Orchestrates email notifications and Teams approvals for one host."""

    def __init__(
        self,
        adapter: Any,
        storage: Any,
        exchange_workiq_tokens: TokenExchange,
        exchange_purview_token: PurviewExchange,
        conversation_key: Callable[[Any], str],
        customer_records: Any = None,
        exchange_graph_token: PurviewExchange | None = None,
    ) -> None:
        self.adapter = adapter
        self.store = TriageStore(storage)
        self._exchange_workiq_tokens = exchange_workiq_tokens
        self._exchange_purview_token = exchange_purview_token
        self._conversation_key = conversation_key
        self.customer_records = customer_records
        self._exchange_graph_token = exchange_graph_token

    # Email notification turn ------------------------------------------------------

    async def handle_email(self, context: Any, notification: Any) -> None:
        from observability import observability_context, runtime_identity
        from purview import begin_purview_turn, capture_purview_response, should_enforce_block

        activity = context.activity
        email = getattr(notification, "email", None)
        if not email or not email.id:
            logger.warning("Email notification without an email reference; ignoring")
            return

        sender = activity.from_property
        sender_address = str(getattr(sender, "id", "") or "")
        sender_name = str(getattr(sender, "name", "") or "")
        agent_address = str(getattr(activity.recipient, "id", "") or "")
        if sender_address and sender_address.lower() == agent_address.lower():
            logger.info("Ignoring email sent by the agent itself")
            return
        if not await self.store.mark_seen(email.id):
            logger.info("Ignoring duplicate email notification")
            return

        conversation_id = self._conversation_key(email.conversation_id or activity.conversation.id)
        body = html_to_text(email.html_body or activity.text or "")
        external = is_external_sender(sender_address, agent_address)
        _tenant_id, runtime_agent_id = runtime_identity(context)
        customer_mode = self.customer_records is not None
        customer, customer_label = (
            await self._resolve_customer(context, sender_address) if customer_mode else (None, "")
        )

        with observability_context(context, conversation_id):
            purview_turn = await begin_purview_turn(
                await self._exchange_purview_token(context), body, conversation_id, runtime_agent_id
            )
            if purview_turn and should_enforce_block(purview_turn.prompt_decision):
                decision = TriageDecision(
                    category=TriageCategory.ESCALATION,
                    priority=TriagePriority.HIGH,
                    summary="Email content was blocked by Microsoft Purview policy and was not analyzed.",
                    needs_reply=False,
                    escalate=False,
                    risk_flags=["sensitive_data"],
                )
                proposal = self._new_proposal(activity, email.id, sender_address, sender_name, external, "", decision)
                proposal.purview_blocked = True
                proposal.status = "closed"
            else:
                try:
                    decision = apply_policy(
                        await classify_email(sender_name, sender_address, external, body), external
                    )
                    decision = apply_customer_policy(decision, customer, customer_mode)
                except Exception as exc:  # noqa: BLE001
                    logger.error("Email classification failed: %s", exc, exc_info=True)
                    decision = TriageDecision(
                        category=TriageCategory.ESCALATION,
                        priority=TriagePriority.NORMAL,
                        summary="Automatic triage failed. Review this email manually.",
                        needs_reply=False,
                        escalate=False,
                    )
                research, research_note = await self._ground_reply(
                    context, sender_name, sender_address, external, body, decision, customer, customer_mode
                )
                if research:
                    decision = decision.model_copy(update={"reply_text": research.reply_text.strip()[:_MAX_REPLY_CHARS]})
                if decision.reply_text and should_enforce_block(
                    await capture_purview_response(purview_turn, decision.reply_text)
                ):
                    decision = decision.model_copy(update={"needs_reply": False, "reply_text": ""})
                proposal = self._new_proposal(
                    activity, email.id, sender_address, sender_name, external, body[:_EXCERPT_CHARS], decision
                )
                proposal.research_note = research_note
                proposal.customer_label = customer_label
                if customer is not None and customer.account_owner:
                    proposal.escalation_target = customer.account_owner
                logger.info(
                    "Email triaged: code=%s account=%s verified=%s category=%s priority=%s needs_reply=%s "
                    "escalate=%s flags=%s external=%s researched=%s note=%s",
                    proposal.code,
                    customer.account_id if customer is not None else "-",
                    customer.verified if customer is not None else "-",
                    decision.category.value,
                    decision.priority.value,
                    decision.needs_reply,
                    decision.escalate,
                    ",".join(decision.risk_flags) or "-",
                    external,
                    research is not None,
                    research_note or "-",
                )
                if research:
                    proposal.reply_sources = [item.strip()[:300] for item in research.sources if item.strip()][:8]
                    proposal.reply_unresolved = [item.strip()[:300] for item in research.unresolved if item.strip()][:5]
                if not proposal.has_outbound_actions:
                    proposal.status = "closed"
                if settings.email_triage_auto_tag:
                    await self._apply_tags(context, proposal)

        await self.store.save(proposal)
        if await self._notify_approvers(context, render_proposal(proposal)):
            await self.store.mark_delivered(proposal.code)
        else:
            logger.warning(
                "Triage proposal %s is queued: no approver has chatted with the agent in Teams yet",
                proposal.code,
            )

    async def _ground_reply(
        self,
        context: Any,
        sender_name: str,
        sender_address: str,
        external: bool,
        body: str,
        decision: TriageDecision,
        customer: Any = None,
        customer_mode: bool = False,
    ) -> tuple[ResearchedReply | None, str]:
        """Return a tool-grounded reply, or None with a note explaining why it was skipped."""

        from tools.workiq_tools import reset_workiq_context, set_workiq_context

        wanted, note = should_research(decision, external, customer, customer_mode)
        if not wanted:
            return None, note
        recipient = context.activity.recipient
        resets = set_workiq_context(
            await self._exchange_workiq_tokens(context),
            _account_value(recipient, "tenant_id", "tenantId") or settings.tenant_id,
            _account_value(recipient, "agentic_app_id", "agenticAppId") or settings.workiq_consumer_id,
            settings.workiq_environment_id,
        )
        try:
            research = await asyncio.wait_for(
                research_reply(sender_name, sender_address, body, decision, customer),
                timeout=settings.email_triage_research_timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Reply research failed; keeping the unverified draft: %s", exc)
            return None, "Research failed; this draft is not verified against your data."
        finally:
            reset_workiq_context(resets)
        if not research.reply_text.strip():
            return None, "Research produced no reply; this draft is not verified against your data."
        return research, ""

    async def _resolve_customer(self, context: Any, sender_address: str) -> tuple[Any, str]:
        """Resolve the sender to one customer account; never raises."""

        from customer_records import resolve_customer

        try:
            token = await self._exchange_graph_token(context) if self._exchange_graph_token else None
            if not token:
                raise RuntimeError("no Graph token for the customer workbook")
            customer = resolve_customer(await self.customer_records.sheets(token), sender_address)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Customer records unavailable: %s", exc)
            return None, "Customer records unavailable; sender not checked."
        if customer is None:
            return None, "Not found in customer records."
        return customer, customer.describe()

    def _new_proposal(
        self,
        activity: Any,
        email_id: str,
        sender_address: str,
        sender_name: str,
        external: bool,
        excerpt: str,
        decision: TriageDecision,
    ) -> PendingProposal:
        reference = activity.get_conversation_reference()
        return PendingProposal(
            code=new_code(),
            email_id=email_id,
            email_reference=reference.model_dump(mode="json", by_alias=True, exclude_none=True),
            sender_address=sender_address,
            sender_name=sender_name,
            external=external,
            excerpt=excerpt,
            decision=decision,
            tags=triage_tags(decision),
        )

    async def _apply_tags(self, context: Any, proposal: PendingProposal) -> None:
        from tools.workiq_tools import tool_input_properties

        def arguments(tool_definition: dict[str, Any]) -> dict[str, Any]:
            properties = tool_input_properties(tool_definition)
            if "categories" not in properties and "message" in properties:
                return {"id": proposal.email_id, "message": {"categories": proposal.tags}}
            return {"id": proposal.email_id, "categories": proposal.tags}

        try:
            await self._run_mail_operation(context, "tag", arguments)
            proposal.tags_applied = True
        except Exception as exc:  # noqa: BLE001
            proposal.tag_error = str(exc)[:200]
            logger.warning("Email tagging failed: %s", exc)

    # Teams approval turn ----------------------------------------------------------

    async def handle_approver_message(self, context: Any) -> bool:
        """Handle approval commands. Return True when the turn was fully handled."""

        activity = context.activity
        text = (activity.text or "").strip()
        command = parse_approval_command(text)
        key = approver_key(activity.from_property)
        if not key:
            if command or text.lower() == PENDING_COMMAND:
                await context.send_activity("Only configured approvers can act on triaged email.")
                return True
            return False

        reference = activity.get_conversation_reference()
        await self.store.save_approver_reference(
            key, reference.model_dump(mode="json", by_alias=True, exclude_none=True)
        )

        if text.lower() == PENDING_COMMAND:
            await self._send_pending(context)
            return True
        if not command:
            await self._deliver_queued(context)
            return False

        proposal = await self.store.claim(command.code)
        if not proposal:
            await context.send_activity(
                f"No pending proposal `{command.code}`. It may have expired or already been handled."
            )
            return True
        if command.verb == "reject":
            proposal.status = "rejected"
            proposal.result = f"Rejected by {key}"
            await self.store.save(proposal)
            await context.send_activity(f"Rejected `{proposal.code}`. Nothing was sent.")
            return True
        if command.verb == "edit" and not command.text:
            proposal.status = "pending"
            await self.store.save(proposal)
            await context.send_activity(f"Add your reply text: `edit {proposal.code}: <text>`.")
            return True

        await self._execute(context, proposal, key, command.text if command.verb == "edit" else "")
        return True

    async def _execute(self, context: Any, proposal: PendingProposal, approver: str, edited_reply: str) -> None:
        decision = proposal.decision
        reply_text = edited_reply or (decision.reply_text if decision.needs_reply else "")
        outcomes: list[str] = []
        failed = False

        if reply_text:
            try:
                outcomes.append(await self._send_reply(context, proposal, reply_text[:_MAX_REPLY_CHARS]))
            except Exception as exc:  # noqa: BLE001
                failed = True
                logger.error("Approved reply failed for %s: %s", proposal.code, exc, exc_info=True)
                outcomes.append(f"Reply failed: {str(exc)[:200]}")
        if decision.escalate:
            try:
                outcomes.append(await self._escalate(context, proposal))
            except Exception as exc:  # noqa: BLE001
                failed = True
                logger.error("Approved escalation failed for %s: %s", proposal.code, exc, exc_info=True)
                outcomes.append(f"Escalation failed: {str(exc)[:200]}")
        if not outcomes:
            outcomes.append("No outbound action was proposed.")

        proposal.status = "failed" if failed else "approved"
        proposal.result = f"Approved by {approver}: " + " ".join(outcomes)
        await self.store.save(proposal)
        await context.send_activity(f"`{proposal.code}`: " + " ".join(outcomes))

    async def _send_reply(self, context: Any, proposal: PendingProposal, reply_text: str) -> str:
        """Reply in the original email thread, falling back to the Mail MCP reply tool."""

        from microsoft_agents.activity import ConversationReference
        from microsoft_agents_a365.notifications import EmailResponse

        try:
            reference = ConversationReference.model_validate(proposal.email_reference)
            continuation = reference.get_continuation_activity()
            if reference.activity_id:
                continuation.id = reference.activity_id
            response = EmailResponse.create_email_response_activity(text_to_email_html(reply_text))

            async def send(turn_context: Any) -> None:
                await turn_context.send_activity(response)

            await self.adapter.continue_conversation_with_claims(context.identity, continuation, send)
            return "Reply sent in the email thread."
        except Exception as exc:  # noqa: BLE001
            logger.warning("Email-channel reply failed, trying Mail MCP reply: %s", exc)

        await self._run_mail_operation(context, "reply", lambda _tool: {"id": proposal.email_id, "comment": reply_text})
        return "Reply sent through Mail."

    async def _escalate(self, context: Any, proposal: PendingProposal) -> str:
        from tools.workiq_tools import tool_input_properties

        target = (proposal.escalation_target or settings.email_triage_escalation_address).strip()
        if not target:
            return "Escalation skipped: no account owner or EMAIL_TRIAGE_ESCALATION_ADDRESS is set."
        decision = proposal.decision
        subject = f"[Escalation] Email from {proposal.sender_name or proposal.sender_address}"[:200]
        body = text_to_email_html(
            f"LangchainOA escalated an email (approval code {proposal.code}).\n\n"
            f"From: {proposal.sender_name} <{proposal.sender_address}>\n"
            f"{('Customer: ' + proposal.customer_label + chr(10)) if proposal.customer_label else ''}"
            f"Category: {decision.category.value}, priority: {decision.priority.value}\n"
            f"Reason: {decision.escalation_reason or 'human decision needed'}\n\n"
            f"Summary: {decision.summary}\n\nExcerpt:\n{proposal.excerpt}"
        )
        message = {
            "subject": subject,
            "body": {"contentType": "HTML", "content": body},
            "toRecipients": [{"emailAddress": {"address": target}}],
        }

        def arguments(tool_definition: dict[str, Any]) -> dict[str, Any]:
            properties = tool_input_properties(tool_definition)
            payload: dict[str, Any] = {}
            if "message" in properties or not properties:
                payload["message"] = message
            for field_name in ("subject", "body", "toRecipients"):
                if field_name in properties:
                    payload[field_name] = message[field_name]
            return payload

        await self._run_mail_operation(context, "send", arguments)
        return f"Escalated to {target}."

    # Shared -----------------------------------------------------------------------

    async def _run_mail_operation(
        self,
        context: Any,
        operation: str,
        arguments: Callable[[dict[str, Any]], dict[str, Any]],
    ) -> dict[str, Any]:
        from tools.workiq_tools import invoke_mail_operation, reset_workiq_context, set_workiq_context

        recipient = context.activity.recipient
        tokens = await self._exchange_workiq_tokens(context)
        resets = set_workiq_context(
            tokens,
            _account_value(recipient, "tenant_id", "tenantId") or settings.tenant_id,
            _account_value(recipient, "agentic_app_id", "agenticAppId") or settings.workiq_consumer_id,
            settings.workiq_environment_id,
        )
        try:
            return await invoke_mail_operation(operation, arguments)
        finally:
            reset_workiq_context(resets)

    async def _notify_approvers(self, context: Any, text: str) -> bool:
        from microsoft_agents.activity import ConversationReference

        delivered = False
        for key, data in (await self.store.approver_references()).items():
            try:
                reference = ConversationReference.model_validate(data)

                async def send(turn_context: Any) -> None:
                    await turn_context.send_activity(text)

                await self.adapter.continue_conversation_with_claims(
                    context.identity, reference.get_continuation_activity(), send
                )
                delivered = True
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not notify approver %s: %s", key, exc)
        return delivered

    async def _send_pending(self, context: Any) -> None:
        pending = await self.store.pending()
        if not pending:
            await context.send_activity("No triaged email is waiting for approval.")
            return
        for proposal in pending:
            await context.send_activity(render_proposal(proposal))
            await self.store.mark_delivered(proposal.code)

    async def _deliver_queued(self, context: Any) -> None:
        for proposal in await self.store.undelivered():
            if proposal.status == "pending" and _expired(proposal):
                continue
            await context.send_activity(render_proposal(proposal))
            await self.store.mark_delivered(proposal.code)


def _account_value(source: Any, *names: str) -> str:
    if source is None:
        return ""
    for name in names:
        value = getattr(source, name, None)
        if value:
            return str(value)
    return ""
