"""LangChain orchestration for LangchainOA."""

from __future__ import annotations

import asyncio
import logging
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
MAX_HISTORY_CONVERSATIONS = 1024

_agent: Any = None
_prompt_mtime = -1.0
_agent_lock = asyncio.Lock()
_history_lock = asyncio.Lock()
_histories: dict[str, list[BaseMessage]] = {}


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
    async with _history_lock:
        history = _histories.get(conversation_id, [])[-MAX_HISTORY_MESSAGES:]

    resets = set_workiq_context(workiq_token, tenant_id, consumer_id, environment_id)
    try:
        result = await agent.ainvoke({"messages": [*history, HumanMessage(content=user_message)]})
    finally:
        reset_workiq_context(resets)

    messages = result.get("messages", []) if isinstance(result, dict) else []
    if messages:
        async with _history_lock:
            _histories.pop(conversation_id, None)
            _histories[conversation_id] = messages[-MAX_HISTORY_MESSAGES:]
            while len(_histories) > MAX_HISTORY_CONVERSATIONS:
                _histories.pop(next(iter(_histories)))
    return _last_text(messages) or "I completed the request but did not receive a text response."


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
