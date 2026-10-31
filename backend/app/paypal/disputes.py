"""PayPal Customer Disputes API v1 (read-only calls).

Checked against the official OpenAPI spec
(developer.paypal.com/api/customer-disputes/v1/schema.json):
- GET /v1/customer/disputes        operationId disputes.list -> dispute_search {items, links}
- GET /v1/customer/disputes/{id}   operationId disputes.get  -> dispute
List query params: page (1..50, default 1), page_size (1..50, default 10),
dispute_state (comma-separated), disputed_transaction_id,
create_time_before/after, update_time_before/after. start_time and
next_page_token are deprecated, so they are not exposed here.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, PrivateAttr

from app.paypal.client import PayPalClient, get_paypal_client

DISPUTES_PATH = "/v1/customer/disputes"

DisputeReason = Literal[
    "MERCHANDISE_OR_SERVICE_NOT_RECEIVED",
    "MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED",
    "UNAUTHORISED",
    "CREDIT_NOT_PROCESSED",
    "DUPLICATE_TRANSACTION",
    "INCORRECT_AMOUNT",
    "PAYMENT_BY_OTHER_MEANS",
    "CANCELED_RECURRING_BILLING",
    "PROBLEM_WITH_REMITTANCE",
    "OTHER",
]
DisputeStatus = Literal[
    "OPEN", "WAITING_FOR_BUYER_RESPONSE", "WAITING_FOR_SELLER_RESPONSE", "UNDER_REVIEW", "RESOLVED", "OTHER"
]
DisputeState = Literal[
    "OPEN_INQUIRIES",
    "REQUIRED_ACTION",
    "REQUIRED_OTHER_PARTY_ACTION",
    "UNDER_PAYPAL_REVIEW",
    "APPEALABLE",
    "RESOLVED",
]
LifecycleStage = Literal["INQUIRY", "CHARGEBACK", "PRE_ARBITRATION", "ARBITRATION"]


class _PayPalModel(BaseModel):
    # PayPal adds fields over time; keep unknown ones instead of failing.
    model_config = ConfigDict(extra="allow")


class Money(_PayPalModel):
    currency_code: str
    value: Decimal


class Link(_PayPalModel):
    href: str
    rel: str
    method: str | None = None


class DisputeSummary(_PayPalModel):
    """dispute_info: one item of the list response."""

    dispute_id: str
    create_time: datetime | None = None
    update_time: datetime | None = None
    # Typed as str (not the Literal) so a new PayPal enum value doesn't break listing.
    reason: str | None = None
    status: str | None = None
    dispute_state: str | None = None
    dispute_amount: Money | None = None
    dispute_life_cycle_stage: str | None = None
    dispute_channel: str | None = None
    buyer_response_due_date: datetime | None = None
    seller_response_due_date: datetime | None = None
    links: list[Link] = []


class DisputeList(_PayPalModel):
    """dispute_search."""

    items: list[DisputeSummary] = []
    links: list[Link] = []


class TransactionInfo(_PayPalModel):
    buyer_transaction_id: str | None = None
    seller_transaction_id: str | None = None
    reference_id: str | None = None
    create_time: datetime | None = None
    transaction_status: str | None = None
    gross_amount: Money | None = None
    invoice_number: str | None = None
    custom: str | None = None


class Dispute(_PayPalModel):
    """dispute: full details. Nested objects we don't model yet stay as dicts via extra='allow'."""

    dispute_id: str
    create_time: datetime | None = None
    update_time: datetime | None = None
    disputed_transactions: list[TransactionInfo] = []
    reason: str | None = None
    status: str | None = None
    dispute_amount: Money | None = None
    dispute_life_cycle_stage: str | None = None
    dispute_channel: str | None = None
    buyer_response_due_date: datetime | None = None
    seller_response_due_date: datetime | None = None
    messages: list[dict] = []
    evidences: list[dict] = []
    offer: dict | None = None
    allowed_response_options: dict | None = None
    links: list[Link] = []
    # The JSON exactly as PayPal returned it (stored in disputes.raw).
    _raw: dict = PrivateAttr(default_factory=dict)

    @property
    def raw(self) -> dict:
        return self._raw or self.model_dump(mode="json")


