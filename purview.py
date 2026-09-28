"""Optional Microsoft Purview content capture and DLP for LangchainOA.

LangChain owns the turn lifecycle, so this module calls the Graph
`dataSecurityAndGovernance` endpoints directly instead of wrapping the agent run.
Every failure degrades to "unavailable" so a Purview outage never breaks chat.
Tokens and evaluated content are never logged.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import httpx

from config import settings

logger = logging.getLogger(__name__)

PURVIEW_GRAPH_SCOPES = [
    "https://graph.microsoft.com/Content.Process.User",
    "https://graph.microsoft.com/ProtectionScopes.Compute.User",
    "https://graph.microsoft.com/ContentActivity.Write",
]

_GRAPH_ROOT = "https://graph.microsoft.com/v1.0/me/dataSecurityAndGovernance"
_scope_cache: dict[str, tuple[float, str | None, dict[str, str]]] = {}


@dataclass(frozen=True)
class PurviewDecision:
    """Sanitized result of one Purview content operation."""

    available: bool
    blocked: bool = False
    operation: str = ""
    execution_mode: str = ""
    status_code: int | None = None
    error: str = ""


@dataclass
class PurviewTurn:
    """Request-scoped correlation state and the delegated agentic-user token."""

    token: str = field(repr=False)
    correlation_id: str
    client_request_id: str
    user_id: str
    conversation_id: str
    agent_id: str
    next_sequence: int = 1
    prompt_decision: PurviewDecision = field(default_factory=lambda: PurviewDecision(available=False))


def is_purview_enabled() -> bool:
    return settings.enable_purview


def should_enforce_block(decision: PurviewDecision) -> bool:
    return bool(settings.purview_enforce_blocks and decision.blocked)


async def begin_purview_turn(
    token: str | None,
    prompt: str,
    conversation_id: str,
    agent_id: str,
) -> PurviewTurn | None:
    """Start a correlated turn and evaluate or capture its prompt."""

    if not (is_purview_enabled() and token and agent_id):
        return None
    turn = PurviewTurn(
        token=token,
        correlation_id=str(uuid4()),
        client_request_id=str(uuid4()),
        user_id=_jwt_claim(token, "oid"),
        conversation_id=conversation_id,
        agent_id=agent_id,
    )
    turn.prompt_decision = await _process_text(turn, prompt, "uploadText", 0, "Prompt")
    return turn


async def capture_purview_response(turn: PurviewTurn | None, response: str) -> PurviewDecision:
    """Evaluate or capture the final agent response."""

    if not turn:
        return PurviewDecision(available=False, operation="downloadText", error="Purview turn unavailable")
    sequence = turn.next_sequence
    turn.next_sequence += 1
    return await _process_text(turn, response, "downloadText", sequence, "Response")


async def _process_text(
    turn: PurviewTurn,
    text: str,
    activity: str,
    sequence_number: int,
    name: str,
) -> PurviewDecision:
    content = text[: settings.purview_max_content_chars]
    try:
        etag, modes = await _get_scope(turn)
        execution_mode = modes.get(activity, "")
        payload = _content_payload(
            turn, content, activity, sequence_number, name, truncated=len(text) > len(content)
        )
        # No applicable protection scope is the only case that uses contentActivities.
        if not execution_mode:
            response = await _post_graph(
                f"{_GRAPH_ROOT}/activities/contentActivities",
                turn.token,
                payload,
                turn.client_request_id,
            )
            ok = response.status_code in (200, 201, 202, 204)
            decision = PurviewDecision(
                available=ok,
                operation="contentActivities",
                execution_mode=execution_mode,
                status_code=response.status_code,
                error="" if ok else _safe_http_error(response),
            )
        else:
            response = await _post_graph(
                f"{_GRAPH_ROOT}/processContent",
                turn.token,
                payload,
                turn.client_request_id,
                etag=etag,
                evaluate_inline=execution_mode == "evaluateInline",
            )
            body = _response_json(response)
            ok = response.status_code in (200, 202, 204)
            decision = PurviewDecision(
                available=ok,
                blocked=_contains_block(body),
                operation="processContent",
                execution_mode=execution_mode,
                status_code=response.status_code,
                error="" if ok else _safe_http_error(response),
            )
            if body.get("protectionScopeState") == "modified":
                _scope_cache.pop(_scope_cache_key(turn), None)
        logger.info(
            "Purview operation=%s activity=%s status=%s available=%s blocked=%s mode=%s",
            decision.operation,
            activity,
            decision.status_code,
            decision.available,
            decision.blocked,
            decision.execution_mode,
        )
        return decision
    except Exception as exc:  # noqa: BLE001
        logger.warning("Purview activity=%s unavailable: %s", activity, type(exc).__name__)
        return PurviewDecision(
            available=False,
            operation=activity,
            error=f"{type(exc).__name__}: {str(exc)[:300]}",
        )


def _scope_cache_key(turn: PurviewTurn) -> str:
    return f"{turn.user_id or 'unknown'}:{turn.agent_id}"


async def _get_scope(turn: PurviewTurn) -> tuple[str | None, dict[str, str]]:
    cache_key = _scope_cache_key(turn)
    cached = _scope_cache.get(cache_key)
    if cached and time.time() < cached[0]:
        return cached[1], cached[2]
    response = await _post_graph(
        f"{_GRAPH_ROOT}/protectionScopes/compute",
        turn.token,
        {
            "activities": "uploadText,downloadText",
            "locations": [
                {
                    "@odata.type": "microsoft.graph.policyLocationApplication",
                    "value": turn.agent_id,
                }
            ],
        },
        turn.client_request_id,
    )
    if response.status_code != 200:
        raise RuntimeError(_safe_http_error(response))
    body = _response_json(response)
    modes: dict[str, str] = {}
    locations: list[str] = []
    for scope in body.get("value") or []:
        if not isinstance(scope, dict):
            continue
        mode = str(scope.get("executionMode") or "")
        for location in scope.get("locations") or []:
            if isinstance(location, dict) and location.get("value"):
                locations.append(str(location["value"]))
        for activity in str(scope.get("activities") or "").split(","):
            if activity.strip() and mode:
                modes[activity.strip()] = mode
    etag = response.headers.get("etag")
    _scope_cache[cache_key] = (time.time() + settings.purview_scope_cache_seconds, etag, modes)
    # Policies must be scoped to the location reported here, not to the blueprint.
    logger.info(
        "Purview operation=protectionScopes.compute status=200 activities=%s locations=%s",
        sorted(modes),
        sorted(set(locations)),
    )
    return etag, modes


async def _post_graph(
    url: str,
    token: str,
    payload: dict[str, Any],
    client_request_id: str,
    etag: str | None = None,
    evaluate_inline: bool = False,
) -> httpx.Response:
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Client-Request-Id": client_request_id,
    }
    if etag:
        headers["If-None-Match"] = etag
    if evaluate_inline:
        headers["Prefer"] = "evaluateInline"
    async with httpx.AsyncClient(timeout=settings.purview_request_timeout_seconds) as client:
        return await client.post(url, headers=headers, json=payload)


def _content_payload(
    turn: PurviewTurn,
    text: str,
    activity: str,
    sequence_number: int,
    name: str,
    truncated: bool = False,
) -> dict[str, Any]:
    timestamp = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    blueprint_id = settings.observability_blueprint_id or settings.blueprint_app_id
    location = {
        "@odata.type": "microsoft.graph.policyLocationApplication",
        "value": turn.agent_id,
    }
    return {
        "contentToProcess": {
            "contentEntries": [
                {
                    "@odata.type": "microsoft.graph.processConversationMetadata",
                    "identifier": str(uuid4()),
                    "content": {
                        "@odata.type": "microsoft.graph.textContent",
                        "data": text,
                    },
                    "agents": [
                        {
                            "@odata.type": "microsoft.graph.aiAgentInfo",
                            "blueprintId": blueprint_id,
                            "identifier": turn.agent_id,
                            "name": settings.purview_app_name,
                            "version": settings.purview_app_version,
                        }
                    ],
                    "name": name,
                    "correlationId": turn.correlation_id,
                    "sequenceNumber": sequence_number,
                    "isTruncated": truncated,
                    "createdDateTime": timestamp,
                    "modifiedDateTime": timestamp,
                }
            ],
            "activityMetadata": {"activity": activity},
            "protectedAppMetadata": {
                "name": settings.purview_app_name,
                "version": settings.purview_app_version,
                "applicationLocation": location,
            },
            "integratedAppMetadata": {
                "name": settings.purview_app_name,
                "version": settings.purview_app_version,
            },
        }
    }


def _contains_block(body: dict[str, Any]) -> bool:
    return any(
        isinstance(action, dict)
        and str(action.get("action") or "").lower() == "restrictaccess"
        and str(action.get("restrictionAction") or "").lower() == "block"
        for action in body.get("policyActions") or []
    )


def _response_json(response: httpx.Response) -> dict[str, Any]:
    if not response.content:
        return {}
    try:
        value = response.json()
        return value if isinstance(value, dict) else {}
    except ValueError:
        return {}


def _safe_http_error(response: httpx.Response) -> str:
    request_id = response.headers.get("request-id", "")
    return f"Graph HTTP {response.status_code}; request_id={request_id}"[:300]


def _jwt_claim(token: str, claim: str) -> str:
    try:
        segment = token.split(".")[1]
        segment += "=" * (-len(segment) % 4)
        payload = json.loads(base64.urlsafe_b64decode(segment))
        return str(payload.get(claim) or "")
    except (IndexError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
        return ""
