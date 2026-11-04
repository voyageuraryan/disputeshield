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
    # Phase 15: checkout signals, stored ONLY as keyed hashes (app/signals.py); never raw IPs or fingerprints
    ip_hash: Optional[str] = Field(default=None, max_length=32, index=True)
    device_hash: Optional[str] = Field(default=None, max_length=32, index=True)
    payer_hash: Optional[str] = Field(default=None, max_length=32, index=True)   # PayPal payer.payer_id
    # Phase 16: set when a demo scenario created this order (DEMO_MODE only); drives its post-payment store data
    demo_scenario: Optional[str] = Field(default=None, max_length=40)


class Shipment(SQLModel, table=True):
    __tablename__ = "shipments"

    id: Optional[int] = Field(default=None, primary_key=True)
    order_id: int = Field(foreign_key="orders.id", index=True)
    carrier: Optional[str] = None
    tracking_number: Optional[str] = None
    status: Optional[str] = None
    delivered_at: Optional[datetime] = None
    delivered_address: Optional[dict] = _json_col()
    # "merchant" when the merchant recorded delivery from the carrier's confirmation (sandbox carriers never
    # report delivery); None when it came from a carrier feed / seed data.
    delivery_recorded_by: Optional[str] = None


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
    buyer_portal = "buyer_portal"  # Phase 9D: the buyer troubleshooting page
    merchant_upload = "merchant_upload"  # Phase 12: a file or fact the merchant added (fields confirmed by them)


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
    # Phase 11 (nullable for the auto-migration): run-scoped evidence. Items an investigation didn't see again become
    # inactive (kept for history); None = written before Phase 11, treated as active.
    last_seen_run: Optional[str] = None
    last_seen_at: Optional[datetime] = None
    active: Optional[bool] = True


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
    # Phase 11: how fresh the data behind this decision was
    data_as_of: Optional[datetime] = None            # when the PayPal dispute copy used was fetched
    stale_sources: Optional[list[str]] = _json_col()  # sources that failed to load this run (previous data used)
    # Phase 13: partial offers and the money calculator
    offer_amount: Optional[Decimal] = Field(default=None, max_digits=12, decimal_places=2)  # SETTLE_PARTIAL
    options: Optional[list[dict]] = _json_col()      # calculator output at analysis time (rules/money.py)
    # Phase 14: the deterministic rule score behind this decision (calibration: rule_score -> P(win))
    rule_score: Optional[float] = None


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
    # Phase 9A: invite eligible buyers to troubleshooting automatically after investigation (off by default).
    # Nullable so the auto-migration can add it to existing databases; None means off.
    auto_invite_troubleshooting: Optional[bool] = Field(default=False)
    updated_at: datetime = Field(default_factory=utcnow)
    # Phase 13: money calculator inputs (nullable for the auto-migration; None -> app.rules.money defaults)
    dispute_fee: Optional[Decimal] = Field(default=None, max_digits=10, decimal_places=2)
    handling_minutes: Optional[int] = None
    hourly_value: Optional[Decimal] = Field(default=None, max_digits=10, decimal_places=2)
    max_offer_pct: Optional[int] = None
    # Phase 17: alerts (None = default) and the MCP access token (only its SHA-256 is stored)
    alert_email_to: Optional[str] = None
    alert_slack: Optional[bool] = None            # default on when SLACK_WEBHOOK_URL is set
    alert_on_proposed: Optional[bool] = None      # default on
    alert_on_troubleshooting: Optional[bool] = None
    alert_on_deadlines: Optional[bool] = None
    mcp_token_hash: Optional[str] = Field(default=None, max_length=64)
    mcp_token_created_at: Optional[datetime] = None


# ---- Phase 9A: buyer troubleshooting portal ------------------------------------

class TSStatus(str, Enum):
    INVITED = "INVITED"                    # link sent, buyer hasn't opened it yet
    ACTIVE = "ACTIVE"                      # buyer opened the link
    AWAITING_PROOF = "AWAITING_PROOF"      # a step asks the buyer for a photo/video/screenshot
    RESOLVED_FIXED = "RESOLVED_FIXED"      # a step fixed the problem
    DEFECT_CONFIRMED = "DEFECT_CONFIRMED"  # steps done, item still faulty
    SAFETY_STOP = "SAFETY_STOP"            # stopped for safety (smell, heat, smoke, damage)
    EXPIRED = "EXPIRED"                    # link past its expiry
    HANDED_TO_MERCHANT = "HANDED_TO_MERCHANT"


