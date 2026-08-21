"""Read-only LangchainOA tools."""

from .azure_tools import AZURE_TOOLS
from .workiq_tools import WORKIQ_TOOLS

ALL_TOOLS = [*AZURE_TOOLS, *WORKIQ_TOOLS]

__all__ = ["ALL_TOOLS", "AZURE_TOOLS", "WORKIQ_TOOLS"]
