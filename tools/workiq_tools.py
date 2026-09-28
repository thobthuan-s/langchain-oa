"""Governed Work IQ MCP tools for SharePoint, mail, and calendar.

The LangChain tools exported here are read-only. ``invoke_mail_operation`` is a
separate, code-only path used by email triage after a human approves an action.
"""

from __future__ import annotations

import base64
import binascii
import io
import json
import logging
import os
import re
import time
import zipfile
import xml.etree.ElementTree as ET
from contextvars import ContextVar
from pathlib import Path
from typing import Any

import httpx
from langchain.tools import tool

from config import settings

logger = logging.getLogger(__name__)

_MANIFEST_PATH = Path(__file__).resolve().parent.parent / "ToolingManifest.json"
_GATEWAY_ROOT = "https://agent365.svc.cloud.microsoft/agents/servers"

# Fallback catalog. ToolingManifest.json, written by `a365 develop add-mcp-servers`,
# overrides these names and URLs when present.
_DEFAULT_SERVERS: dict[str, dict[str, str]] = {
    "sharepoint": {
        "name": "mcp_SharePointRemoteServer",
        "url": f"{_GATEWAY_ROOT}/mcp_SharePointRemoteServer",
        "scope": "Tools.ListInvoke.All",
        "audience": "292cff14-c0e8-4116-9e3b-99934ae05766",
    },
    "mail": {
        "name": "mcp_MailTools",
        "url": f"{_GATEWAY_ROOT}/mcp_MailTools",
        "scope": "Tools.ListInvoke.All",
        "audience": "16b1878d-62c7-4009-aa25-68989d63bbad",
    },
    "calendar": {
        "name": "mcp_CalendarTools",
        "url": f"{_GATEWAY_ROOT}/mcp_CalendarTools",
        "scope": "Tools.ListInvoke.All",
        "audience": "910333d2-47e9-43ca-981f-6df2f4531ef4",
    },
}

# Read-only policy. Tool names are split into words (camelCase, snake_case, kebab),
# server prefixes such as "mcp_MailTools_graph_mail_" are removed, and the first
# word must be a read verb. No word anywhere in the name may be a write verb.
_READ_VERBS = frozenset({"get", "list", "find", "read", "search", "query", "browse"})
_WRITE_WORDS = frozenset(
    {
        "accept", "add", "append", "apply", "approve", "archive", "assign", "cancel",
        "check", "checkin", "checkout", "clear", "copy", "create", "decline", "delete",
        "dismiss", "edit", "flag", "forward", "grant", "import", "insert",
        "invite", "lock", "mark", "modify", "move", "patch", "pin", "post", "publish",
        "put", "reject", "remove", "rename", "reply", "replyall", "reset", "respond",
        "restore", "revoke", "save", "send", "set", "share", "snooze", "submit",
        "subscribe", "tag", "unlock", "unpin", "unsubscribe", "update", "upload",
        "upsert", "write",
    }
)
_SERVER_PREFIX_RE = re.compile(r"^(?:mcp_[A-Za-z0-9]+_)?(?:graph_[A-Za-z0-9]+_)?")

_access_tokens: ContextVar[dict[str, str]] = ContextVar("langchainoa_workiq_tokens", default={})
_tenant_id: ContextVar[str] = ContextVar("langchainoa_workiq_tenant", default="")
_consumer_id: ContextVar[str] = ContextVar("langchainoa_workiq_consumer", default="")
_environment_id: ContextVar[str] = ContextVar("langchainoa_workiq_environment", default="")

_tool_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}


class WorkIqError(Exception):
    """A sanitized Work IQ MCP failure."""


def _load_manifest() -> dict[str, dict[str, str]]:
    """Merge the CLI-owned ToolingManifest.json over the fallback catalog."""

    servers = {key: dict(value) for key, value in _DEFAULT_SERVERS.items()}
    if not _MANIFEST_PATH.exists():
        return servers

    try:
        manifest = json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("ToolingManifest.json could not be read: %s", exc)
        return servers

    entries = manifest if isinstance(manifest, list) else manifest.get("mcpServers", [])
    for entry in entries if isinstance(entries, list) else []:
        name = str(entry.get("mcpServerName") or entry.get("name") or "")
        url = str(entry.get("url") or "")
        if not name or not url:
            continue
        lowered = name.lower()
        for key in servers:
            if key in lowered:
                servers[key] = {
                    "name": name,
                    "url": url,
                    "scope": str(entry.get("scope") or ""),
                    "audience": str(entry.get("audience") or ""),
                }

    return servers


