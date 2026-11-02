"""Deterministic evidence rules (rule 4: rules decide what evidence a reason needs; the LLM explains).

Reason codes are the exact Disputes v1 `dispute_reason` enum values
(developer.paypal.com/api/customer-disputes/v1). Note PayPal's British spelling:
UNAUTHORISED, not UNAUTHORIZED.

rule_score = 0.7 * (required present / required) + 0.3 * (supporting present / supporting)
             - sum(red-flag penalties), clamped to 0..1, rounded to 2 places.

Red flags come only from structured evidence flags set by the investigator
(e.g. "address_mismatch"), never from free text. Free text (a buyer message, the
listing) is the LLM's to read; it may move the recommendation one step, citing it.

Buyer troubleshooting (Phase 9D, SNAD only). The investigator adds one "troubleshooting_record" item when
the buyer's session has finished, flagged with exactly one outcome (ts_fixed / ts_defect_confirmed /
ts_not_attempted / ts_partial / ts_safety_stop) plus ts_proof_suspect when a proof check fired:
- the record is an *optional* supporting item: it counts (numerator and denominator) only when present, so
  cases that could never have troubleshooting (non-electronics) don't lose score for its absence;
- ts_fixed: supporting + "buyer confirmed the item works after troubleshooting" in the baseline's why;
- ts_defect_confirmed without ts_proof_suspect: red flag defect_confirmed (-0.3);
- ts_not_attempted (after a sent invite): supporting, no penalty, noted;
- ts_safety_stop: the baseline is always GATHER_MORE ("safety issue: review personally");
- ts_proof_suspect: never a penalty for the buyer; a merchant-only note (not sent to the LLM).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

from app.schemas import CaseFile, ChecklistResult

INR = "MERCHANDISE_OR_SERVICE_NOT_RECEIVED"
SNAD = "MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED"
UNAUTHORISED = "UNAUTHORISED"

# Exact decimal arithmetic so the score never depends on float noise (0.925 must not become 0.92).
REQUIRED_WEIGHT = Decimal("0.7")
SUPPORTING_WEIGHT = Decimal("0.3")
FIGHT_AT = 0.7   # rule_score >= this -> FIGHT
SETTLE_BELOW = 0.4  # rule_score < this -> SETTLE

Action = Literal["FIGHT", "SETTLE", "GATHER_MORE"]
LADDER: tuple[Action, ...] = ("SETTLE", "GATHER_MORE", "FIGHT")


@dataclass(frozen=True)
class RedFlag:
    code: str
    penalty: str  # decimal string, e.g. "0.4"
    description: str


@dataclass(frozen=True)
class ReasonRule:
    reason: str
    label: str
    required: tuple[str, ...]
    supporting: tuple[str, ...]
    # PayPal seller-protection category that should cover this reason (from the capture).
    protection_category: str | None = None
    red_flags: tuple[RedFlag, ...] = field(default_factory=tuple)
    # Supporting items that only count when present (they can't exist for every case of this reason).
    optional_supporting: tuple[str, ...] = field(default_factory=tuple)


ADDRESS_MISMATCH = RedFlag("address_mismatch", "0.4",
                           "Carrier delivered to a different address than the order's shipping address")
TRACKER_CANCELLED = RedFlag("tracker_cancelled", "0.2", "The tracking number registered with PayPal was cancelled")
DEFECT_CONFIRMED = RedFlag("defect_confirmed", "0.3",
                           "The buyer tried every troubleshooting step and showed the fault: a case for settling "
                           "or offering a replacement")
TS_RECORD = "troubleshooting_record"
TS_FIXED_NOTE = "buyer confirmed the item works after troubleshooting"
TS_NOT_ATTEMPTED_NOTE = "the buyer was invited to troubleshoot but didn't try the steps"
TS_PARTIAL_NOTE = "the buyer started troubleshooting but didn't finish"
TS_DEFECT_NOTE = "the buyer tried every troubleshooting step and showed the fault"
SAFETY_WHY = "safety issue: review personally (the buyer reported a safety problem during troubleshooting)"
TS_SUSPECT_NOTE = ("a troubleshooting proof check flagged something (duplicate file, photo taken before the dispute, "
                   "or the advisory photo check said it doesn't show the step): review the uploads yourself; "
                   "no penalty was applied")

RULES: dict[str, ReasonRule] = {
    INR: ReasonRule(
        reason=INR, label="Item not received",
        # Proof the item was shipped and delivered.
        required=("tracking", "delivery_confirmation"),
        supporting=("invoice", "order", "buyer_history", "buyer_message"),
        protection_category="ITEM_NOT_RECEIVED",
        red_flags=(ADDRESS_MISMATCH, TRACKER_CANCELLED,
                   RedFlag("not_protected", "0.1", "PayPal seller protection does not cover item-not-received here")),
    ),
    SNAD: ReasonRule(
        reason=SNAD, label="Not as described",
        # What was promised (listing), what was sold (order), and the return/refund terms.
        required=("listing", "order", "refund_policy"),
        supporting=("tracking", "delivery_confirmation", "buyer_message", "buyer_history", "invoice"),
        red_flags=(DEFECT_CONFIRMED,),
        optional_supporting=(TS_RECORD,),
    ),
    UNAUTHORISED: ReasonRule(
        reason=UNAUTHORISED, label="Unauthorized transaction",
        # Proof of delivery to the transaction's address and seller protection for unauthorized payments.
        required=("tracking", "delivery_confirmation", "invoice"),
        supporting=("order", "buyer_history", "buyer_message"),
        protection_category="UNAUTHORIZED_TRANSACTION",  # PayPal's category enum uses the Z spelling
        red_flags=(ADDRESS_MISMATCH, TRACKER_CANCELLED,
                   RedFlag("not_protected", "0.3", "PayPal seller protection does not cover unauthorized payments here")),
    ),
}

SUPPORTED_REASONS = tuple(RULES)


def _flags(case: CaseFile, evidence_type: str) -> set[str]:
    return {f for e in case.evidence if e.type == evidence_type for f in e.flags}


def detect_red_flags(rule: ReasonRule, case: CaseFile) -> list[RedFlag]:
    found: list[RedFlag] = []
    codes = {rf.code: rf for rf in rule.red_flags}
    if "address_mismatch" in codes and "address_mismatch" in _flags(case, "delivery_confirmation"):
        found.append(codes["address_mismatch"])
    if "tracker_cancelled" in codes and "tracker_cancelled" in _flags(case, "tracking"):
        found.append(codes["tracker_cancelled"])
    if "defect_confirmed" in codes:
        ts = _flags(case, TS_RECORD)
        if "ts_defect_confirmed" in ts and "ts_proof_suspect" not in ts:
            found.append(codes["defect_confirmed"])
    if "not_protected" in codes and rule.protection_category:
        invoice_flags = _flags(case, "invoice")
        # Only judge protection when we actually have the capture's data.
        if invoice_flags and f"protected:{rule.protection_category}" not in invoice_flags:
            found.append(codes["not_protected"])
    return found


def evaluate(case: CaseFile) -> ChecklistResult:
    rule = RULES.get(case.reason)
    if rule is None:
        return ChecklistResult(required=[], present=sorted({e.type for e in case.evidence}), missing=[],
                               rule_score=0.0, red_flags=[], reason_supported=False)
    have = {e.type for e in case.evidence}
    supporting = rule.supporting + tuple(t for t in rule.optional_supporting if t in have)
    present = [t for t in rule.required + supporting if t in have]
    missing = [t for t in rule.required if t not in have]
    req = Decimal(sum(t in have for t in rule.required)) / len(rule.required)
    sup = Decimal(sum(t in have for t in supporting)) / len(supporting) if supporting else Decimal(0)
    flags = detect_red_flags(rule, case)
    score = REQUIRED_WEIGHT * req + SUPPORTING_WEIGHT * sup - sum((Decimal(f.penalty) for f in flags), Decimal(0))
    score = min(Decimal(1), max(Decimal(0), score)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    notes, merchant_notes, safety = troubleshooting_notes(rule, case)
    return ChecklistResult(required=list(rule.required), present=present, missing=missing,
                           rule_score=float(score), red_flags=[f.code for f in flags], notes=notes,
                           merchant_notes=merchant_notes, safety_review=safety)


def troubleshooting_notes(rule: ReasonRule, case: CaseFile) -> tuple[list[str], list[str], bool]:
    """(notes for the baseline why, merchant-only notes, safety review?) from the troubleshooting record."""
    if TS_RECORD not in rule.optional_supporting:
        return [], [], False
    ts = _flags(case, TS_RECORD)
    notes = []
    if "ts_fixed" in ts:
        notes.append(TS_FIXED_NOTE)
    if "ts_not_attempted" in ts:
        notes.append(TS_NOT_ATTEMPTED_NOTE)
    if "ts_partial" in ts:
        notes.append(TS_PARTIAL_NOTE)
    if "ts_defect_confirmed" in ts:
        notes.append(TS_DEFECT_NOTE)
    merchant = [TS_SUSPECT_NOTE] if "ts_proof_suspect" in ts else []
    return notes, merchant, "ts_safety_stop" in ts


def red_flag_description(reason: str, code: str) -> str:
    rule = RULES.get(reason)
    for rf in rule.red_flags if rule else ():
        if rf.code == code:
            return rf.description
    return code


# ---- economic rule (CLAUDE.md task spec) ---------------------------------------

@dataclass(frozen=True)
class Baseline:
    action: Action
    why: str


def economic_baseline(checklist: ChecklistResult, amount: Decimal, threshold: Decimal,
                      *, can_fight: bool = True, can_settle: bool = True) -> Baseline:
    """FIGHT if rule_score >= 0.7. SETTLE if rule_score < 0.4, or amount < threshold with a weak
    case (rule_score < 0.7). Otherwise GATHER_MORE. Then, if PayPal doesn't allow the action
    right now, fall back to GATHER_MORE. A safety report from buyer troubleshooting always means GATHER_MORE:
    a person reviews it, nothing is fought or settled automatically. Troubleshooting notes extend the why."""
    if getattr(checklist, "safety_review", False):
        return Baseline("GATHER_MORE", SAFETY_WHY)
    b = _baseline(checklist, amount, threshold, can_fight=can_fight, can_settle=can_settle)
    notes = getattr(checklist, "notes", None) or []
    return Baseline(b.action, b.why + "".join(f"; {n}" for n in notes)) if notes else b


def _baseline(checklist: ChecklistResult, amount: Decimal, threshold: Decimal,
              *, can_fight: bool, can_settle: bool) -> Baseline:
    s = checklist.rule_score
    if s >= FIGHT_AT:
        b = Baseline("FIGHT", f"rule score {s:.2f} >= {FIGHT_AT}")
    elif s < SETTLE_BELOW:
        b = Baseline("SETTLE", f"rule score {s:.2f} < {SETTLE_BELOW}")
    elif amount < threshold:
        b = Baseline("SETTLE", f"amount {amount} is under the {threshold} threshold and the case is weak "
                               f"(rule score {s:.2f} < {FIGHT_AT})")
    else:
        b = Baseline("GATHER_MORE", f"rule score {s:.2f} is between {SETTLE_BELOW} and {FIGHT_AT}"
                                    + (f"; missing {', '.join(checklist.missing)}" if checklist.missing else ""))
    if b.action == "FIGHT" and not can_fight:
        return Baseline("GATHER_MORE", b.why + ("; but PayPal does not accept evidence at this stage"
                                                " (settling is still possible)" if can_settle
                                                else "; but PayPal does not allow a response right now"))
    if b.action == "SETTLE" and not can_settle:
        return Baseline("GATHER_MORE", b.why + "; but PayPal does not allow settling right now")
    return b


def steps_apart(a: Action, b: Action) -> int:
    return abs(LADDER.index(a) - LADDER.index(b))
