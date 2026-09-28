"""Microsoft 365 Agents SDK Activity Protocol host for LangchainOA."""

from __future__ import annotations

import logging
import os
from os import environ
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from aiohttp.web import Application, Request, Response, json_response, run_app
from aiohttp.web_middlewares import middleware as web_middleware
from dotenv import load_dotenv
from microsoft_agents.activity import load_configuration_from_env
from microsoft_agents.authentication.msal import MsalConnectionManager
from microsoft_agents.hosting.aiohttp import (
    CloudAdapter,
    jwt_authorization_middleware,
    start_agent_process,
)
from microsoft_agents.hosting.core import (
    AgentApplication,
    AgentAuthConfiguration,
    AuthenticationConstants,
    Authorization,
    ClaimsIdentity,
    MemoryStorage,
    RouteRank,
    TurnContext,
    TurnState,
)

from config import settings
from observability import (
    configure_observability,
    observability_context,
    runtime_identity,
)
from purview import (
    PURVIEW_GRAPH_SCOPES,
    begin_purview_turn,
    capture_purview_response,
    is_purview_enabled,
    should_enforce_block,
)
from token_cache import cache_agentic_token

load_dotenv()
logger = logging.getLogger(__name__)
_sdk_config = load_configuration_from_env(environ)


class LangchainOaHost:
    """Route authenticated AI Teammate turns into the LangChain agent."""

    def __init__(self) -> None:
        from tools.workiq_tools import workiq_server_scopes

        self.workiq_scopes = workiq_server_scopes()
        self.storage = MemoryStorage()
        self.auth_handler_name = settings.auth_handler_name.strip() or None
        self.connection_manager = _create_connection_manager()
        self.adapter = CloudAdapter(connection_manager=self.connection_manager)
        self.authorization = (
            Authorization(self.storage, self.connection_manager, **_sdk_config)
            if self.connection_manager
            else None
        )
        self.agent_app = AgentApplication[TurnState](
            storage=self.storage,
            adapter=self.adapter,
            authorization=self.authorization,
            **_sdk_config,
        )
        self.email_triage = None
        if settings.enable_email_triage:
            from email_triage import EmailTriageController

            self.email_triage = EmailTriageController(
                adapter=self.adapter,
                storage=self.storage,
                exchange_workiq_tokens=self._exchange_workiq_tokens,
                exchange_purview_token=self._exchange_purview_token,
                conversation_key=_stable_conversation_id,
            )
        self._register_routes()

    def _register_routes(self) -> None:
        handler_config = (
            {"auth_handlers": [self.auth_handler_name]}
            if self.auth_handler_name and self.connection_manager
            else {}
        )

        async def welcome(context: TurnContext, _state: TurnState) -> None:
            text = (
                "Hi, I'm **LangchainOA**. I can search and read governed SharePoint, mail, and "
                "calendar data, and inspect Azure resources and monitoring data."
            )
            if self.email_triage:
                text += (
                    " I also triage email sent to my mailbox: approvers get proposals here and answer "
                    "`approve <code>`, `reject <code>`, `edit <code>: <text>`, or `/pending`."
                )
            else:
                text += " I am read-only."
            await context.send_activity(text)

        self.agent_app.conversation_update("membersAdded", **handler_config)(welcome)
        self.agent_app.message("/help", **handler_config)(welcome)

        if self.email_triage:
            from microsoft_agents_a365.notifications import AgentNotification

            @AgentNotification(self.agent_app).on_email(rank=RouteRank.FIRST, **handler_config)
            async def on_email(context: TurnContext, _state: TurnState, notification: Any) -> None:
                try:
                    await self.email_triage.handle_email(context, notification)
                except Exception as exc:  # noqa: BLE001
                    logger.error("Email triage failed: %s", exc, exc_info=True)

        @self.agent_app.activity("message", **handler_config)
        async def on_message(context: TurnContext, _state: TurnState) -> None:
            from langchain_agent import run_agent

            user_message = (context.activity.text or "").strip()
            if not user_message or user_message == "/help":
                return
            if self.email_triage and await self.email_triage.handle_approver_message(context):
                return

            recipient = context.activity.recipient
            conversation_id = _stable_conversation_id(getattr(context.activity.conversation, "id", None))
            await self._cache_observability_token(context)
            workiq_tokens = await self._exchange_workiq_tokens(context)
            _tenant_id, runtime_agent_id = runtime_identity(context)
            purview_turn = await begin_purview_turn(
                await self._exchange_purview_token(context),
                user_message,
                conversation_id,
                runtime_agent_id,
            )
            if purview_turn and should_enforce_block(purview_turn.prompt_decision):
                await context.send_activity(settings.purview_blocked_prompt_message)
                return

            try:
                with observability_context(context, conversation_id):
                    response = await run_agent(
                        user_message=user_message,
                        conversation_id=conversation_id,
                        workiq_token=workiq_tokens,
                        tenant_id=_activity_value(recipient, "tenant_id", "tenantId") or settings.tenant_id,
                        consumer_id=(
                            _activity_value(recipient, "agentic_app_id", "agenticAppId")
                            or settings.workiq_consumer_id
                        ),
                        environment_id=settings.workiq_environment_id,
                    )
                if should_enforce_block(await capture_purview_response(purview_turn, response)):
                    response = settings.purview_blocked_response_message
                await context.send_activity(response)
            except Exception as exc:  # noqa: BLE001
                logger.error("LangchainOA turn failed: %s", exc, exc_info=True)
                await context.send_activity("Sorry, I couldn't complete that request. Please try again.")

    async def _exchange_workiq_tokens(self, context: TurnContext) -> dict[str, str]:
        if not (settings.enable_workiq and self.authorization and self.auth_handler_name):
            return {}
        tokens: dict[str, str] = {}
        for server, scope in self.workiq_scopes.items():
            try:
                response = await self.authorization.exchange_token(
                    context,
                    scopes=[scope],
                    auth_handler_id=self.auth_handler_name,
                )
                token = getattr(response, "token", None)
                if token:
                    tokens[server] = token
            except Exception as exc:  # noqa: BLE001
                logger.info("Work IQ token exchange unavailable for %s: %s", server, exc)
        return tokens

    async def _exchange_purview_token(self, context: TurnContext) -> str | None:
        """Mint the delegated agentic-user Graph token Purview evaluates against."""

        if not (is_purview_enabled() and self.authorization and self.auth_handler_name):
            return None
        try:
            response = await self.authorization.exchange_token(
                context,
                scopes=PURVIEW_GRAPH_SCOPES,
                auth_handler_id=self.auth_handler_name,
            )
            return getattr(response, "token", None)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Purview token exchange unavailable: %s", exc)
            return None

    async def _cache_observability_token(self, context: TurnContext) -> None:
        """Exchange and cache the delegated exporter token for this runtime turn."""

        if not (
            settings.enable_a365_observability_exporter
            and self.authorization
            and self.auth_handler_name
        ):
            return
        tenant_id, agent_id = runtime_identity(context)
        if not (tenant_id and agent_id):
            logger.debug("Observability token exchange skipped: runtime identity unavailable")
            return
        try:
            from microsoft_agents_a365.runtime.environment_utils import (
                get_observability_authentication_scope,
            )

            response = await self.authorization.exchange_token(
                context,
                scopes=get_observability_authentication_scope(),
                auth_handler_id=self.auth_handler_name,
            )
            token = getattr(response, "token", None)
            if token:
                cache_agentic_token(tenant_id, agent_id, token)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Observability token exchange unavailable: %s", exc)

    def create_auth_configuration(self) -> AgentAuthConfiguration | None:
        client_id = environ.get("CLIENT_ID") or settings.service_connection_client_id
        tenant_id = environ.get("TENANT_ID") or settings.service_connection_tenant_id or settings.tenant_id
        client_secret = environ.get("CLIENT_SECRET") or settings.service_connection_client_secret
        auth_type = settings.service_connection_auth_type.strip() or "ClientSecret"
        if not (client_id and tenant_id):
            logger.warning("Activity Protocol credentials are incomplete; only local evaluation can be used")
            return None
        if needs_client_secret(auth_type) and not client_secret:
            logger.warning("Activity Protocol credentials are incomplete; only local evaluation can be used")
            return None
        scopes = [scope.strip() for scope in settings.service_connection_scopes.split(",") if scope.strip()]
        return AgentAuthConfiguration(
            auth_type=auth_type,
            client_id=client_id,
            tenant_id=tenant_id,
            client_secret=client_secret or None,
            scopes=scopes,
            FEDERATEDCLIENTID=settings.service_connection_federated_client_id or None,
        )

    def start(self) -> None:
        auth_configuration = self.create_auth_configuration()

        async def messages(request: Request) -> Response:
            return await start_agent_process(request, request.app["agent_app"], request.app["adapter"])

        async def health(_request: Request) -> Response:
            return json_response(
                {
                    "status": "healthy",
                    "agent": "LangchainOA",
                    "model": settings.azure_openai_deployment,
                    "orchestrator": "langchain",
                    "host": "microsoft-365-agents-sdk",
                    "read_only": not settings.enable_email_triage,
                    "email_triage": "approval" if settings.enable_email_triage else "disabled",
                    "workiq": settings.enable_workiq,
                    "observability": (
                        "export" if settings.enable_a365_observability_exporter
                        else ("instrument" if settings.enable_a365_observability else "disabled")
                    ),
                    "purview": (
                        "enforce" if settings.purview_enforce_blocks
                        else ("capture" if settings.enable_purview else "disabled")
                    ),
                }
            )

        async def eval_invoke(request: Request) -> Response:
            from langchain_agent import run_agent

            try:
                payload = await request.json()
            except Exception:  # noqa: BLE001
                return json_response({"error": "invalid JSON"}, status=400)
            message = str(payload.get("message") or payload.get("input") or "").strip()
            if not message:
                return json_response({"error": "missing message"}, status=400)
            try:
                response = await run_agent(message, _stable_conversation_id(payload.get("conversation_id")))
            except Exception as exc:  # noqa: BLE001
                # Surface the reason here; this route exists for local diagnosis.
                logger.error("Local evaluation failed: %s", exc, exc_info=True)
                return json_response({"status": "error", "error": str(exc)[:800]}, status=500)
            return json_response({"status": "success", "response": response})

        middlewares = []
        if auth_configuration:

            @web_middleware
            async def jwt_with_public_endpoints(request: Request, handler):
                public_paths = {"/api/health"}
                if settings.enable_local_eval:
                    public_paths.add("/eval/invoke")
                if request.path in public_paths:
                    return await handler(request)
                return await jwt_authorization_middleware(request, handler)

            middlewares.append(jwt_with_public_endpoints)

        @web_middleware
        async def anonymous_claims(request: Request, handler):
            if not auth_configuration or request.path == "/api/health" or (
                settings.enable_local_eval and request.path == "/eval/invoke"
            ):
                request["claims_identity"] = ClaimsIdentity(
                    {
                        AuthenticationConstants.AUDIENCE_CLAIM: "anonymous",
                        AuthenticationConstants.APP_ID_CLAIM: "anonymous-app",
                    },
                    False,
                    "Anonymous",
                )
            return await handler(request)

        middlewares.append(anonymous_claims)

        app = Application(middlewares=middlewares)
        app.router.add_post("/api/messages", messages)
        app.router.add_get("/api/messages", lambda _request: Response(status=200))
        app.router.add_get("/api/health", health)
        if settings.enable_local_eval:
            app.router.add_post("/eval/invoke", eval_invoke)
        app["agent_configuration"] = auth_configuration
        app["agent_app"] = self.agent_app
        app["adapter"] = self.agent_app.adapter
        run_app(app, host=settings.host, port=int(os.environ.get("PORT", settings.port)), handle_signals=True)


def run_host() -> None:
    logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO))
    configure_observability()
    LangchainOaHost().start()


def needs_client_secret(auth_type: str) -> bool:
    """Only the ClientSecret blueprint auth mode requires a stored secret."""

    return auth_type.strip().lower() in {"", "clientsecret", "client_secret"}


def _create_connection_manager() -> MsalConnectionManager | None:
    try:
        return MsalConnectionManager(**_sdk_config)
    except ValueError as exc:
        if "No service connection configuration provided" not in str(exc):
            raise
        logger.warning("No Microsoft Agents SDK service connection config; using anonymous local host")
        return None


def _activity_value(source: Any, *names: str) -> str:
    if source is None:
        return ""
    for name in names:
        value = source.get(name) if isinstance(source, dict) else getattr(source, name, None)
        if value:
            return str(value)
    additional = getattr(source, "additional_properties", None)
    if isinstance(additional, dict):
        for name in names:
            if additional.get(name):
                return str(additional[name])
    return ""


def _stable_conversation_id(source: Any) -> str:
    """Derive a stable, non-reversible conversation key used across history and telemetry."""

    return str(uuid5(NAMESPACE_URL, f"langchainoa:{source or 'local-eval'}"))