_SERVER_CONFIG = _load_manifest()


def workiq_server_scopes() -> dict[str, str]:
    """Return the OAuth scope required by each configured V2 MCP server."""

    result: dict[str, str] = {}
    for server, config in _SERVER_CONFIG.items():
        audience = config.get("audience", "").strip()
        scope = config.get("scope", "").strip()
        if not audience:
            continue
        result[server] = f"{audience}/{scope}" if scope else f"{audience}/.default"
    return result


def set_workiq_context(
    access_tokens: dict[str, str] | None,
    tenant_id: str | None,
    consumer_id: str | None,
    environment_id: str | None,
) -> tuple[Any, Any, Any, Any]:
    """Set request-scoped identity context read by the Work IQ tools."""

    if not access_tokens and settings.python_environment.strip().lower() != "production":
        access_tokens = _local_access_tokens()
    return (
        _access_tokens.set(access_tokens or {}),
        _tenant_id.set(tenant_id or settings.tenant_id or ""),
        _consumer_id.set(consumer_id or settings.workiq_consumer_id),
        _environment_id.set(environment_id or settings.workiq_environment_id),
    )


def reset_workiq_context(resets: tuple[Any, Any, Any, Any]) -> None:
    """Clear request-scoped Work IQ identity context."""

    tokens_reset, tenant_reset, consumer_reset, environment_reset = resets
    _access_tokens.reset(tokens_reset)
    _tenant_id.reset(tenant_reset)
    _consumer_id.reset(consumer_reset)
    _environment_id.reset(environment_reset)


def tool_name_words(tool_name: str) -> list[str]:
    """Split a tool name into lowercase words after removing the server prefix."""

    operation = _SERVER_PREFIX_RE.sub("", tool_name.strip()) or tool_name.strip()
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", operation)
    spaced = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", spaced)
    return [word for word in re.split(r"[^A-Za-z0-9]+", spaced.lower()) if word]


def is_read_only_workiq_tool(tool_name: str, tool_definition: dict[str, Any] | None = None) -> bool:
    """Return whether a discovered MCP tool passes the read-only policy.

    The name policy is the primary gate. MCP annotations can only make it stricter,
    and WORKIQ_ALLOWED_TOOLS, when set, restricts calls to an explicit list.
    """

    allowlist = {name.strip() for name in settings.workiq_allowed_tools.split(",") if name.strip()}
    if allowlist and tool_name not in allowlist:
        return False
    words = tool_name_words(tool_name)
    if not words or words[0] not in _READ_VERBS or _WRITE_WORDS.intersection(words):
        return False
    annotations = (tool_definition or {}).get("annotations")
    if isinstance(annotations, dict):
        if annotations.get("readOnlyHint") is False or annotations.get("destructiveHint") is True:
            return False
    return True


@tool
async def list_workiq_tools(server: str) -> dict[str, Any]:
    """List read-only tools for server='sharepoint', 'mail', or 'calendar'."""

    try:
        tools = await _list_tools(server)
        safe_tools = [item for item in tools if is_read_only_workiq_tool(str(item.get("name") or ""), item)]
        return {
            "status": "success",
            "server": server,
            "count": len(safe_tools),
            "tools": [_summarize_tool(candidate) for candidate in safe_tools],
        }
    except Exception as exc:  # noqa: BLE001
        return _safe_error(server, exc)