async def list_disputes(
    *,
    page: int | None = None,
    page_size: int | None = None,
    dispute_state: list[str] | str | None = None,
    disputed_transaction_id: str | None = None,
    create_time_after: str | None = None,
    create_time_before: str | None = None,
    update_time_after: str | None = None,
    update_time_before: str | None = None,
    client: PayPalClient | None = None,
) -> DisputeList:
    if page is not None and not 1 <= page <= 50:
        raise ValueError("page must be 1..50")
    if page_size is not None and not 1 <= page_size <= 50:
        raise ValueError("page_size must be 1..50")
    if isinstance(dispute_state, list):
        dispute_state = ",".join(dispute_state)
    params = {
        "page": page,
        "page_size": page_size,
        "dispute_state": dispute_state,
        "disputed_transaction_id": disputed_transaction_id,
        "create_time_after": create_time_after,
        "create_time_before": create_time_before,
        "update_time_after": update_time_after,
        "update_time_before": update_time_before,
    }
    data = await (client or get_paypal_client()).get(DISPUTES_PATH, params=params)
    return DisputeList.model_validate(data or {})


async def get_dispute(dispute_id: str, *, client: PayPalClient | None = None) -> Dispute:
    if not dispute_id or not all(c.isalnum() or c == "-" for c in dispute_id):
        raise ValueError(f"invalid dispute id: {dispute_id!r}")  # spec pattern ^[A-Za-z0-9-]+$
    data = await (client or get_paypal_client()).get(f"{DISPUTES_PATH}/{dispute_id}")
    dispute = Dispute.model_validate(data)
    dispute._raw = data
    return dispute


# ---- what the merchant may do now ----------------------------------------------
# PayPal signals allowed actions through the dispute's HATEOAS links (spec: "use the
# appeal link ... If this link does not appear, you cannot appeal"; provide-supporting-info
# likewise). allowed_response_options only covers accept_claim / make_offer /
# acknowledge_return_item. Seen live on a CHARGEBACK under PayPal review:
# rels = self, accept_claim, adjudicate, provide_supporting_info, require_evidence.

# Ways to argue the merchant's side.
FIGHT_ACTIONS = ("provide_evidence", "provide_supporting_info", "appeal")
# Ways to concede or settle (make_offer: INQUIRY stage only, per spec).
SETTLE_ACTIONS = ("accept_claim", "make_offer")
# Sandbox-only test endpoints that simulate PayPal; never merchant actions.
SANDBOX_ONLY_ACTIONS = ("adjudicate", "require_evidence")
MERCHANT_ACTIONS = FIGHT_ACTIONS + SETTLE_ACTIONS + ("acknowledge_return_item", "send_message", "escalate")


def allowed_actions(raw: dict | None) -> list[str]:
    """Merchant actions PayPal currently allows on this dispute, from its HATEOAS links
    and allowed_response_options. Sandbox-only simulation endpoints are excluded."""
    raw = raw or {}
    rels = {(l.get("rel") or "").replace("-", "_") for l in raw.get("links") or []}
    rels |= set((raw.get("allowed_response_options") or {}).keys())
    if not raw.get("links") and raw.get("status") == "WAITING_FOR_SELLER_RESPONSE":
        # No links stored (older copy): the spec says sellers may provide evidence in this status.
        rels.add("provide_evidence")
    return [a for a in MERCHANT_ACTIONS if a in rels]


# ---- responding to a dispute (money-moving: rule 5) -----------------------------
# Request formats from developer.paypal.com/platforms/disputes/handle-disputes/use-disputes-api:
#   provide-evidence       multipart/form-data: part "input" (application/json)
#                          {"evidences": [{"evidence_type", "evidence_info": {"tracking_info": [...]}, "notes"}]}
#                          + file parts ("file1", ...). Allowed when status WAITING_FOR_SELLER_RESPONSE.
#   provide-supporting-info  JSON {"notes"} or multipart "input" {"notes"} + files. Allowed while PayPal
#                          reviews a CHARGEBACK / PRE_ARBITRATION / ARBITRATION.
#   accept-claim           JSON {"note"}; PayPal refunds the buyer.
# Files: .jpg .jpeg .gif .png .pdf, < 10 MB each, <= 50 MB per call.
# None of these document PayPal-Request-Id, so the client never re-sends them after an
# ambiguous failure (5xx / timeout); reconcile with get_dispute() instead.

