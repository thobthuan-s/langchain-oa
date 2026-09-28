import asyncio

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

import langchain_agent


def _tool_turn(index: int) -> list:
    return [
        HumanMessage(content=f"question {index}"),
        AIMessage(content="", tool_calls=[{"id": f"call-{index}", "name": "list_resources", "args": {}}]),
        ToolMessage(content="[]", tool_call_id=f"call-{index}"),
        AIMessage(content=f"answer {index}"),
    ]


def test_trim_never_starts_with_an_orphaned_tool_message() -> None:
    messages = [message for index in range(6) for message in _tool_turn(index)]

    trimmed = langchain_agent.trim_history(messages, limit=6)

    assert isinstance(trimmed[0], HumanMessage)
    assert [message.type for message in trimmed] == ["human", "ai", "tool", "ai"]


def test_trim_keeps_a_long_single_turn_whole() -> None:
    turn = [HumanMessage(content="q")] + [
        message
        for index in range(5)
        for message in (
            AIMessage(content="", tool_calls=[{"id": f"c{index}", "name": "t", "args": {}}]),
            ToolMessage(content="x", tool_call_id=f"c{index}"),
        )
    ]

    assert langchain_agent.trim_history(turn, limit=4) == turn


def _fake_agent(delay: float = 0.0):
    class _Agent:
        async def ainvoke(self, payload):
            await asyncio.sleep(delay)
            messages = payload["messages"]
            return {"messages": [*messages, AIMessage(content=f"reply to {messages[-1].content}")]}

    return _Agent()


def _reset_state(monkeypatch, agent) -> None:
    monkeypatch.setattr(langchain_agent, "_histories", langchain_agent.OrderedDict())
    monkeypatch.setattr(langchain_agent, "_conversation_locks", {})

    async def get_agent():
        return agent

    monkeypatch.setattr(langchain_agent, "_get_agent", get_agent)


def test_concurrent_turns_in_one_conversation_keep_both_exchanges(monkeypatch) -> None:
    _reset_state(monkeypatch, _fake_agent(delay=0.01))

    async def scenario():
        await asyncio.gather(
            langchain_agent.run_agent("first", "conv"),
            langchain_agent.run_agent("second", "conv"),
        )
        return langchain_agent._histories["conv"][1]

    history = asyncio.run(scenario())

    assert [message.content for message in history if isinstance(message, HumanMessage)] == ["first", "second"]


def test_history_is_bounded_and_expires(monkeypatch) -> None:
    _reset_state(monkeypatch, _fake_agent())
    monkeypatch.setattr(langchain_agent, "MAX_CONVERSATIONS", 2)

    async def scenario():
        for conversation in ("a", "b", "c"):
            await langchain_agent.run_agent("hi", conversation)
        kept = list(langchain_agent._histories)
        langchain_agent._histories["b"] = (0.0, langchain_agent._histories["b"][1])
        expired = await langchain_agent._load_history("b")
        return kept, expired

    kept, expired = asyncio.run(scenario())

    assert kept == ["b", "c"]
    assert expired == []
    assert "a" not in langchain_agent._conversation_locks


def test_history_survives_a_restart_through_storage(monkeypatch) -> None:
    from microsoft_agents.hosting.core import MemoryStorage

    storage = MemoryStorage()
    _reset_state(monkeypatch, _fake_agent())
    langchain_agent.configure_history_storage(storage)
    try:
        asyncio.run(langchain_agent.run_agent("before restart", "conv-r"))
        # A restart loses the process cache but not the storage.
        monkeypatch.setattr(langchain_agent, "_histories", langchain_agent.OrderedDict())
        history = asyncio.run(langchain_agent._load_history("conv-r"))
    finally:
        langchain_agent.configure_history_storage(None)

    assert [message.content for message in history] == ["before restart", "reply to before restart"]


def test_tool_call_messages_round_trip_through_storage(monkeypatch) -> None:
    from microsoft_agents.hosting.core import MemoryStorage

    storage = MemoryStorage()
    langchain_agent.configure_history_storage(storage)
    monkeypatch.setattr(langchain_agent, "_histories", langchain_agent.OrderedDict())
    try:
        asyncio.run(langchain_agent._save_history("conv-t", _tool_turn(1)))
        monkeypatch.setattr(langchain_agent, "_histories", langchain_agent.OrderedDict())
        restored = asyncio.run(langchain_agent._load_history("conv-t"))
    finally:
        langchain_agent.configure_history_storage(None)

    assert [message.type for message in restored] == ["human", "ai", "tool", "ai"]
    assert restored[1].tool_calls[0]["id"] == "call-1"
    assert restored[2].tool_call_id == "call-1"


def test_storage_failure_does_not_break_the_turn(monkeypatch) -> None:
    class _BrokenStorage:
        async def read(self, *_args, **_kwargs):
            raise RuntimeError("403")

        async def write(self, *_args, **_kwargs):
            raise RuntimeError("403")

    _reset_state(monkeypatch, _fake_agent())
    langchain_agent.configure_history_storage(_BrokenStorage())
    try:
        reply = asyncio.run(langchain_agent.run_agent("hello", "conv-b"))
    finally:
        langchain_agent.configure_history_storage(None)

    assert reply == "reply to hello"