@tool
async def call_readonly_workiq_tool(
    server: str,
    tool_name: str,
    arguments: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Call one discovered read-only Work IQ tool for SharePoint, mail, or calendar."""

    if not is_read_only_workiq_tool(tool_name):
        return {
            "status": "blocked",
            "server": server,
            "tool": tool_name,
            "error": "The requested Work IQ operation is not allowed by the read-only policy.",
        }
    try:
        discovered = await _list_tools(server)
        definition = next((item for item in discovered if str(item.get("name") or "") == tool_name), None)
        if definition is None:
            return {
                "status": "error",
                "server": server,
                "tool": tool_name,
                "error": "Tool name was not returned by the selected Work IQ server.",
            }
        if not is_read_only_workiq_tool(tool_name, definition):
            return {
                "status": "blocked",
                "server": server,
                "tool": tool_name,
                "error": "The requested Work IQ operation is not allowed by the read-only policy.",
            }
        result = await _call_tool(server, tool_name, arguments or {})
        return {
            "status": "success",
            "server": server,
            "tool": tool_name,
            "result": _post_process_result(tool_name, result),
        }
    except Exception as exc:  # noqa: BLE001
        return _safe_error(server, exc, tool_name)


@tool
async def search_sharepoint(query: str, top: int = 5) -> dict[str, Any]:
    """Search SharePoint documents and folders through governed Work IQ."""

    try:
        tools = await _list_tools("sharepoint")
        safe_tools = [item for item in tools if is_read_only_workiq_tool(str(item.get("name") or ""), item)]
        search_tool = _select_search_tool(safe_tools)
        if not search_tool:
            return {
                "status": "error",
                "error": "No read-only SharePoint search tool was discovered.",
                "available_tools": [_summarize_tool(candidate) for candidate in safe_tools],
            }
        arguments = _build_search_arguments(search_tool, query, top)
        result = await _call_tool("sharepoint", str(search_tool["name"]), arguments)
        return {
            "status": "success",
            "tool": search_tool["name"],
            "arguments": arguments,
            "result": _bounded(result),
        }
    except Exception as exc:  # noqa: BLE001
        return _safe_error("sharepoint", exc)


async def _list_tools(server: str) -> list[dict[str, Any]]:
    config = _get_server(server)
    _ensure_authenticated(server)
    cache_key = f"{_tenant_id.get()}:{config['name']}"
    cached = _tool_cache.get(cache_key)
    if cached and time.time() - cached[0] < settings.workiq_tool_cache_seconds:
        return cached[1]

    client = _McpClient(config["url"], server)
    try:
        await client.initialize()
        result = await client.request("tools/list", {})
        tools = result.get("tools", []) if isinstance(result, dict) else []
        if not isinstance(tools, list):
            raise WorkIqError("tools/list returned an unexpected payload")
        _tool_cache[cache_key] = (time.time(), tools)
        return tools
    finally:
        await client.close()


async def _call_tool(server: str, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    config = _get_server(server)
    _ensure_authenticated(server)
    client = _McpClient(config["url"], server)
    try:
        await client.initialize()
        return await client.request(
            "tools/call",
            {"name": tool_name, "arguments": _inject_context(tool_name, arguments)},
        )
    finally:
        await client.close()


def _get_server(server: str) -> dict[str, str]:
    key = server.strip().lower()
    if key not in _SERVER_CONFIG:
        raise WorkIqError("server must be one of: sharepoint, mail, calendar")
    return _SERVER_CONFIG[key]


def _ensure_authenticated(server: str) -> None:
    if not settings.enable_workiq:
        raise WorkIqError("Work IQ is disabled")
    if not _access_tokens.get().get(server):
        raise WorkIqError(
            f"No Work IQ token is available for {server}. Use the Microsoft 365 channel, "
            "or set the server-specific BEARER_TOKEN_MCP_* value from "
            "`a365 develop get-token --resource mcp -o raw` for local development."
        )


def _local_access_tokens() -> dict[str, str]:
    result: dict[str, str] = {}
    for server, config in _SERVER_CONFIG.items():
        suffix = re.sub(r"[^A-Za-z0-9_]", "_", config["name"]).upper()
        token = os.getenv(f"BEARER_TOKEN_{suffix}")
        if token:
            result[server] = token
    return result


def _inject_context(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    result = dict(arguments)
    if tool_name.lower() == "query_federated_knowledge":
        if _consumer_id.get():
            result.setdefault("consumerId", _consumer_id.get())
        if _environment_id.get():
            result.setdefault("environmentId", _environment_id.get())
    return result


class _McpClient:
    """Minimal streamable-HTTP JSON-RPC client for the Work IQ MCP gateway."""

    def __init__(self, base_url: str, server: str) -> None:
        tenant = _tenant_id.get()
        if tenant and "/tenants/" not in base_url:
            base_url = base_url.replace("/agents/servers/", f"/agents/tenants/{tenant}/servers/")
        self._url = base_url
        self._server = server
        self._client = httpx.AsyncClient(timeout=settings.workiq_request_timeout_seconds)
        self._request_id = 0
        self._session_id = ""

    async def close(self) -> None:
        await self._client.aclose()

    async def initialize(self) -> None:
        if not settings.workiq_initialize_session:
            return
        await self.request(
            "initialize",
            {
                "protocolVersion": settings.workiq_protocol_version,
                "capabilities": {},
                "clientInfo": {"name": "langchain-oa", "version": "1.0.0"},
            },
        )
        await self._post(
            {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
            expect_response=False,
        )

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._request_id += 1
        response = await self._post(
            {"jsonrpc": "2.0", "id": self._request_id, "method": method, "params": params},
            expect_response=True,
            request_id=self._request_id,
        )
        if "error" in response:
            error = response["error"]
            message = error.get("message") if isinstance(error, dict) else str(error)
            raise WorkIqError(f"MCP {method} failed: {message}")
        result = response.get("result", {})
        return result if isinstance(result, dict) else {"value": result}

    async def _post(
        self,
        payload: dict[str, Any],
        expect_response: bool,
        request_id: int | None = None,
    ) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {_access_tokens.get()[self._server]}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": settings.workiq_protocol_version,
        }
        if _environment_id.get():
            headers["x-ms-environment-id"] = _environment_id.get()
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        _inject_trace_context(headers)

        response = await self._client.post(self._url, headers=headers, json=payload)
        session_id = response.headers.get("mcp-session-id")
        if session_id:
            self._session_id = session_id
        if response.status_code in (202, 204) and not expect_response:
            return {}
        if response.status_code >= 400:
            raise WorkIqError(f"MCP HTTP {response.status_code}: {response.text[:500]}")
        if not response.text.strip():
            return {}
        return _decode_response(response, request_id)


def _inject_trace_context(headers: dict[str, str]) -> None:
    """Send the active W3C trace context so the MCP gateway can continue this trace."""

    try:
        from opentelemetry.propagate import inject
    except ImportError:  # pragma: no cover - OpenTelemetry is a runtime dependency
        return
    inject(headers)


def _decode_response(response: httpx.Response, request_id: int | None = None) -> dict[str, Any]:
    """Decode a JSON or SSE response, returning the message that answers ``request_id``."""

    text = response.text.strip()
    content_type = response.headers.get("content-type", "")
    if "text/event-stream" in content_type or text.startswith(("event:", "data:")):
        for message in _sse_messages(text):
            if request_id is None or message.get("id") == request_id:
                return message
        raise WorkIqError("MCP event stream did not contain a response for the request")
    return response.json()


def _sse_messages(text: str):
    """Yield JSON objects from SSE events, joining multi-line data fields."""

    for block in re.split(r"\r?\n\r?\n", text):
        data = "\n".join(
            line.removeprefix("data:").removeprefix(" ")
            for line in block.splitlines()
            if line.startswith("data:")
        ).strip()
        if not data or data == "[DONE]":
            continue
        try:
            message = json.loads(data)
        except json.JSONDecodeError:
            continue
        if isinstance(message, dict):
            yield message


def _select_search_tool(tools: list[dict[str, Any]]) -> dict[str, Any] | None:
    ranked: list[tuple[int, dict[str, Any]]] = []
    for candidate in tools:
        name = str(candidate.get("name") or "").lower()
        description = str(candidate.get("description") or "").lower()
        score = sum(term in f"{name} {description}" for term in ("search", "find", "query", "document", "file"))
        if "search" in name:
            score += 3
        if score:
            ranked.append((score, candidate))
    return max(ranked, key=lambda item: item[0])[1] if ranked else None


def _build_search_arguments(tool_definition: dict[str, Any], query: str, top: int) -> dict[str, Any]:
    schema = tool_definition.get("inputSchema") or tool_definition.get("input_schema") or {}
    properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
    arguments: dict[str, Any] = {}
    for key in ("searchQuery", "query", "search", "searchText", "keywords", "text", "q"):
        if key in properties:
            arguments[key] = query
            break
    if not arguments:
        arguments["query"] = query
    for key in ("top", "limit", "count", "maxResults", "size"):
        if key in properties:
            arguments[key] = max(1, min(top, settings.workiq_max_search_results))
            break
    return arguments


def _summarize_tool(tool_definition: dict[str, Any]) -> dict[str, Any]:
    schema = tool_definition.get("inputSchema") or tool_definition.get("input_schema") or {}
    return {
        "name": tool_definition.get("name"),
        "description": str(tool_definition.get("description") or "")[:600],
        "input_schema": _bounded(schema, max_string=300, max_items=30),
    }


def _post_process_result(tool_name: str, result: dict[str, Any]) -> dict[str, Any]:
    """Extract text from .docx payloads that arrive as base64 ZIP content."""

    if tool_name.lower() in {"readsmalltextfile", "readsmallbinaryfile"}:
        extracted = _extract_docx_text_from_result(result)
        if extracted:
            return {
                "format": "docx",
                "extracted_text": extracted[:6000],
                "truncated": len(extracted) > 6000,
            }
    return _bounded(result)


def _bounded(value: Any, max_string: int = 2000, max_items: int = 50) -> Any:
    if isinstance(value, str):
        return value[:max_string]
    if isinstance(value, list):
        return [_bounded(item, max_string, max_items) for item in value[:max_items]]
    if isinstance(value, dict):
        return {key: _bounded(item, max_string, max_items) for key, item in list(value.items())[:max_items]}
    return value


def _safe_error(server: str, exc: Exception, tool_name: str | None = None) -> dict[str, Any]:
    payload = {"status": "error", "server": server, "error": str(exc)[:800]}
    if tool_name:
        payload["tool"] = tool_name
    return payload


def _extract_docx_text_from_result(value: Any) -> str | None:
    for text in _iter_strings(value):
        payload = _decode_docx_payload(text)
        if payload:
            extracted = _extract_docx_text(payload)
            if extracted:
                return extracted
    return None


def _iter_strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _iter_strings(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _iter_strings(item)


def _decode_docx_payload(value: str) -> bytes | None:
    text = value.strip()
    if text.startswith("data:") and "," in text:
        text = text.split(",", 1)[1].strip()
    if text.startswith("PK\x03\x04"):
        return text.encode("latin-1", errors="ignore")
    compact = "".join(text.split())
    if not compact.startswith("UEsDB"):
        return None
    try:
        return base64.b64decode(compact, validate=False)
    except (binascii.Error, ValueError):
        return None


def _extract_docx_text(payload: bytes) -> str | None:
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            xml_bytes = archive.read("word/document.xml")
    except (zipfile.BadZipFile, KeyError, OSError):
        return None

    namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return None

    paragraphs: list[str] = []
    for paragraph in root.iter(f"{namespace}p"):
        text = "".join(node.text or "" for node in paragraph.iter(f"{namespace}t"))
        if text.strip():
            paragraphs.append(text.strip())
    return "\n".join(paragraphs) or None


# Fixed mail write operations used by the email-triage executor after human approval.
# They are never registered as LangChain tools, so the model cannot invoke them.
# Candidate name suffixes per operation, in preference order. Catalog names vary
# between Mail server versions, so each operation accepts several spellings.
_MAIL_OPERATION_SUFFIXES: dict[str, tuple[str, ...]] = {
    "tag": ("updatemessage",),
    "reply": ("reply",),
    "send": ("sendmail", "sendemail", "sendmailmessage"),
    "draft": ("createmessage", "createdraft", "createdraftmessage"),
    "send_draft": ("senddraft", "senddraftmessage"),
}


class MailToolNotFound(WorkIqError):
    """No discovered Mail MCP tool matches a fixed operation."""


def _normalized_tool_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def resolve_mail_operation(operation: str, tools: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Pick the discovered Mail MCP tool for a fixed operation by exact name suffix."""

    suffixes = _MAIL_OPERATION_SUFFIXES.get(operation)
    if not suffixes:
        raise WorkIqError(f"Unsupported mail operation: {operation}")
    for suffix in suffixes:
        matches = [
            candidate
            for candidate in tools
            if _normalized_tool_name(str(candidate.get("name") or "")).endswith(suffix)
        ]
        if matches:
            return min(matches, key=lambda item: len(str(item.get("name") or "")))
    return None


def tool_input_properties(tool_definition: dict[str, Any]) -> dict[str, Any]:
    schema = tool_definition.get("inputSchema") or tool_definition.get("input_schema") or {}
    properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
    return properties if isinstance(properties, dict) else {}


async def invoke_mail_operation(
    operation: str,
    build_arguments: Any,
) -> dict[str, Any]:
    """Run one fixed Mail MCP write operation in the current Work IQ context.

    ``build_arguments`` receives the discovered tool definition so arguments can
    follow the live input schema.
    """

    tools = await _list_tools("mail")
    tool_definition = resolve_mail_operation(operation, tools)
    if not tool_definition:
        names = sorted(str(item.get("name") or "") for item in tools)
        logger.warning("No Mail MCP tool for operation %s; available tools: %s", operation, names)
        raise MailToolNotFound(f"No Mail MCP tool was discovered for operation '{operation}'")
    tool_name = str(tool_definition["name"])
    result = await _call_tool("mail", tool_name, build_arguments(tool_definition))
    if isinstance(result, dict) and result.get("isError"):
        raise WorkIqError(f"Mail MCP {tool_name} reported an error: {str(_bounded(result))[:500]}")
    return {"tool": tool_name, "result": _bounded(result)}


WORKIQ_TOOLS = [
    list_workiq_tools,
    call_readonly_workiq_tool,
    search_sharepoint,
]
