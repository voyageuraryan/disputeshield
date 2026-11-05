"""'What would win this?' (Phase 12): for each missing checklist item, the rule score and the rules' suggestion if
the merchant added it. Pure: re-runs evaluate() / economic_baseline() on a hypothetical copy of the case file with
one clean item of that type added (no red flags, e.g. delivered to the order's address). Nothing is stored."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal

from app.rules.evidence_rules import DIGITAL_INR, defect_confirmed, economic_baseline, evaluate, rule_for
from app.schemas import CaseFile, EvidenceItem

ADDABLE = {"tracking", "delivery_confirmation", "invoice", "listing", "refund_policy", "buyer_message", "order"}

HOWTO: dict[str, list[str]] = {
    "tracking": ["Upload the shipping label or carrier receipt that shows the tracking number, or type the number.",
                 "Use the same tracking number you gave the buyer; a number that isn't on the order counts for less."],
    "delivery_confirmation": [
        "Look up the tracking number on the carrier's website and save the delivered status page (date, time, "
        "city/ZIP) as a PDF, or request the carrier's proof-of-delivery letter.",
        "It should show the delivery date and the delivery address, which must match the order's shipping address."],
    "invoice": ["Upload the receipt or invoice you sent the buyer (PDF), showing the item, amount and date."],
    "listing": ["Upload a screenshot or PDF of the product page as the buyer saw it (title, description, photos)."],
    "refund_policy": ["Upload or paste your published return / refund policy (the version in force when the buyer "
                      "ordered). You can also set it once in Settings."],
    "buyer_message": ["Upload a screenshot of the buyer's message (e.g. one confirming they received the item), "
                      "with the date visible."],
    "order": ["Upload the order confirmation (order number, items, amount, shipping address)."],
}
DIGITAL_HOWTO = [
    "Digital item: there is no tracking. Upload the download or access log (date, time, the buyer's account or email, "
    "the file) or the delivery email you sent, as a PDF or screenshot.",
    "If the buyer downloaded it more than once, show every download; it proves they received it.",
]
CARRIER_HOWTO = {
    "USPS": "USPS: on usps.com, enter the tracking number, open the delivered event and save the page as PDF; for a "
            "signed letter, request USPS Proof of Delivery (Return Receipt Electronic / Signature Confirmation).",
    "UPS": "UPS: on ups.com tracking, open the delivered package and choose 'Proof of Delivery', then save it as PDF.",
    "FEDEX": "FedEx: on fedex.com tracking, open the delivered shipment and choose 'Obtain proof of delivery' "
             "(PDF).",
    "DHL": "DHL: on dhl.com tracking, open the shipment and request the electronic proof of delivery (ePOD).",
}


@dataclass(frozen=True)
class CoachItem:
    type: str
    required: bool
    score_now: float
    score_after: float
    gain: float
    action_now: str
    action_after: str
    howto: list[str]
    assumption: str


def _with(case: CaseFile, types: list[str]) -> CaseFile:
    extra = [EvidenceItem(id=-(i + 1), type=t, source="merchant_upload", source_ref=f"upload:hypothetical-{t}",
                          summary="(hypothetical)") for i, t in enumerate(types)]
    return case.model_copy(update={"evidence": [*case.evidence, *extra]})


def howto(evidence_type: str, carrier: str | None = None) -> list[str]:
    tips = list(HOWTO.get(evidence_type, []))
    c = (carrier or "").upper().replace(" ", "")
    if evidence_type in ("tracking", "delivery_confirmation") and c in CARRIER_HOWTO:
        tips.insert(0, CARRIER_HOWTO[c])
    return tips


def coach(case: CaseFile, threshold: Decimal, *, carrier: str | None = None) -> list[CoachItem]:
    """Missing items (required first by gain, then supporting), plus 'all required together' when 2+ are missing."""
    rule = rule_for(case)
    now = evaluate(case)
    if rule is None or not now.reason_supported:
        return []
    base = lambda cl: economic_baseline(cl, case.amount, threshold, can_fight=case.can_fight,  # noqa: E731
                                        can_settle=case.can_settle, defect=defect_confirmed(case)).action
    action_now = base(now)
    have = {e.type for e in case.evidence}
    missing = [t for t in (*rule.required, *rule.supporting) if t not in have and t in ADDABLE]
    out = []
    for t in missing:
        after = evaluate(_with(case, [t]))
        out.append(CoachItem(type=t, required=t in rule.required, score_now=now.rule_score,
                             score_after=after.rule_score, gain=round(after.rule_score - now.rule_score, 2),
                             action_now=action_now, action_after=base(after),
                             howto=DIGITAL_HOWTO if (rule is DIGITAL_INR and t == "delivery_confirmation")
                             else howto(t, carrier),
                             assumption=("assuming it shows this buyer received the download" if rule is DIGITAL_INR
                                         else "assuming it is clean (e.g. delivered to the order's shipping address)")))
    req = [t for t in missing if t in rule.required]
    if len(req) > 1:
        after = evaluate(_with(case, req))
        out.append(CoachItem(type="+".join(req), required=True, score_now=now.rule_score,
                             score_after=after.rule_score, gain=round(after.rule_score - now.rule_score, 2),
                             action_now=action_now, action_after=base(after), howto=[],
                             assumption="all missing required items together"))
    out.sort(key=lambda c: (not c.required, -c.gain, c.type))
    return out


def as_dicts(items: list[CoachItem]) -> list[dict]:
    return [asdict(i) for i in items]