TS_OPEN_STATUSES = (TSStatus.INVITED, TSStatus.ACTIVE, TSStatus.AWAITING_PROOF)


class TroubleshootingSession(SQLModel, table=True):
    """A private, token-protected troubleshooting page for one dispute's buyer.
    Only the SHA-256 of the access token is stored; the token itself exists only in the link."""

    __tablename__ = "troubleshooting_sessions"

    id: Optional[int] = Field(default=None, primary_key=True)
    dispute_id: int = Field(foreign_key="disputes.id", index=True, unique=True)
    order_id: Optional[int] = Field(default=None, foreign_key="orders.id")
    product_name: str
    product_model: Optional[str] = None
    status: TSStatus = Field(default=TSStatus.INVITED)
    token_hash: str = Field(max_length=64)
    expires_at: datetime
    invited_at: Optional[datetime] = None
    invited_via: Optional[str] = None  # paypal_message / email
    buyer_last_seen_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=utcnow)
    # Phase 9B (all nullable so the auto-migration can add them)
    language: Optional[str] = None            # buyer's language (BCP-47 code), replies use it
    symptom: Optional[str] = None             # short English summary of the problem (redacted)
    clarifications: Optional[int] = 0         # clarifying questions asked so far (max 2)
    awaiting_fixed_confirm: Optional[bool] = False  # agent thinks it's fixed; buyer must confirm with a button
    plan_generic: Optional[bool] = None       # True = generic safe plan (not product-specific)
    device_hash: Optional[str] = None         # SHA-256 of the device key of the first browser that opened it
    ended_reason: Optional[str] = None
    # Phase 9D
    finished_at: Optional[datetime] = None        # when it reached a final status (toast, re-analysis)
    merchant_active_at: Optional[datetime] = None  # seller replied in the chat: the assistant pauses for a while


class ResearchCache(SQLModel, table=True):
    """Web research per (model, symptom), reused for 24 h so the same fault isn't searched again."""

    __tablename__ = "research_cache"

    id: Optional[int] = Field(default=None, primary_key=True)
    key: str = Field(unique=True, index=True)  # sha256 of normalised model + symptom
    product_model: Optional[str] = None
    symptom: str
    provider: str
    notes: Optional[list[str]] = _json_col()
    sources: Optional[list[dict]] = _json_col()
    created_at: datetime = Field(default_factory=utcnow)


class TSStep(SQLModel, table=True):
    __tablename__ = "ts_steps"

    id: Optional[int] = Field(default=None, primary_key=True)
    session_id: int = Field(foreign_key="troubleshooting_sessions.id", index=True)
    ordinal: int
    title: str
    instruction: str
    why: Optional[str] = None
    safety_level: str = "safe"  # safe / caution
    requires_proof: bool = False
    proof_kind: str = "none"  # photo / video / screenshot / none
    source_ids: Optional[list[int]] = _json_col()  # ts_sources rows this step is based on
    status: str = "PENDING"  # PENDING / DONE_FIXED / DONE_NOT_FIXED / SKIPPED / CANT_DO
    buyer_note: Optional[str] = None
    completed_at: Optional[datetime] = None


class TSMessage(SQLModel, table=True):
    __tablename__ = "ts_messages"

    id: Optional[int] = Field(default=None, primary_key=True)
    session_id: int = Field(foreign_key="troubleshooting_sessions.id", index=True)
    role: str  # buyer / agent / merchant / system
    body: str
    step_id: Optional[int] = Field(default=None, foreign_key="ts_steps.id")
    created_at: datetime = Field(default_factory=utcnow)


