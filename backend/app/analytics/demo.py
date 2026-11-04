"""Demo history (Phase 14): realistic, clearly labelled historical outcomes for the Analytics demo.

Written ONLY as DisputeOutcome rows with is_demo=True and ref DEMO-####: no Dispute rows, so they never appear in the
queue, never mix with real sandbox cases, and are never used to calibrate real decisions (calibration reads
is_demo=False). The Analytics page shows a "Demo data" badge whenever it displays them.
Deterministic (seeded RNG) so screenshots and tests are stable.  python -m app.scripts.seed --demo-history
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlmodel import Session, delete

from app.models import DisputeOutcome

REASONS = (("MERCHANDISE_OR_SERVICE_NOT_RECEIVED", 0.45), ("MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED", 0.40),
           ("UNAUTHORISED", 0.15))
# "true" win curves used to simulate PayPal's decisions (so the calibration chart has something real-looking to find)
TRUE_WIN = {"MERCHANDISE_OR_SERVICE_NOT_RECEIVED": lambda s: min(0.95, 0.08 + 0.95 * s),
            "MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED": lambda s: min(0.9, 0.05 + 0.75 * s),
            "UNAUTHORISED": lambda s: min(0.9, 0.1 + 0.6 * s)}


def _pick(rng: random.Random, choices):  # noqa: ANN001, ANN202
    x, acc = rng.random(), 0.0
    for v, w in choices:
        acc += w
        if x < acc:
            return v
    return choices[-1][0]


def generate(n: int = 140, *, now: datetime | None = None, seed: int = 14) -> list[DisputeOutcome]:
    rng = random.Random(seed)
    now = now or datetime.now(timezone.utc)
    rows = []
    for i in range(1, n + 1):
        reason = _pick(rng, REASONS)
        amount = Decimal(str(round(min(450.0, max(8.0, rng.lognormvariate(4.0, 0.7))), 2)))
        score = round(min(1.0, max(0.0, rng.betavariate(2.2, 1.8))), 2)
        created = now - timedelta(days=rng.uniform(20, 270), hours=rng.uniform(0, 23))
        stage = "INQUIRY" if rng.random() < 0.45 else "CHARGEBACK"
        due = created + timedelta(days=20 if stage == "INQUIRY" else 10)
        rec = "FIGHT" if score >= 0.7 else ("SETTLE" if score < 0.4 or amount < 25 else "GATHER_MORE")
        if rec == "SETTLE" and stage == "INQUIRY" and amount >= 25 and rng.random() < 0.6:
            rec = "SETTLE_PARTIAL"
        final = rec if rng.random() < 0.85 else rng.choice(["FIGHT", "SETTLE"])
        if final == "GATHER_MORE":
            final = "FIGHT" if rng.random() < 0.6 else "NONE"
        late = rng.random() < 0.07
        response = None if final == "NONE" else created + timedelta(hours=rng.uniform(1, 30) if not late else
                                                                    (due - created).total_seconds() / 3600 + 12)
        offer = None
        p_win = TRUE_WIN[reason](score) * (0.4 if late else 1.0)
        if final == "SETTLE":
            outcome, refunded = "BUYER_FAVOUR", amount
        elif final == "SETTLE_PARTIAL":
            offer = (amount * Decimal(str(rng.choice([0.3, 0.4, 0.5])))).quantize(Decimal("0.01"))
            if rng.random() < 0.1 + 0.8 * float(offer / amount):
                outcome, refunded = "OFFER_ACCEPTED", offer
            else:  # declined -> fought
                final = "FIGHT"
                won = rng.random() < p_win
                outcome, refunded = ("SELLER_FAVOUR", None) if won else ("BUYER_FAVOUR", amount)
        elif rng.random() < 0.06:
            outcome, refunded = "CANCELLED", None
        else:
            won = rng.random() < p_win
            outcome, refunded = ("SELLER_FAVOUR", None) if won else ("BUYER_FAVOUR", amount)
        resolved = (response or created) + timedelta(days=rng.uniform(3, 35))
        resolved = min(resolved, now - timedelta(hours=1))
        code = {"SELLER_FAVOUR": "RESOLVED_SELLER_FAVOUR", "BUYER_FAVOUR": "RESOLVED_BUYER_FAVOUR",
                "CANCELLED": "CANCELED_BY_BUYER", "OFFER_ACCEPTED": "RESOLVED_BUYER_FAVOUR"}[outcome]
        rows.append(DisputeOutcome(
            is_demo=True, ref=f"DEMO-{i:04d}", reason=reason, stage=stage, amount=amount, currency="USD",
            outcome_code=code, outcome=outcome, amount_refunded=refunded, offer_amount=offer, final_action=final,
            recommended_action=rec, followed=None if rec == "GATHER_MORE" else final == rec,
            rule_score=score, created_at=created, due_at=due, response_at=response, resolved_at=resolved))
    return rows


def replace_demo_history(session: Session, n: int = 140, *, now: datetime | None = None) -> int:
    session.exec(delete(DisputeOutcome).where(DisputeOutcome.is_demo == True))  # noqa: E712
    rows = generate(n, now=now)
    session.add_all(rows)
    session.commit()
    return len(rows)


def clear_demo_history(session: Session) -> int:
    res = session.exec(delete(DisputeOutcome).where(DisputeOutcome.is_demo == True))  # noqa: E712
    session.commit()
    return res.rowcount or 0


def auto_seed(session: Session) -> dict:
    """AUTO_SEED (Render free plan): seed the demo store and the demo outcome history if they're missing.
    Idempotent: never touches existing orders or real data."""
    from sqlmodel import select

    from app.models import Order
    from app.scripts.seed import seed

    out = {"store": False, "demo_history": 0}
    if session.exec(select(Order).limit(1)).first() is None:
        seed(session)
        out["store"] = True
    if session.exec(select(DisputeOutcome).where(DisputeOutcome.is_demo == True).limit(1)).first() is None:  # noqa: E712
        out["demo_history"] = replace_demo_history(session)
    return out

