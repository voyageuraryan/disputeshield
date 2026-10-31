"""SQLModel tables. Seven tables carry the whole app (see data model in CLAUDE.md task)."""

from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Optional

from sqlalchemy import JSON, Column
from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _json_col(nullable: bool = True) -> Any:
    return Field(default=None, sa_column=Column(JSON, nullable=nullable))


class Order(SQLModel, table=True):
    __tablename__ = "orders"

    id: Optional[int] = Field(default=None, primary_key=True)
    paypal_order_id: Optional[str] = Field(default=None, index=True)
    capture_id: Optional[str] = Field(default=None, index=True)
    # Sent to PayPal as purchase_units[0].invoice_id; disputes echo it as invoice_number.
    invoice_id: Optional[str] = Field(default=None, unique=True, index=True)
    buyer_email: Optional[str] = None  # known only after capture for storefront orders
    items: Optional[list[dict]] = _json_col()
    amount: Decimal = Field(max_digits=12, decimal_places=2)
    currency: str = Field(max_length=3)
    shipping_address: Optional[dict] = _json_col()
    product_listing: Optional[str] = None
    created_at: datetime = Field(default_factory=utcnow)


class Shipment(SQLModel, table=True):
    __tablename__ = "shipments"

    id: Optional[int] = Field(default=None, primary_key=True)
    order_id: int = Field(foreign_key="orders.id", index=True)
    carrier: Optional[str] = None
    tracking_number: Optional[str] = None
    status: Optional[str] = None
    delivered_at: Optional[datetime] = None
    delivered_address: Optional[dict] = _json_col()


class MessageSender(str, Enum):
    buyer = "buyer"
    seller = "seller"


class Message(SQLModel, table=True):
    __tablename__ = "messages"

    id: Optional[int] = Field(default=None, primary_key=True)
    order_id: int = Field(foreign_key="orders.id", index=True)
    sender: MessageSender
    body: str
    sent_at: datetime = Field(default_factory=utcnow)


class Dispute(SQLModel, table=True):
    __tablename__ = "disputes"

    id: Optional[int] = Field(default=None, primary_key=True)
    paypal_dispute_id: str = Field(unique=True, index=True)
    transaction_id: Optional[str] = Field(default=None, index=True)
    order_id: Optional[int] = Field(default=None, foreign_key="orders.id")
    reason: str  # PayPal dispute_reason enum, e.g. MERCHANDISE_OR_SERVICE_NOT_RECEIVED
    stage: Optional[str] = None  # dispute_life_cycle_stage: INQUIRY/CHARGEBACK/...
    status: str  # PayPal status: OPEN/WAITING_FOR_SELLER_RESPONSE/...
    amount: Decimal = Field(max_digits=12, decimal_places=2)
    currency: str = Field(max_length=3)
    seller_response_due: Optional[datetime] = None
    raw: Optional[dict] = _json_col()
    # Our pipeline's state for this case: PROPOSED / NEEDS_HUMAN / ... (PayPal's is `status`).
    agent_status: Optional[str] = None
    refreshed_at: Optional[datetime] = None  # when `raw` was last fetched from PayPal


class EvidenceSource(str, Enum):
    paypal = "paypal"
    store = "store"


class EvidenceItemRow(SQLModel, table=True):
    __tablename__ = "evidence_items"

    id: Optional[int] = Field(default=None, primary_key=True)
    dispute_id: int = Field(foreign_key="disputes.id", index=True)
    type: str
    source: EvidenceSource
    source_ref: str
    summary: str
    strength: Optional[float] = None
    file_path: Optional[str] = None
    # Structured facts for the rules, e.g. ["address_mismatch"], ["protected:ITEM_NOT_RECEIVED"].
    flags: Optional[list[str]] = _json_col()


class AgentStep(SQLModel, table=True):
    __tablename__ = "agent_steps"

    id: Optional[int] = Field(default=None, primary_key=True)
    dispute_id: int = Field(foreign_key="disputes.id", index=True)
    agent: str
    action: str
    detail: Optional[dict] = _json_col()
    created_at: datetime = Field(default_factory=utcnow)


class DecisionStatus(str, Enum):
    PROPOSED = "PROPOSED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXECUTED = "EXECUTED"
    FAILED = "FAILED"
    SUPERSEDED = "SUPERSEDED"  # a newer analysis of the same dispute replaced this proposal


class Decision(SQLModel, table=True):
    __tablename__ = "decisions"

    id: Optional[int] = Field(default=None, primary_key=True)
    dispute_id: int = Field(foreign_key="disputes.id", index=True)
    recommendation: str  # FIGHT / SETTLE / GATHER_MORE
    confidence: str  # low / medium / high
    reasoning: str
    cited_evidence_ids: list[int] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    draft_note: Optional[str] = None
    status: DecisionStatus = Field(default=DecisionStatus.PROPOSED)
    approved_at: Optional[datetime] = None
    executed_at: Optional[datetime] = None
    paypal_response: Optional[dict] = _json_col()
    run_key: Optional[str] = Field(default=None, index=True)  # LangGraph thread to resume on approval
    # Resolution + approval
    action_request: Optional[dict] = _json_col()   # the exact PayPal call execute will make (built by resolution)
    approved_action: Optional[str] = None          # FIGHT / SETTLE actually approved (merchant may override)
    final_note: Optional[str] = None               # note actually sent (merchant may edit draft_note)
    retry_of: Optional[int] = Field(default=None, foreign_key="decisions.id")  # FAILED decision this one retries
    economic_note: Optional[str] = None             # analyst: amount vs effort vs win odds
    missing_items: Optional[list[str]] = _json_col()  # analyst: evidence still needed
    analysed_threshold: Optional[Decimal] = Field(default=None, max_digits=10, decimal_places=2)  # settle threshold used


class ProcessedEvent(SQLModel, table=True):
    """Idempotency ledger for dispute ingestion.

    key examples:
      "evt:WH-..."                   a webhook event we have fully processed
      "run:PP-D-123:2026-10-31T..."  an agent run queued for this dispute version
    The run key uses the dispute's update_time, so a webhook and the poller seeing
    the same change queue only one agent run.
    """

    __tablename__ = "processed_events"

    id: Optional[int] = Field(default=None, primary_key=True)
    key: str = Field(unique=True, index=True)
    dispute_id: Optional[int] = Field(default=None, foreign_key="disputes.id", index=True)
    kind: str  # webhook / agent_run
    detail: Optional[dict] = _json_col()
    created_at: datetime = Field(default_factory=utcnow)


class MerchantSettings(SQLModel, table=True):
    """Merchant-editable settings (single row, id=1). Env/config values are only the defaults."""

    __tablename__ = "merchant_settings"

    id: Optional[int] = Field(default=1, primary_key=True)
    # Under this amount a weak case (rule_score < 0.7) is recommended for SETTLE. Never sent without approval.
    settle_threshold: Decimal = Field(default=Decimal("25"), max_digits=10, decimal_places=2)
    # The store's refund / return policy as shown to buyers; becomes refund_policy evidence when set.
    refund_policy: Optional[str] = None
    updated_at: datetime = Field(default_factory=utcnow)
