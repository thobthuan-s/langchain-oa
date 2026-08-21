"""Thread-safe cache for per-turn Agent 365 observability tokens."""

from __future__ import annotations

from threading import Lock

_tokens: dict[str, str] = {}
_lock = Lock()


def cache_agentic_token(tenant_id: str, agent_id: str, token: str) -> None:
    """Cache a delegated observability token for one runtime agent identity."""

    if not (tenant_id and agent_id and token):
        return
    with _lock:
        _tokens[f"{tenant_id}:{agent_id}"] = token


def get_cached_agentic_token(agent_id: str, tenant_id: str) -> str | None:
    """Resolve a cached token using the callback order expected by the exporter."""

    with _lock:
        return _tokens.get(f"{tenant_id}:{agent_id}")


def clear_token_cache() -> None:
    """Clear cached tokens during tests or process shutdown."""

    with _lock:
        _tokens.clear()