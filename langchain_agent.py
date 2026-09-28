"""LangChain orchestration for LangchainOA."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

from langchain.agents import create_agent
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from aoai_model import create_chat_model
from tools import ALL_TOOLS
from tools.workiq_tools import reset_workiq_context, set_workiq_context

logger = logging.getLogger(__name__)
PROMPT_PATH = Path(__file__).parent / "agent-prompt.txt"
MAX_HISTORY_MESSAGES = 16
MAX_CONVERSATIONS = 500
HISTORY_TTL_SECONDS = 24 * 3600

_agent: Any = None
_prompt_mtime = -1.0
_agent_lock = asyncio.Lock()
_history_lock = asyncio.Lock()
# conversation_id -> (last_used, messages); ordered oldest to newest use.
_histories: OrderedDict[str, tuple[float, list[BaseMessage]]] = OrderedDict()
_conversation_locks: dict[str, asyncio.Lock] = {}


async def run_agent(
    user_message: str,
    conversation_id: str,
    workiq_token: dict[str, str] | None = None,
    tenant_id: str | None = None,
    consumer_id: str | None = None,
    environment_id: str | None = None,
) -> str:
    """Run one user turn with isolated conversation history and Work IQ context."""

    agent = await _get_agent()
    # Serialize turns per conversation so overlapping turns cannot overwrite each other.
    async with await _conversation_lock(conversation_id):
        history = await _load_history(conversation_id)

        resets = set_workiq_context(workiq_token, tenant_id, consumer_id, environment_id)
        try:
            result = await agent.ainvoke({"messages": [*history, HumanMessage(content=user_message)]})
        finally:
            reset_workiq_context(resets)

        messages = result.get("messages", []) if isinstance(result, dict) else []
        if messages:
            await _save_history(conversation_id, trim_history(messages))
    return _last_text(messages) or "I completed the request but did not receive a text response."


def trim_history(messages: list[BaseMessage], limit: int = MAX_HISTORY_MESSAGES) -> list[BaseMessage]:
    """Keep recent messages, starting on a human turn so no tool result is orphaned."""

    start = max(0, len(messages) - limit)
    for index in range(start, len(messages)):
        if isinstance(messages[index], HumanMessage):
            return list(messages[index:])
    for index in range(start - 1, -1, -1):
        if isinstance(messages[index], HumanMessage):
            return list(messages[index:])
    return []


async def _conversation_lock(conversation_id: str) -> asyncio.Lock:
    async with _history_lock:
        lock = _conversation_locks.get(conversation_id)
        if lock is None:
            lock = _conversation_locks[conversation_id] = asyncio.Lock()
        return lock


async def _load_history(conversation_id: str) -> list[BaseMessage]:
    async with _history_lock:
        _evict_expired(time.time())
        entry = _histories.get(conversation_id)
        if not entry or time.time() - entry[0] > HISTORY_TTL_SECONDS:
            return []
        _histories.move_to_end(conversation_id)
        return list(entry[1])


async def _save_history(conversation_id: str, messages: list[BaseMessage]) -> None:
    async with _history_lock:
        _histories[conversation_id] = (time.time(), messages)
        _histories.move_to_end(conversation_id)
        while len(_histories) > MAX_CONVERSATIONS:
            oldest, _ = _histories.popitem(last=False)
            _drop_idle_lock(oldest)


def _evict_expired(now: float) -> None:
    while _histories:
        oldest, (last_used, _messages) = next(iter(_histories.items()))
        if now - last_used <= HISTORY_TTL_SECONDS:
            break
        _histories.popitem(last=False)
        _drop_idle_lock(oldest)


def _drop_idle_lock(conversation_id: str) -> None:
    lock = _conversation_locks.get(conversation_id)
    if lock is not None and not lock.locked():
        del _conversation_locks[conversation_id]


async def _get_agent() -> Any:
    """Create or hot-reload the LangChain agent when the prompt file changes."""

    global _agent, _prompt_mtime
    current_mtime = PROMPT_PATH.stat().st_mtime
    if _agent is not None and current_mtime == _prompt_mtime:
        return _agent

    async with _agent_lock:
        current_mtime = PROMPT_PATH.stat().st_mtime
        if _agent is not None and current_mtime == _prompt_mtime:
            return _agent

        _agent = create_agent(
            model=create_chat_model(),
            tools=ALL_TOOLS,
            system_prompt=PROMPT_PATH.read_text(encoding="utf-8"),
            name="LangchainOA",
        )
        _prompt_mtime = current_mtime
        logger.info("LangchainOA initialized with %d tools", len(ALL_TOOLS))
        return _agent


def _last_text(messages: list[Any]) -> str:
    for message in reversed(messages):
        if isinstance(message, AIMessage):
            text = _message_text(message)
            if text:
                return text
    return ""


def _message_text(message: AIMessage) -> str:
    """Extract text from string and provider-native structured content blocks."""

    content = message.content
    if isinstance(content, str):
        return content.strip()

    parts: list[str] = []
    for block in content if isinstance(content, list) else [content]:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and str(block.get("type") or "").lower() in {
            "text",
            "output_text",
            "message",
        }:
            value = block.get("text") or block.get("content") or block.get("value")
            if isinstance(value, str):
                parts.append(value)

    return "\n".join(part.strip() for part in parts if part and part.strip()).strip()