import json as _json
from dataclasses import dataclass as _dataclass
from pathlib import Path as _Path

MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_TOTAL_BYTES = 50 * 1024 * 1024
ALLOWED_FILE_SUFFIXES = {".jpg", ".jpeg", ".gif", ".png", ".pdf"}


class NotApprovedError(Exception):
    """Raised when a money-moving call is attempted without an APPROVED decision (rule 5)."""


def require_approved(decision) -> None:
    from app.models import DecisionStatus  # local import: models import nothing from here

    status = getattr(decision, "status", None)
    if decision is None or status != DecisionStatus.APPROVED:
        raise NotApprovedError(f"refusing PayPal action: decision status is {getattr(status, 'value', status)!r}, "
                               f"not APPROVED")


@_dataclass(frozen=True)
class Attachment:
    path: str
    content_type: str = "application/pdf"


def _files(input_json: dict, attachments: list[Attachment]) -> dict:
    files = {"input": (None, _json.dumps(input_json), "application/json")}
    total = 0
    for i, a in enumerate(attachments, start=1):
        p = _Path(a.path)
        if p.suffix.lower() not in ALLOWED_FILE_SUFFIXES:
            raise ValueError(f"{p.name}: PayPal accepts only {sorted(ALLOWED_FILE_SUFFIXES)}")
        data = p.read_bytes()
        if len(data) >= MAX_FILE_BYTES:
            raise ValueError(f"{p.name}: must be under 10 MB")
        total += len(data)
        files[f"file{i}"] = (p.name, data, a.content_type)
    if total > MAX_TOTAL_BYTES:
        raise ValueError("attachments exceed 50 MB per call")
    return files


def _check_id(dispute_id: str) -> None:
    if not dispute_id or not all(c.isalnum() or c == "-" for c in dispute_id):
        raise ValueError(f"invalid dispute id: {dispute_id!r}")


async def provide_evidence(decision, dispute_id: str, evidences: list[dict], attachments: list[Attachment], *,
                           client: PayPalClient | None = None) -> dict:
    require_approved(decision)
    _check_id(dispute_id)
    return await (client or get_paypal_client()).post(
        f"{DISPUTES_PATH}/{dispute_id}/provide-evidence", files=_files({"evidences": evidences}, attachments))


async def provide_supporting_info(decision, dispute_id: str, notes: str, attachments: list[Attachment], *,
                                  client: PayPalClient | None = None) -> dict:
    require_approved(decision)
    _check_id(dispute_id)
    return await (client or get_paypal_client()).post(
        f"{DISPUTES_PATH}/{dispute_id}/provide-supporting-info", files=_files({"notes": notes}, attachments))


async def accept_claim(decision, dispute_id: str, note: str, *, client: PayPalClient | None = None) -> dict:
    require_approved(decision)
    _check_id(dispute_id)
    return await (client or get_paypal_client()).post(f"{DISPUTES_PATH}/{dispute_id}/accept-claim",
                                                      json={"note": note})


# ---- sandbox-only simulators (PayPal's side of the case; not merchant money movement) ----

async def sandbox_require_evidence(dispute_id: str, action: str, *, client: PayPalClient | None = None) -> dict:
    """Sandbox only: UNDER_REVIEW -> WAITING_FOR_SELLER_RESPONSE (SELLER_EVIDENCE) or ..._BUYER_..."""
    if action not in ("SELLER_EVIDENCE", "BUYER_EVIDENCE"):
        raise ValueError("action must be SELLER_EVIDENCE or BUYER_EVIDENCE")
    _check_id(dispute_id)
    return await (client or get_paypal_client()).post(f"{DISPUTES_PATH}/{dispute_id}/require-evidence",
                                                      json={"action": action})


async def sandbox_adjudicate(dispute_id: str, outcome: str, *, client: PayPalClient | None = None) -> dict:
    """Sandbox only: settle the dispute in BUYER_FAVOR or SELLER_FAVOR."""
    if outcome not in ("BUYER_FAVOR", "SELLER_FAVOR"):
        raise ValueError("outcome must be BUYER_FAVOR or SELLER_FAVOR")
    _check_id(dispute_id)
    return await (client or get_paypal_client()).post(f"{DISPUTES_PATH}/{dispute_id}/adjudicate",
                                                      json={"adjudication_outcome": outcome})
