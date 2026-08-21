"""Read-only Azure Resource Manager and Azure Monitor tools."""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from azure.identity import DefaultAzureCredential
from azure.monitor.query import LogsQueryClient, LogsQueryStatus, MetricsQueryClient
from langchain.tools import tool

try:
    from azure.mgmt.resource import ResourceManagementClient
except ImportError:  # azure-mgmt-resource 26+
    from azure.mgmt.resource.resources import ResourceManagementClient

from config import settings

_RESOURCE_TYPE_PATTERN = re.compile(r"^[A-Za-z0-9.]+(?:/[A-Za-z0-9._-]+)+$")
_MAX_RESOURCES = 100
_MAX_LOG_ROWS = 50


def _subscription_id() -> str:
    if not settings.azure_subscription_id:
        raise ValueError("AZURE_SUBSCRIPTION_ID is not configured")
    return settings.azure_subscription_id


def _resource_client() -> ResourceManagementClient:
    return ResourceManagementClient(DefaultAzureCredential(), _subscription_id())


def _safe_error(exc: Exception) -> dict[str, str]:
    return {"status": "error", "error": str(exc)[:800]}


@tool
async def list_resource_groups() -> dict[str, Any]:
    """List Azure resource groups in the configured subscription."""

    def _list() -> dict[str, Any]:
        groups = [
            {
                "name": group.name,
                "location": group.location,
                "tags": dict(group.tags or {}),
            }
            for group in _resource_client().resource_groups.list()
        ]
        return {"status": "success", "count": len(groups), "resource_groups": groups}

    try:
        return await asyncio.to_thread(_list)
    except Exception as exc:  # noqa: BLE001
        return _safe_error(exc)


@tool
async def list_resources(
    resource_group: str | None = None,
    resource_type: str | None = None,
) -> dict[str, Any]:
    """List up to 100 Azure resources, optionally filtered by resource group and type."""

    if resource_type and not _RESOURCE_TYPE_PATTERN.fullmatch(resource_type):
        return {"status": "error", "error": "resource_type has an invalid Azure resource type format"}

    def _list() -> dict[str, Any]:
        client = _resource_client()
        filter_value = f"resourceType eq '{resource_type}'" if resource_type else None
        iterator = (
            client.resources.list_by_resource_group(resource_group, filter=filter_value)
            if resource_group
            else client.resources.list(filter=filter_value)
        )

        resources: list[dict[str, Any]] = []
        for resource in iterator:
            resource_id = str(resource.id or "")
            id_parts = resource_id.split("/")
            resources.append(
                {
                    "name": resource.name,
                    "type": resource.type,
                    "location": resource.location,
                    "resource_group": id_parts[4] if len(id_parts) > 4 else None,
                    "id": resource_id,
                }
            )
            if len(resources) >= _MAX_RESOURCES:
                break
        return {"status": "success", "count": len(resources), "resources": resources}

    try:
        return await asyncio.to_thread(_list)
    except Exception as exc:  # noqa: BLE001
        return _safe_error(exc)


@tool
async def get_resource(resource_id: str) -> dict[str, Any]:
    """Read details for one Azure resource using its full resource ID."""

    expected_prefix = f"/subscriptions/{_subscription_id()}/"
    if not resource_id.lower().startswith(expected_prefix.lower()):
        return {"status": "error", "error": "resource_id must belong to the configured subscription"}

    def _get() -> dict[str, Any]:
        resource = _resource_client().resources.get_by_id(resource_id, api_version="2024-03-01")
        return {
            "status": "success",
            "resource": {
                "name": resource.name,
                "type": resource.type,
                "location": resource.location,
                "kind": resource.kind,
                "tags": dict(resource.tags or {}),
                "properties": resource.properties or {},
            },
        }

    try:
        return await asyncio.to_thread(_get)
    except Exception as exc:  # noqa: BLE001
        return _safe_error(exc)


@tool
async def query_logs(workspace_id: str, query: str, timespan_hours: int = 24) -> dict[str, Any]:
    """Run a bounded read-only KQL query against an Azure Log Analytics workspace."""

    cleaned_query = query.strip()
    if not cleaned_query or len(cleaned_query) > 4000:
        return {"status": "error", "error": "query must contain 1 to 4000 characters"}
    if cleaned_query.startswith("."):
        return {"status": "error", "error": "Kusto management commands are not allowed"}
    bounded_hours = max(1, min(timespan_hours, 168))

    def _query() -> dict[str, Any]:
        end_time = datetime.now(timezone.utc)
        response = LogsQueryClient(DefaultAzureCredential()).query_workspace(
            workspace_id=workspace_id,
            query=cleaned_query,
            timespan=(end_time - timedelta(hours=bounded_hours), end_time),
        )
        if response.status != LogsQueryStatus.SUCCESS:
            return {"status": "partial", "error": str(response.partial_error)[:800]}

        rows: list[dict[str, Any]] = []
        for table in response.tables:
            columns = [column.name for column in table.columns]
            for row in table.rows:
                rows.append(dict(zip(columns, row)))
                if len(rows) >= _MAX_LOG_ROWS:
                    break
            if len(rows) >= _MAX_LOG_ROWS:
                break
        return {"status": "success", "row_count": len(rows), "rows": rows}

    try:
        return await asyncio.to_thread(_query)
    except Exception as exc:  # noqa: BLE001
        return _safe_error(exc)


@tool
async def query_metrics(
    resource_id: str,
    metric_names: str,
    timespan_hours: int = 1,
    aggregation: str = "Average",
) -> dict[str, Any]:
    """Read Azure Monitor metrics for one resource. Comma-separate multiple metric names."""

    allowed_aggregations = {"Average", "Maximum", "Minimum", "Total", "Count"}
    if aggregation not in allowed_aggregations:
        return {"status": "error", "error": f"aggregation must be one of {sorted(allowed_aggregations)}"}

    expected_prefix = f"/subscriptions/{_subscription_id()}/"
    if not resource_id.lower().startswith(expected_prefix.lower()):
        return {"status": "error", "error": "resource_id must belong to the configured subscription"}

    names = [name.strip() for name in metric_names.split(",") if name.strip()][:10]
    if not names:
        return {"status": "error", "error": "at least one metric name is required"}
    bounded_hours = max(1, min(timespan_hours, 168))

    def _query() -> dict[str, Any]:
        end_time = datetime.now(timezone.utc)
        response = MetricsQueryClient(DefaultAzureCredential()).query_resource(
            resource_uri=resource_id,
            metric_names=names,
            timespan=(end_time - timedelta(hours=bounded_hours), end_time),
            aggregations=[aggregation],
        )
        results: dict[str, Any] = {}
        for metric in response.metrics:
            points: list[dict[str, Any]] = []
            for series in metric.timeseries:
                for point in series.data:
                    value = getattr(point, aggregation.lower(), None)
                    if value is not None:
                        points.append(
                            {
                                "timestamp": point.timestamp.isoformat() if point.timestamp else None,
                                "value": value,
                            }
                        )
            results[metric.name] = {"unit": str(metric.unit), "data_points": points[-20:]}
        return {"status": "success", "metrics": results}

    try:
        return await asyncio.to_thread(_query)
    except Exception as exc:  # noqa: BLE001
        return _safe_error(exc)


AZURE_TOOLS = [
    list_resource_groups,
    list_resources,
    get_resource,
    query_logs,
    query_metrics,
]
