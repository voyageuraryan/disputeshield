"""Core Pydantic schemas shared by rules, agents and API."""

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, computed_field

EvidenceType = Literal[
    "tracking",
    "delivery_confirmation",
    "order",
    "invoice",
    "listing",
    "buyer_message",
    "refund_policy",
    "buyer_history",
    "troubleshooting_record",  # Phase 9D: result of the buyer troubleshooting page
]


class EvidenceItem(BaseModel):
    id: int
    type: EvidenceType
    source: Literal["paypal", "store", "buyer_portal"]
    source_ref: str  # e.g. "paypal:tracker/ABC123" or "store:order/42"
    summary: str
    flags: list[str] = []  # structured facts for the rules, e.g. "address_mismatch"


class CaseFile(BaseModel):
    dispute_id: str
    reason: str  # PayPal reason code, e.g. MERCHANDISE_OR_SERVICE_NOT_RECEIVED
    amount: Decimal
    currency: str
    due_date: datetime | None
    order_id: int | None
    evidence: list[EvidenceItem]
    # Where the dispute stands at PayPal and what the merchant may do now.
    status: str | None = None          # e.g. UNDER_REVIEW, WAITING_FOR_SELLER_RESPONSE
    stage: str | None = None           # INQUIRY / CHARGEBACK / PRE_ARBITRATION / ARBITRATION
    dispute_state: str | None = None   # e.g. UNDER_PAYPAL_REVIEW, REQUIRED_ACTION
    allowed_actions: list[str] = []    # from PayPal's HATEOAS links, e.g. ["accept_claim", "provide_supporting_info"]
    # Phase 11: freshness. data_as_of = when the PayPal dispute copy was fetched; stale_sources = sources that failed
    # to load in this run (their previous items are kept, flagged "stale:<time of last success>").
    data_as_of: datetime | None = None
    stale_sources: list[str] = []

    @computed_field
    @property
    def can_fight(self) -> bool:
        from app.paypal.disputes import FIGHT_ACTIONS
        return any(a in FIGHT_ACTIONS for a in self.allowed_actions)

    @computed_field
    @property
    def can_settle(self) -> bool:
        from app.paypal.disputes import SETTLE_ACTIONS
        return any(a in SETTLE_ACTIONS for a in self.allowed_actions)

    @property
    def evidence_ids(self) -> set[int]:
        return {e.id for e in self.evidence}


class ChecklistResult(BaseModel):
    required: list[str]  # evidence types the rule requires
    present: list[str]
    missing: list[str]
    rule_score: float = Field(ge=0, le=1)  # deterministic
    red_flags: list[str] = []      # e.g. "address_mismatch"; each one lowers rule_score
    reason_supported: bool = True  # False -> no rule for this PayPal reason; a human decides
    # Phase 9D (troubleshooting): plain-English facts for the baseline's "why" (also shown to the LLM) ...
    notes: list[str] = []
    # ... facts for the merchant only (never sent to the LLM, never a penalty), e.g. suspect proof checks
    merchant_notes: list[str] = []
    safety_review: bool = False    # the buyer reported a safety problem: always GATHER_MORE, a person reviews


class Recommendation(BaseModel):
    action: Literal["FIGHT", "SETTLE", "GATHER_MORE"]
    confidence: Literal["low", "medium", "high"]
    reasoning: str  # plain English, max ~120 words
    cited_evidence_ids: list[int]  # must all exist in CaseFile.evidence
    missing_items: list[str]
    economic_note: str  # amount vs effort vs win odds

    def check_citations(self, case: CaseFile) -> "Recommendation":
        """Hard rule 4: the LLM may only cite evidence that exists in the case file."""
        unknown = set(self.cited_evidence_ids) - case.evidence_ids
        if unknown:
            raise ValueError(f"Recommendation cites unknown evidence ids: {sorted(unknown)}")
        return self

    def check_allowed(self, case: CaseFile) -> "Recommendation":
        """Never recommend an action PayPal won't accept right now (GATHER_MORE is always possible)."""
        if self.action == "FIGHT" and not case.can_fight:
            raise ValueError(f"FIGHT is not possible now; PayPal allows only {case.allowed_actions or 'nothing'}")
        if self.action == "SETTLE" and not case.can_settle:
            raise ValueError(f"SETTLE is not possible now; PayPal allows only {case.allowed_actions or 'nothing'}")
        return self

    def validate_against(self, case: CaseFile) -> "Recommendation":
        return self.check_citations(case).check_allowed(case)


class SellerNote(BaseModel):
    """LLM-drafted note sent to PayPal with the merchant's response (FIGHT)."""

    note: str                      # factual, under 1,000 characters
    cited_evidence_ids: list[int]  # must all exist in CaseFile.evidence
