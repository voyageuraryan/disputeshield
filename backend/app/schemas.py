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
    "prior_undisputed_history",  # Phase 15: earlier undisputed orders sharing device/IP/address/account
]


class EvidenceItem(BaseModel):
    id: int
    type: EvidenceType
    source: Literal["paypal", "store", "buyer_portal", "merchant_upload"]
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
    # Phase 13: make-offer types PayPal allows now (allowed_response_options.make_offer.offer_types)
    offer_types: list[str] = []

    @computed_field
    @property
    def can_fight(self) -> bool:
        from app.paypal.disputes import FIGHT_ACTIONS
        return any(a in FIGHT_ACTIONS for a in self.allowed_actions)

    @computed_field
    @property
    def can_settle(self) -> bool:
        """Some way to give in is possible (accept the claim, or an offer PayPal lists)."""
        return self.can_accept_claim or self.can_offer or self.can_replace

    @computed_field
    @property
    def can_accept_claim(self) -> bool:  # SETTLE = full refund via accept-claim
        return "accept_claim" in self.allowed_actions

    @computed_field
    @property
    def can_offer(self) -> bool:  # SETTLE_PARTIAL = make-offer REFUND with an amount
        return "make_offer" in self.allowed_actions and "REFUND" in self.offer_types

    @computed_field
    @property
    def can_replace(self) -> bool:  # REPLACEMENT = make-offer REPLACEMENT_WITHOUT_REFUND
        return "make_offer" in self.allowed_actions and "REPLACEMENT_WITHOUT_REFUND" in self.offer_types

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


Action = Literal["FIGHT", "SETTLE", "SETTLE_PARTIAL", "REPLACEMENT", "GATHER_MORE"]


class Recommendation(BaseModel):
    action: Action
    confidence: Literal["low", "medium", "high"]
    reasoning: str  # plain English, max ~120 words
    cited_evidence_ids: list[int]  # must all exist in CaseFile.evidence
    missing_items: list[str]
    economic_note: str  # amount vs effort vs win odds
    # Phase 13: SETTLE_PARTIAL only: the refund to offer, a decimal string like "25.00"; null for other actions.
    offer_amount: str | None = None

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
        if self.action == "SETTLE" and not case.can_accept_claim:
            raise ValueError(f"SETTLE (accept the claim, full refund) is not possible now; PayPal allows only "
                             f"{case.allowed_actions or 'nothing'}")
        if self.action == "SETTLE_PARTIAL" and not case.can_offer:
            raise ValueError("SETTLE_PARTIAL is not possible now: PayPal doesn't list a REFUND offer (make_offer, "
                             f"inquiry stage only); allowed: {case.allowed_actions or 'nothing'}")
        if self.action == "REPLACEMENT" and not case.can_replace:
            raise ValueError("REPLACEMENT is not possible now: PayPal doesn't list a REPLACEMENT_WITHOUT_REFUND offer")
        if self.action != "SETTLE_PARTIAL" and self.offer_amount not in (None, ""):
            raise ValueError("offer_amount is only for SETTLE_PARTIAL; set it to null")
        return self

    def validate_against(self, case: CaseFile) -> "Recommendation":
        return self.check_citations(case).check_allowed(case)


class SellerNote(BaseModel):
    """LLM-drafted note sent to PayPal with the merchant's response (FIGHT)."""

    note: str                      # factual, under 1,000 characters
    cited_evidence_ids: list[int]  # must all exist in CaseFile.evidence
