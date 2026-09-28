"""Validated environment configuration for LangchainOA."""

from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Configuration loaded from environment variables and an optional .env file."""

    # --- Azure OpenAI (keyless; caller needs Cognitive Services OpenAI User) ---
    azure_openai_endpoint: str = ""
    azure_openai_deployment: str = "gpt-4o-mini"
    azure_openai_api_version: str = "2025-04-01-preview"
    model_temperature: float = 0.0
    model_max_tokens: int = 4096

    # --- Entra Agent ID ---
    tenant_id: str = ""
    blueprint_app_id: str = ""
    auth_handler_name: str = "AGENTIC"

    # --- Azure read-only tools ---
    azure_subscription_id: str = ""

    # --- Work IQ MCP ---
    enable_workiq: bool = True
    workiq_environment_id: str = ""
    workiq_consumer_id: str = ""
    workiq_initialize_session: bool = False
    workiq_protocol_version: str = "2025-03-26"
    workiq_tool_cache_seconds: int = 300
    workiq_request_timeout_seconds: float = 30.0
    workiq_max_search_results: int = 10

    # --- Activity Protocol outbound replies (stamped by `a365 setup all`) ---
    service_connection_auth_type: str = Field(
        default="ClientSecret",
        validation_alias="CONNECTIONS__SERVICE_CONNECTION__SETTINGS__AUTHTYPE",
    )
    service_connection_federated_client_id: str = Field(
        default="",
        validation_alias="CONNECTIONS__SERVICE_CONNECTION__SETTINGS__FEDERATEDCLIENTID",
    )
    service_connection_client_id: str = Field(
        default="",
        validation_alias="CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTID",
    )
    service_connection_client_secret: str = Field(
        default="",
        validation_alias="CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTSECRET",
    )
    service_connection_tenant_id: str = Field(
        default="",
        validation_alias="CONNECTIONS__SERVICE_CONNECTION__SETTINGS__TENANTID",
    )
    service_connection_scopes: str = Field(
        default="5a807f24-c9de-44ee-a3a7-329e88a00ffc/.default",
        validation_alias="CONNECTIONS__SERVICE_CONNECTION__SETTINGS__SCOPES",
    )

    # --- Agent 365 observability ---
    enable_a365_observability: bool = False
    enable_a365_observability_exporter: bool = False
    enable_a365_sensitive_data: bool = False
    observability_tenant_id: str = Field(default="", validation_alias="AGENT365OBSERVABILITY__TENANTID")
    observability_blueprint_id: str = Field(
        default="",
        validation_alias="AGENT365OBSERVABILITY__AGENTBLUEPRINTID",
    )

    # --- Microsoft Purview content capture and DLP (optional) ---
    enable_purview: bool = False
    purview_enforce_blocks: bool = False
    purview_request_timeout_seconds: float = 45.0
    purview_scope_cache_seconds: int = 300
    purview_max_content_chars: int = 100000
    purview_app_name: str = "LangchainOA"
    purview_app_version: str = "1.0"
    purview_blocked_prompt_message: str = "This request was blocked by Microsoft Purview policy."
    purview_blocked_response_message: str = "The response was blocked by Microsoft Purview policy."

    # --- Email triage with Teams approval (optional) ---
    enable_email_triage: bool = False
    # Comma-separated Entra object IDs (or Teams user IDs) allowed to approve actions.
    email_triage_approvers: str = ""
    email_triage_escalation_address: str = ""
    # Comma-separated domains treated as internal; other senders are flagged external.
    email_triage_internal_domains: str = ""
    email_triage_auto_tag: bool = True
    email_triage_approval_ttl_hours: int = 72
    email_triage_max_body_chars: int = 12000

    # --- Server ---
    host: str = "0.0.0.0"
    port: int = 8080
    log_level: str = "INFO"
    enable_local_eval: bool = False

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()
