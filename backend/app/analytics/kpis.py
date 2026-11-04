"""Fact rows and KPIs (Phase 14). Pure functions over DisputeOutcome rows; every definition is here.

Per row (derived):
  won          outcome SELLER_FAVOUR or CANCELLED (the merchant keeps the money)
  contested    the case was left to PayPal to decide: final action FIGHT or NONE (no response), and the outcome is
               SELLER_FAVOUR / BUYER_FAVOUR / CANCELLED. Settled cases are not losses of a fight.
  money_protected  WON/CANCELLED: the disputed amount; OFFER_ACCEPTED: amount - offer (the partial saving)
  money_refunded   BUYER_FAVOUR: amount_refunded (PayPal's) or the amount; OFFER_ACCEPTED: the offer
  correct      agent accuracy, only where the outcome reveals the truth (contested cases with a FIGHT / settling
               recommendation): recommended FIGHT and won, or recommended a settling action and lost. None otherwise
               (settled cases have no counterfactual; GATHER_MORE isn't a prediction).
  overridden   the agent recommended something sendable (FIGHT or a settling action) and the merchant sent something
               else, or nothing (GATHER_MORE isn't an action, so acting on it isn't an override)
  hours_to_response  PayPal create_time -> our response sent;  before_deadline  response <= seller deadline
  month        the month the dispute resolved (YYYY-MM)

KPIs: money protected / refunded (per currency), win rate (won / contested) overall and by reason, accuracy
(correct / scored), median hours to response, % answered before the deadline, overrides and how they turned out.
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from statistics import median

from app.models import DisputeOutcome

SETTLING = ("SETTLE", "SETTLE_PARTIAL", "REPLACEMENT")
SENDABLE = ("FIGHT", *SETTLING)
DECIDED = ("SELLER_FAVOUR", "BUYER_FAVOUR", "CANCELLED")
REASON_LABEL = {"MERCHANDISE_OR_SERVICE_NOT_RECEIVED": "Not received",
                "MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED": "Not as described", "UNAUTHORISED": "Unauthorised"}


def fact(o: DisputeOutcome) -> dict:
    won = o.outcome in ("SELLER_FAVOUR", "CANCELLED")
    contested = o.final_action in ("FIGHT", "NONE") and o.outcome in DECIDED
    amount = Decimal(o.amount)
    offer = Decimal(o.offer_amount) if o.offer_amount is not None else None
    protected = amount if won else (amount - offer if o.outcome == "OFFER_ACCEPTED" and offer is not None
                                    else Decimal(0))
    refunded = ((Decimal(o.amount_refunded) if o.amount_refunded is not None else amount)
                if o.outcome == "BUYER_FAVOUR" else (offer or Decimal(0)) if o.outcome == "OFFER_ACCEPTED"
                else Decimal(0))
    correct = None
    if contested and o.recommended_action == "FIGHT":
        correct = won
    elif contested and o.recommended_action in SETTLING:
        correct = not won
    hours = ((o.response_at - o.created_at).total_seconds() / 3600
             if o.response_at and o.created_at else None)
    return {
        "id": o.id, "ref": o.ref, "is_demo": o.is_demo, "reason": o.reason,
        "reason_label": REASON_LABEL.get(o.reason, o.reason), "stage": o.stage,
        "amount": float(amount), "currency": o.currency, "outcome": o.outcome, "outcome_code": o.outcome_code,
        "final_action": o.final_action, "recommended_action": o.recommended_action, "followed": o.followed,
        "overridden": (o.recommended_action in SENDABLE and o.final_action != o.recommended_action),
        "won": won, "contested": contested, "correct": correct,
        "rule_score": o.rule_score, "offer_amount": float(offer) if offer is not None else None,
        "money_protected": float(protected), "money_refunded": float(refunded),
        "created_at": o.created_at.isoformat() if o.created_at else None,
        "response_at": o.response_at.isoformat() if o.response_at else None,
        "resolved_at": o.resolved_at.isoformat() if o.resolved_at else None,
        "due_at": o.due_at.isoformat() if o.due_at else None,
        "month": o.resolved_at.strftime("%Y-%m") if o.resolved_at else None,
        "hours_to_response": round(hours, 1) if hours is not None else None,
        "before_deadline": (o.response_at <= o.due_at) if o.response_at and o.due_at else None,
    }


def _rate(n: int, d: int) -> float | None:
    return round(n / d, 4) if d else None


def win_rate(rows: list[dict]) -> float | None:
    c = [r for r in rows if r["contested"]]
    return _rate(sum(r["won"] for r in c), len(c))


def accuracy(rows: list[dict]) -> float | None:
    s = [r for r in rows if r["correct"] is not None]
    return _rate(sum(r["correct"] for r in s), len(s))


def median_hours(rows: list[dict]) -> float | None:
    h = [r["hours_to_response"] for r in rows if r["hours_to_response"] is not None]
    return round(median(h), 1) if h else None


def before_deadline_pct(rows: list[dict]) -> float | None:
    a = [r for r in rows if r["before_deadline"] is not None]
    return _rate(sum(r["before_deadline"] for r in a), len(a))


def kpis(rows: list[dict]) -> dict:
    money: dict[str, dict[str, float]] = defaultdict(lambda: {"protected": 0.0, "refunded": 0.0})
    for r in rows:
        money[r["currency"]]["protected"] += r["money_protected"]
        money[r["currency"]]["refunded"] += r["money_refunded"]
    by_reason = []
    for reason in sorted({r["reason"] for r in rows}):
        rr = [r for r in rows if r["reason"] == reason]
        c = [r for r in rr if r["contested"]]
        by_reason.append({"reason": reason, "label": REASON_LABEL.get(reason, reason), "won": sum(r["won"] for r in c),
                          "contested": len(c), "win_rate": _rate(sum(r["won"] for r in c), len(c)),
                          "accuracy": accuracy(rr), "count": len(rr)})
    over = [r for r in rows if r["overridden"]]
    over_out: dict[str, int] = defaultdict(int)
    for r in over:
        over_out[r["outcome"]] += 1
    scored = [r for r in rows if r["correct"] is not None]
    answered = [r for r in rows if r["before_deadline"] is not None]
    return {
        "count": len(rows),
        "money": {k: {"protected": round(v["protected"], 2), "refunded": round(v["refunded"], 2)}
                  for k, v in sorted(money.items())},
        "win_rate": win_rate(rows), "contested": sum(r["contested"] for r in rows),
        "win_rate_by_reason": by_reason,
        "accuracy": {"rate": accuracy(rows), "correct": sum(r["correct"] for r in scored), "scored": len(scored)},
        "median_hours_to_response": median_hours(rows),
        "before_deadline": {"pct": before_deadline_pct(rows), "on_time": sum(r["before_deadline"] for r in answered),
                            "answered": len(answered)},
        "overrides": {"count": len(over), "outcomes": dict(over_out),
                      "won": sum(r["won"] for r in over), "accuracy_when_overridden": accuracy(over)},
    }


def by_month(rows: list[dict]) -> list[dict]:
    """Money protected / refunded, win rate and accuracy per resolution month (the charts)."""
    out = []
    for m in sorted({r["month"] for r in rows if r["month"]}):
        mm = [r for r in rows if r["month"] == m]
        out.append({"month": m, "money_protected": round(sum(r["money_protected"] for r in mm), 2),
                    "money_refunded": round(sum(r["money_refunded"] for r in mm), 2),
                    "win_rate": win_rate(mm), "accuracy": accuracy(mm), "count": len(mm),
                    "scored": sum(r["correct"] is not None for r in mm)})
    return out