class TSProof(SQLModel, table=True):
    __tablename__ = "ts_proofs"

    id: Optional[int] = Field(default=None, primary_key=True)
    session_id: int = Field(foreign_key="troubleshooting_sessions.id", index=True)
    step_id: Optional[int] = Field(default=None, foreign_key="ts_steps.id")
    file_path: str
    mime: str
    size: int
    sha256: str = Field(max_length=64)
    phash: Optional[str] = None
    exif_time: Optional[datetime] = None
    checks: Optional[dict] = _json_col()
    vision_verdict: Optional[dict] = _json_col()
    created_at: datetime = Field(default_factory=utcnow)
    # Phase 9C (nullable for the auto-migration)
    evidence_path: Optional[str] = None   # copy <= 10 MB that could be forwarded to PayPal as evidence
    original_mime: Optional[str] = None   # what the buyer uploaded (e.g. image/heic before conversion)


class TSSource(SQLModel, table=True):
    __tablename__ = "ts_sources"

    id: Optional[int] = Field(default=None, primary_key=True)
    session_id: int = Field(foreign_key="troubleshooting_sessions.id", index=True)
    url: str
    title: Optional[str] = None
    snippet: Optional[str] = None
    retrieved_at: datetime = Field(default_factory=utcnow)
    provider: Optional[str] = None


class UploadStatus(str, Enum):
    PENDING = "PENDING"        # stored + read; the merchant hasn't confirmed the fields yet (not evidence)
    CONFIRMED = "CONFIRMED"    # the merchant confirmed the fields: the investigator turns it into evidence
    DISCARDED = "DISCARDED"    # removed by the merchant (file deleted; row kept for the timeline)


class MerchantUpload(SQLModel, table=True):
    """Phase 12: evidence the merchant adds for a missing checklist item (a file or a typed fact)."""
    __tablename__ = "merchant_uploads"

    id: Optional[int] = Field(default=None, primary_key=True)
    dispute_id: int = Field(foreign_key="disputes.id", index=True)
    evidence_type: str
    status: UploadStatus = UploadStatus.PENDING
    file_path: Optional[str] = None      # ARTIFACTS_DIR/uploads/{dispute}/{id}.{pdf|jpg|png}; None for a typed fact
    mime: Optional[str] = None
    size: Optional[int] = None
    sha256: Optional[str] = Field(default=None, max_length=64)
    original_name: Optional[str] = None
    fact: Optional[str] = None           # the typed fact, verbatim
    extraction: Optional[dict] = _json_col()   # what Gemini read (advisory, never evidence by itself)
    fields: Optional[dict] = _json_col()       # the merchant-confirmed fields
    flags: Optional[list[str]] = _json_col()   # derived deterministically from the confirmed fields
    summary: Optional[str] = None
    created_at: datetime = Field(default_factory=utcnow)
    confirmed_at: Optional[datetime] = None


class DisputeOutcome(SQLModel, table=True):
    """Phase 14: one row per resolved dispute (the analytics fact table). is_demo rows are the labelled demo history
    (seed --demo-history): they have no dispute row, never appear in the queue and are never used for real decisions."""
    __tablename__ = "dispute_outcomes"

    id: Optional[int] = Field(default=None, primary_key=True)
    dispute_id: Optional[int] = Field(default=None, foreign_key="disputes.id", index=True, unique=True)
    is_demo: bool = Field(default=False, index=True)
    ref: str                                    # PayPal dispute id, or DEMO-#### for demo rows
    reason: str
    stage: Optional[str] = None
    amount: Decimal = Field(max_digits=12, decimal_places=2)
    currency: str
    outcome_code: Optional[str] = None          # PayPal dispute_outcome.outcome_code, verbatim
    outcome: str                                # normalised: WON / LOST / CANCELLED / OFFER_ACCEPTED / PAYOUT / NONE
    amount_refunded: Optional[Decimal] = Field(default=None, max_digits=12, decimal_places=2)  # dispute_outcome
    offer_amount: Optional[Decimal] = Field(default=None, max_digits=12, decimal_places=2)
    final_action: str                           # what was sent: FIGHT / SETTLE / SETTLE_PARTIAL / REPLACEMENT / NONE
    recommended_action: Optional[str] = None    # the agent's recommendation for the decision that was acted on
    followed: Optional[bool] = None             # final action == recommendation (None: nothing was recommended)
    rule_score: Optional[float] = None
    created_at: Optional[datetime] = None       # PayPal create_time
    due_at: Optional[datetime] = None
    response_at: Optional[datetime] = None      # when our response went to PayPal (first executed decision)
    resolved_at: Optional[datetime] = None
    recorded_at: datetime = Field(default_factory=utcnow)

