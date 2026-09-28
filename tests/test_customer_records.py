import asyncio
import importlib.util
import inspect
import json
from pathlib import Path

import httpx
import pytest

import customer_records
from customer_records import (
    CustomerRecords,
    GraphWorkbookSource,
    build_customer_tools,
    resolve_customer,
    rows_from_values,
    share_id,
)

DEMO = "demo@partner-test.com"
OWNER = "owner@partner-test.com"


def _sample_values() -> dict[str, list[list[str]]]:
    path = Path(__file__).resolve().parent.parent / "samples" / "partner" / "create_customer_workbook.py"
    spec = importlib.util.spec_from_file_location("create_customer_workbook", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.build(OWNER, DEMO)


def _sheets():
    return {name: rows_from_values(name, values) for name, values in _sample_values().items()}


def _tools(context) -> dict:
    return {tool.name: tool for tool in build_customer_tools(context)}


def test_internal_columns_and_unknown_sheets_are_dropped() -> None:
    sheets = _sheets()

    assert all("InternalNotes" not in row for row in sheets["Cases"])
    assert sheets["DemoNotes"] == []
    assert "credit of about" not in json.dumps(sheets)


def test_contact_match_is_verified_and_domain_match_is_not() -> None:
    sheets = _sheets()

    verified = resolve_customer(sheets, DEMO.upper())
    domain_only = resolve_customer(sheets, "someone@tailspintoys.com")

    assert verified.account_id == "ACC-001" and verified.verified and verified.billing_authorized
    assert domain_only.account_id == "ACC-004" and not domain_only.verified
    assert resolve_customer(sheets, "nobody@unknown.example") is None
    assert resolve_customer(sheets, "") is None


def test_bound_tools_only_see_the_resolved_account() -> None:
    contoso = resolve_customer(_sheets(), DEMO)
    tools = _tools(contoso)

    cases = tools["list_cases"].invoke({})["cases"]
    other_customer_case = tools["get_case"].invoke({"case_number": "NW-1042"})

    assert [case["CaseNumber"] for case in cases] == ["CON-1017"]
    assert other_customer_case["status"] == "not_found"
    assert tools["get_invoice"].invoke({"invoice_number": "INV-58213"})["status"] == "not_found"
    assert tools["get_invoice"].invoke({"invoice_number": "inv-58190"})["invoice"]["Amount"] == "18,750.00"
    summary = tools["get_account_summary"].invoke({})["account"]
    assert "AccountOwnerEmail" not in summary and "Domains" not in summary


def test_tools_take_no_account_parameter() -> None:
    for tool in build_customer_tools(resolve_customer(_sheets(), DEMO)):
        assert not any("account" in name.lower() for name in tool.args), tool.name


def test_invoice_tools_require_billing_authorization() -> None:
    northwind = resolve_customer(_sheets(), "lee.gu@northwindtraders.com")

    assert northwind.verified and not northwind.billing_authorized
    assert {"list_invoices", "get_invoice"}.isdisjoint(_tools(northwind))


def test_share_id_matches_graph_encoding() -> None:
    url = "https://onedrive.live.com/redir?resid=1231244193912!12&authKey=1201919!12921!1"

    assert share_id(url) == "u!aHR0cHM6Ly9vbmVkcml2ZS5saXZlLmNvbS9yZWRpcj9yZXNpZD0xMjMxMjQ0MTkzOTEyITEyJmF1dGhLZXk9MTIwMTkxOSExMjkyMSEx"


def test_graph_source_reads_text_values_and_caches(monkeypatch) -> None:
    values = _sample_values()
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        assert request.headers["Authorization"] == "Bearer token-1"
        if request.url.path.startswith("/v1.0/shares/"):
            return httpx.Response(200, json={"id": "item-1", "parentReference": {"driveId": "drive-1"}})
        sheet = request.url.path.split("/worksheets/")[1].split("/")[0]
        if sheet not in values:
            return httpx.Response(404, json={"error": {"code": "ItemNotFound"}})
        assert request.url.params["$select"] == "text"
        return httpx.Response(200, json={"text": values[sheet]})

    source = GraphWorkbookSource("https://contoso.sharepoint.com/sites/x/Shared%20Documents/book.xlsx",
                                 transport=httpx.MockTransport(handler))
    records = CustomerRecords(source.load, cache_seconds=60)

    async def scenario():
        first = await records.sheets("token-1")
        second = await records.sheets("token-1")
        return first, second

    first, second = asyncio.run(scenario())

    assert first is second
    assert resolve_customer(first, DEMO).account_id == "ACC-001"
    assert sum(path.startswith("/v1.0/shares/") for path in requests) == 1
    assert "/v1.0/drives/drive-1/items/item-1/workbook/worksheets/Cases/usedRange(valuesOnly=true)" in requests


def test_graph_errors_are_sanitized() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": {"code": "accessDenied", "message": "secret detail"}})

    source = GraphWorkbookSource("https://x/y.xlsx", transport=httpx.MockTransport(handler))

    with pytest.raises(customer_records.CustomerRecordsError) as error:
        asyncio.run(source.load("t"))

    assert "403" in str(error.value) and "accessDenied" in str(error.value)
    assert "secret detail" not in str(error.value)


def test_bound_tool_functions_are_sync_and_documented() -> None:
    for tool in build_customer_tools(resolve_customer(_sheets(), DEMO)):
        assert tool.description
        assert not inspect.iscoroutinefunction(tool.func)


def test_site_url_finds_the_workbook_by_exact_name() -> None:
    values = _sample_values()
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        paths.append(path)
        if path == "/v1.0/sites/contoso.sharepoint.com:/sites/SalesandMarketing":
            return httpx.Response(200, json={"id": "site-1"})
        if path.startswith("/v1.0/sites/site-1/drive/root/search"):
            return httpx.Response(200, json={"value": [
                {"id": "old", "name": "PartnerCustomerRecords (1).xlsx", "parentReference": {"driveId": "d"}},
                {"id": "item-9", "name": "PartnerCustomerRecords.xlsx", "parentReference": {"driveId": "drive-9"}},
            ]})
        sheet = path.split("/worksheets/")[1].split("/")[0]
        return httpx.Response(200, json={"text": values.get(sheet, [])})

    source = GraphWorkbookSource("https://contoso.sharepoint.com/sites/SalesandMarketing",
                                 transport=httpx.MockTransport(handler))
    sheets = asyncio.run(source.load("t"))

    assert resolve_customer(sheets, DEMO).account_id == "ACC-001"
    assert "/v1.0/drives/drive-9/items/item-9/workbook/worksheets/Accounts/usedRange(valuesOnly=true)" in paths
    assert not any(path.startswith("/v1.0/shares/") for path in paths)


def test_site_url_with_no_workbook_is_a_clear_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/v1.0/sites/site-1/drive"):
            return httpx.Response(200, json={"value": []})
        return httpx.Response(200, json={"id": "site-1"})

    source = GraphWorkbookSource("https://contoso.sharepoint.com/sites/X", transport=httpx.MockTransport(handler))

    with pytest.raises(customer_records.CustomerRecordsError, match="found 0"):
        asyncio.run(source.load("t"))
