import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from azure.monitor.query import LogsQueryStatus

from tools import azure_tools


@pytest.mark.parametrize(
    ("resource_id", "expected"),
    [
        (
            "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Storage/storageAccounts/acct",
            ("Microsoft.Storage", "storageAccounts"),
        ),
        (
            "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Sql/servers/srv/databases/db",
            ("Microsoft.Sql", "servers/databases"),
        ),
        (
            "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm"
            "/providers/Microsoft.Insights/diagnosticSettings/d",
            ("Microsoft.Insights", "diagnosticSettings"),
        ),
    ],
)
def test_parse_resource_type(resource_id, expected) -> None:
    assert azure_tools.parse_resource_type(resource_id) == expected


def test_select_api_version_prefers_newest_stable() -> None:
    versions = ["2023-01-01", "2025-06-01-preview", "2024-05-01", "2022-09-01"]

    assert azure_tools.select_api_version(versions) == "2024-05-01"
    assert azure_tools.select_api_version(["2025-06-01-preview"]) == "2025-06-01-preview"


def test_get_resource_uses_the_provider_api_version(monkeypatch) -> None:
    calls = {}
    provider = SimpleNamespace(
        resource_types=[
            SimpleNamespace(resource_type="storageAccounts", api_versions=["2023-05-01", "2024-01-01"]),
        ]
    )

    class _Resources:
        def get_by_id(self, resource_id, api_version):
            calls["api_version"] = api_version
            return SimpleNamespace(name="acct", type="t", location="l", kind="k", tags={}, properties={})

    client = SimpleNamespace(providers=SimpleNamespace(get=lambda namespace: provider), resources=_Resources())
    monkeypatch.setattr(azure_tools.settings, "azure_subscription_id", "sub-1")
    monkeypatch.setattr(azure_tools, "_resource_client", lambda: client)
    monkeypatch.setattr(azure_tools, "_api_versions", {})

    result = asyncio.run(
        azure_tools.get_resource.ainvoke(
            {"resource_id": "/subscriptions/sub-1/resourceGroups/rg/providers/Microsoft.Storage/storageAccounts/acct"}
        )
    )

    assert result["status"] == "success"
    assert calls["api_version"] == "2024-01-01"


def test_query_logs_handles_string_columns(monkeypatch) -> None:
    table = SimpleNamespace(
        columns=["TimeGenerated", "Count"],
        rows=[[datetime(2026, 9, 28, tzinfo=timezone.utc), 3]],
    )
    response = SimpleNamespace(status=LogsQueryStatus.SUCCESS, tables=[table])
    monkeypatch.setattr(
        azure_tools, "_logs_client", lambda: SimpleNamespace(query_workspace=lambda **_kwargs: response)
    )

    result = asyncio.run(azure_tools.query_logs.ainvoke({"workspace_id": "w", "query": "Heartbeat | count"}))

    assert result == {
        "status": "success",
        "row_count": 1,
        "rows": [{"TimeGenerated": "2026-09-28T00:00:00+00:00", "Count": 3}],
    }


def test_missing_subscription_returns_an_error_result(monkeypatch) -> None:
    monkeypatch.setattr(azure_tools.settings, "azure_subscription_id", "")

    result = asyncio.run(azure_tools.query_metrics.ainvoke({"resource_id": "/subscriptions/x/r", "metric_names": "cpu"}))

    assert result == {"status": "error", "error": "AZURE_SUBSCRIPTION_ID is not configured"}
