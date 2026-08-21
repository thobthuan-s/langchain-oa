"""Keyless Azure OpenAI chat model for LangchainOA."""

from __future__ import annotations

from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from langchain_openai import AzureChatOpenAI

from config import settings

_COGNITIVE_SCOPE = "https://cognitiveservices.azure.com/.default"


def create_chat_model() -> AzureChatOpenAI:
    """Create a secretless Azure OpenAI client backed by the ambient Azure identity.

    Locally this resolves through `az login`; in Container Apps it resolves through
    the assigned managed identity. No API key is used in either case.
    """

    if not settings.azure_openai_endpoint or not settings.azure_openai_deployment:
        raise RuntimeError("AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_DEPLOYMENT are required")

    return AzureChatOpenAI(
        azure_endpoint=settings.azure_openai_endpoint,
        azure_deployment=settings.azure_openai_deployment,
        api_version=settings.azure_openai_api_version,
        azure_ad_token_provider=get_bearer_token_provider(
            DefaultAzureCredential(),
            _COGNITIVE_SCOPE,
        ),
        temperature=settings.model_temperature,
        max_tokens=settings.model_max_tokens,
    )
