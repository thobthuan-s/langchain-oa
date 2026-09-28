"""Customer records for partner-style email triage, backed by an Excel workbook in SharePoint.

The workbook is read through Microsoft Graph with the agent's delegated token.
Code resolves the sender to one customer account before the model runs, and the
lookup tools handed to the model are bound to that account: they take no account
parameter, so an email cannot redirect them to another customer's data. Only
allowlisted columns ever leave this module.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import httpx
from langchain_core.tools import StructuredTool

from config import settings

logger = logging.getLogger(__name__)

GRAPH_ROOT = "https://graph.microsoft.com/v1.0"
GRAPH_SCOPES = ["https://graph.microsoft.com/.default"]

# Columns that may reach the model or the approver. Anything else, such as
# Cases.InternalNotes, is dropped when the workbook is loaded.
ALLOWED_COLUMNS: dict[str, tuple[str, ...]] = {
    "Accounts": ("AccountId", "AccountName", "Domains", "Tier", "AccountOwnerEmail", "SupportSLA", "Status"),
    "Contacts": ("ContactEmail", "AccountId", "Name", "Role", "BillingAuthorized"),
    "Subscriptions": (
        "AccountId", "Product", "Quantity", "AssignedSeats", "Status", "TermEnd", "AutoRenew", "BillingFrequency",
    ),
    "Cases": ("CaseNumber", "AccountId", "Title", "Status", "Priority", "Opened", "LastUpdated", "NextStep"),
    "Invoices": ("InvoiceNumber", "AccountId", "InvoiceDate", "Amount", "Currency", "Status", "DueDate"),
    "Contracts": ("AccountId", "Agreement", "StartDate", "EndDate", "SLA", "RenewalNoticeDays"),
}
_MAX_ROWS_PER_TOOL = 25

Sheets = dict[str, list[dict[str, str]]]


class CustomerRecordsError(Exception):
    """A sanitized failure reading customer records."""


# --- Loading ----------------------------------------------------------------------


def rows_from_values(sheet: str, values: list[list[Any]]) -> list[dict[str, str]]:
    """Turn a header row plus data rows into allowlisted string dictionaries."""

    if not values:
        return []
    header = [str(cell).strip() for cell in values[0]]
    allowed = set(ALLOWED_COLUMNS.get(sheet, ()))
    rows: list[dict[str, str]] = []
    for raw in values[1:]:
        row = {
            name: str(raw[index]).strip()
            for index, name in enumerate(header)
            if name in allowed and index < len(raw)
        }
        if any(row.values()):
            rows.append(row)
    return rows


def share_id(url: str) -> str:
    """Encode a SharePoint or OneDrive URL for the Graph /shares endpoint."""

    encoded = base64.urlsafe_b64encode(url.strip().encode("utf-8")).decode("ascii").rstrip("=")
    return f"u!{encoded}"


class GraphWorkbookSource:
    """Read every known sheet of one workbook with the Graph workbook API."""

    def __init__(
        self,
        workbook_url: str,
        timeout_seconds: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._workbook_url = workbook_url
        self._timeout = timeout_seconds
        self._transport = transport
        self._item_path: str | None = None

    async def load(self, token: str) -> Sheets:
        headers = {"Authorization": f"Bearer {token}"}
        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
            item_path = await self._resolve_item(client, headers)
            sheets: Sheets = {}
            for sheet in ALLOWED_COLUMNS:
                url = f"{GRAPH_ROOT}{item_path}/workbook/worksheets/{quote(sheet)}/usedRange(valuesOnly=true)"
                response = await client.get(url, headers=headers, params={"$select": "text"})
                if response.status_code == 404:
                    sheets[sheet] = []
                    continue
                _raise_for_graph(response, f"read sheet {sheet}")
                sheets[sheet] = rows_from_values(sheet, response.json().get("text") or [])
            return sheets

    async def _resolve_item(self, client: httpx.AsyncClient, headers: dict[str, str]) -> str:
        if self._item_path:
            return self._item_path
        response = await client.get(
            f"{GRAPH_ROOT}/shares/{share_id(self._workbook_url)}/driveItem",
            headers=headers,
            params={"$select": "id,parentReference"},
        )
        _raise_for_graph(response, "resolve the workbook URL")
        item = response.json()
        drive_id = (item.get("parentReference") or {}).get("driveId")
        if not drive_id or not item.get("id"):
            raise CustomerRecordsError("The workbook URL did not resolve to a drive item")
        self._item_path = f"/drives/{drive_id}/items/{item['id']}"
        return self._item_path


def _raise_for_graph(response: httpx.Response, action: str) -> None:
    if response.status_code < 400:
        return
    code = ""
    try:
        code = str((response.json().get("error") or {}).get("code") or "")
    except ValueError:
        pass
    raise CustomerRecordsError(f"Graph could not {action}: HTTP {response.status_code} {code}".strip())


class CustomerRecords:
    """Cached access to the workbook with a pluggable loader for tests."""

    def __init__(self, loader: Callable[[str], Awaitable[Sheets]], cache_seconds: int = 60) -> None:
        self._loader = loader
        self._cache_seconds = cache_seconds
        self._cached: tuple[float, Sheets] | None = None
        self._lock = asyncio.Lock()

    async def sheets(self, token: str) -> Sheets:
        async with self._lock:
            if self._cached and time.time() - self._cached[0] < self._cache_seconds:
                return self._cached[1]
            sheets = await self._loader(token)
            self._cached = (time.time(), sheets)
            return sheets


def records_from_settings() -> CustomerRecords | None:
    if not settings.customer_workbook_url.strip():
        return None
    source = GraphWorkbookSource(settings.customer_workbook_url)
    return CustomerRecords(source.load, settings.customer_records_cache_seconds)


# --- Resolution -------------------------------------------------------------------


@dataclass
class CustomerContext:
    """The one account an email is scoped to, resolved by code from the sender."""

    account: dict[str, str]
    contact: dict[str, str] | None = None
    sheets: Sheets = field(default_factory=dict, repr=False)

    @property
    def account_id(self) -> str:
        return self.account.get("AccountId", "")

    @property
    def verified(self) -> bool:
        return self.contact is not None

    @property
    def billing_authorized(self) -> bool:
        return bool(self.contact) and self.contact.get("BillingAuthorized", "").strip().lower() in {
            "yes", "true", "y", "1",
        }

    @property
    def account_owner(self) -> str:
        return self.account.get("AccountOwnerEmail", "").strip()

    def describe(self) -> str:
        name = self.account.get("AccountName") or self.account_id
        tier = self.account.get("Tier")
        label = f"{name} ({tier})" if tier else name
        if not self.contact:
            return f"{label} · sender is not a listed contact"
        role = self.contact.get("Role") or "contact"
        billing = ", billing authorized" if self.billing_authorized else ""
        return f"{label} · {self.contact.get('Name') or 'contact'}, {role}, verified{billing}"


def _domains(account: dict[str, str]) -> set[str]:
    return {d.strip().lower().lstrip("@") for d in account.get("Domains", "").split(",") if d.strip()}


def resolve_customer(sheets: Sheets, sender_email: str) -> CustomerContext | None:
    """Match the sender to a contact first, then to an account by email domain."""

    sender = sender_email.strip().lower()
    if not sender:
        return None
    accounts = {row.get("AccountId", ""): row for row in sheets.get("Accounts", []) if row.get("AccountId")}
    for contact in sheets.get("Contacts", []):
        if contact.get("ContactEmail", "").strip().lower() == sender:
            account = accounts.get(contact.get("AccountId", ""))
            if account:
                return CustomerContext(account=account, contact=contact, sheets=sheets)
    domain = sender.rsplit("@", 1)[-1] if "@" in sender else ""
    for account in accounts.values():
        if domain and domain in _domains(account):
            return CustomerContext(account=account, contact=None, sheets=sheets)
    return None


# --- Bound tools ------------------------------------------------------------------


def _rows(context: CustomerContext, sheet: str) -> list[dict[str, str]]:
    return [
        {key: value for key, value in row.items() if key != "AccountId"}
        for row in context.sheets.get(sheet, [])
        if row.get("AccountId") == context.account_id
    ][:_MAX_ROWS_PER_TOOL]


def _find(rows: list[dict[str, str]], column: str, value: str) -> dict[str, str] | None:
    wanted = value.strip().lower()
    return next((row for row in rows if row.get(column, "").strip().lower() == wanted), None)


def build_customer_tools(context: CustomerContext) -> list[StructuredTool]:
    """Return read-only lookup tools that can only see the resolved account."""

    def get_account_summary() -> dict[str, Any]:
        """Get the customer's account name, tier, support SLA, and status."""

        account = {k: v for k, v in context.account.items() if k not in {"AccountId", "Domains", "AccountOwnerEmail"}}
        return {"status": "success", "account": account}

    def list_subscriptions() -> dict[str, Any]:
        """List the customer's product subscriptions, seat counts, and renewal dates."""

        return {"status": "success", "subscriptions": _rows(context, "Subscriptions")}

    def list_cases() -> dict[str, Any]:
        """List the customer's support cases with status and the customer-safe next step."""

        return {"status": "success", "cases": _rows(context, "Cases")}

    def get_case(case_number: str) -> dict[str, Any]:
        """Get one of this customer's support cases by case number."""

        case = _find(_rows(context, "Cases"), "CaseNumber", case_number)
        if not case:
            return {"status": "not_found", "error": "No case with that number for this customer."}
        return {"status": "success", "case": case}

    def get_contract() -> dict[str, Any]:
        """Get the customer's agreements, term dates, SLA, and renewal notice period."""

        return {"status": "success", "contracts": _rows(context, "Contracts")}

    def list_invoices() -> dict[str, Any]:
        """List the customer's recent invoices with amount, status, and due date."""

        return {"status": "success", "invoices": _rows(context, "Invoices")}

    def get_invoice(invoice_number: str) -> dict[str, Any]:
        """Get one of this customer's invoices by invoice number."""

        invoice = _find(_rows(context, "Invoices"), "InvoiceNumber", invoice_number)
        if not invoice:
            return {"status": "not_found", "error": "No invoice with that number for this customer."}
        return {"status": "success", "invoice": invoice}

    functions: list[Callable[..., dict[str, Any]]] = [
        get_account_summary,
        list_subscriptions,
        list_cases,
        get_case,
        get_contract,
    ]
    if context.billing_authorized:
        functions += [list_invoices, get_invoice]
    return [StructuredTool.from_function(function) for function in functions]
