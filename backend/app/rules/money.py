"""Money calculator (Phase 13): the expected net outcome of each way to resolve a dispute. Pure, no I/O.

Sign convention: amounts are the merchant's expected net change relative to KEEPING THE SALE (0 = the merchant keeps
the money and spends no time). Positive is never possible; the least negative option is the best.

  fight   = -(1 - P(win)) * (amount + dispute_fee) - handling_cost
  settle  = -amount                                          (full refund; no dispute fee at the inquiry stage)
  partial = -offer * P(accept) + (1 - P(accept)) * fight     (a denied offer leaves the case to be fought)
  handling_cost = handling_minutes / 60 * hourly_value       (merchant settings)

Note on the Phase 13 brief: it wrote fight as "P(win) * amount - (1 - P(win)) * (amount + fee) - handling". With
settle = -amount on the same scale, that counts the amount twice (a win would be worth +amount on top of keeping
the sale), so fighting would beat refunding even at P(win) = 0.15. The keep-the-sale baseline above drops only the
"+P(win) * amount" term; everything else is as written.

P(win) from the deterministic rule_score (WIN_TABLE). This is an initial, documented assumption, NOT calibrated
yet; Phase 14 recalibrates it from real outcomes. P(accept) of a partial offer grows with the share of the disputed
amount offered (accept_probability). Also an assumption until real offers are recorded.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")
# (rule_score lower bound, P(win)), highest bound first. Initial assumption (uncalibrated): Phase 14.
WIN_TABLE: tuple[tuple[Decimal, Decimal], ...] = (
    (Decimal("0.85"), Decimal("0.80")),
    (Decimal("0.70"), Decimal("0.65")),
    (Decimal("0.55"), Decimal("0.45")),
    (Decimal("0.40"), Decimal("0.30")),
    (Decimal("0"), Decimal("0.15")),
)
OFFER_MIN_PCT = Decimal("10")   # validator bounds for a partial offer (% of the disputed amount)
OFFER_MAX_PCT = Decimal("90")
OFFER_STEP_PCT = Decimal("5")   # the suggested offer is searched on this grid


@dataclass(frozen=True)
class MoneySettings:
    dispute_fee: Decimal = Decimal("15")        # PayPal US standard dispute fee (see Settings help for sources)
    handling_minutes: Decimal = Decimal("30")   # merchant's time to fight one dispute
    hourly_value: Decimal = Decimal("25")       # what an hour of the merchant's time is worth
    max_offer_pct: Decimal = Decimal("70")      # never suggest offering more than this % of the disputed amount

    @property
    def handling_cost(self) -> Decimal:
        return (self.handling_minutes / Decimal(60) * self.hourly_value).quantize(CENT, ROUND_HALF_UP)


@dataclass(frozen=True)
class Option:
    action: str                 # FIGHT | SETTLE | SETTLE_PARTIAL | REPLACEMENT
    label: str
    available: bool
    net: Decimal | None         # expected net outcome (None when it can't be computed, e.g. replacement cost unknown)
    chance: Decimal | None      # P(win) for FIGHT, P(accept) for SETTLE_PARTIAL, 1 for SETTLE
    offer_amount: Decimal | None
    why: str

    def as_dict(self) -> dict:
        d = asdict(self)
        return {k: (str(v) if isinstance(v, Decimal) else v) for k, v in d.items()}


def win_probability(rule_score: float | Decimal) -> Decimal:
    s = Decimal(str(rule_score))
    for bound, p in WIN_TABLE:
        if s >= bound:
            return p
    return WIN_TABLE[-1][1]


def accept_probability(offer: Decimal, amount: Decimal) -> Decimal:
    """0.1 + 0.8 * (offer / amount), clamped to 0.05..0.95 (assumption: bigger offers are accepted more often)."""
    if amount <= 0:
        return Decimal("0")
    p = Decimal("0.1") + Decimal("0.8") * (offer / amount)
    return min(Decimal("0.95"), max(Decimal("0.05"), p)).quantize(Decimal("0.0001"), ROUND_HALF_UP)


def fight_net(p_win: Decimal, amount: Decimal, s: MoneySettings) -> Decimal:
    return (-(1 - p_win) * (amount + s.dispute_fee) - s.handling_cost).quantize(CENT, ROUND_HALF_UP)


def settle_net(amount: Decimal) -> Decimal:
    return (-amount).quantize(CENT, ROUND_HALF_UP)


def partial_net(offer: Decimal, amount: Decimal, fight: Decimal) -> Decimal:
    p = accept_probability(offer, amount)
    return (-offer * p + (1 - p) * fight).quantize(CENT, ROUND_HALF_UP)


def offer_bounds(amount: Decimal, s: MoneySettings) -> tuple[Decimal, Decimal]:
    """Allowed partial offer range: 10%..90% of the disputed amount and at most the merchant's max-offer %."""
    lo = (amount * OFFER_MIN_PCT / 100).quantize(CENT, ROUND_HALF_UP)
    hi = (amount * min(OFFER_MAX_PCT, s.max_offer_pct) / 100).quantize(CENT, ROUND_HALF_UP)
    return lo, hi


def offer_problems(offer: Decimal | None, amount: Decimal, s: MoneySettings) -> list[str]:
    if offer is None:
        return ["SETTLE_PARTIAL needs an offer_amount"]
    lo, hi = offer_bounds(amount, s)
    out = []
    if offer > amount:
        out.append(f"offer {offer} is above the disputed amount {amount}")
    if offer < lo:
        out.append(f"offer {offer} is below 10% of the disputed amount ({lo})")
    if offer > hi:
        out.append(f"offer {offer} is above the allowed maximum {hi} (90% of the amount and your max-offer "
                   f"setting {s.max_offer_pct}%)")
    if offer != offer.quantize(CENT):
        out.append(f"offer {offer} has more than 2 decimals")
    return out


def best_offer(amount: Decimal, fight: Decimal, s: MoneySettings) -> Decimal | None:
    """The offer on the 5% grid within bounds with the best expected net (None if the range is empty)."""
    lo, hi = offer_bounds(amount, s)
    if hi < lo:
        return None
    best, best_net = None, None
    pct = OFFER_MIN_PCT
    while pct <= OFFER_MAX_PCT:
        offer = (amount * pct / 100).quantize(CENT, ROUND_HALF_UP)
        if lo <= offer <= hi:
            net = partial_net(offer, amount, fight)
            if best_net is None or net > best_net:
                best, best_net = offer, net
        pct += OFFER_STEP_PCT
    return best


def options(rule_score: float, amount: Decimal, currency: str, *, can_fight: bool, can_accept_claim: bool,
            offer_types: list[str], s: MoneySettings, offer: Decimal | None = None) -> list[Option]:
    """Every way to resolve, with its expected net. `offer`: evaluate this partial offer instead of the best one."""
    p_win = win_probability(rule_score)
    fight = fight_net(p_win, amount, s)
    out = [Option("FIGHT", "Fight", can_fight, fight, p_win, None,
                  f"{int(p_win * 100)}% estimated chance to win (rule score {rule_score:.2f}); if you lose you also "
                  f"pay the {s.dispute_fee} {currency} dispute fee; your time ~{s.handling_cost} {currency}"
                  + ("" if can_fight else ". PayPal doesn't allow contesting right now")),
           Option("SETTLE", "Full refund", can_accept_claim, settle_net(amount), Decimal(1), None,
                  f"refund {amount} {currency} now; the case closes, no dispute fee"
                  + ("" if can_accept_claim else ". PayPal doesn't allow accepting the claim right now"))]
    if "REFUND" in offer_types:
        chosen = offer if offer is not None else best_offer(amount, fight, s)
        if chosen is not None:
            p = accept_probability(chosen, amount)
            out.append(Option("SETTLE_PARTIAL", "Partial refund offer", True, partial_net(chosen, amount, fight), p,
                              chosen, f"offer {chosen} {currency} ({(chosen / amount * 100).quantize(Decimal(1))}% of "
                                      f"the amount); ~{int(p * 100)}% chance the buyer accepts (estimate); if they "
                                      "decline, the case goes on and is analysed again"))
        else:
            out.append(Option("SETTLE_PARTIAL", "Partial refund offer", False, None, None, None,
                              "your max-offer setting leaves no room for an offer of at least 10%"))
    else:
        out.append(Option("SETTLE_PARTIAL", "Partial refund offer", False, None, None, None,
                          "PayPal allows partial offers only in the inquiry stage, when it lists REFUND offers"))
    if "REPLACEMENT_WITHOUT_REFUND" in offer_types:
        out.append(Option("REPLACEMENT", "Send a replacement", True, None, None, None,
                          "the buyer gets a new item instead of money; the cost is your item + shipping "
                          "(not known to DisputeShield)"))
    return out


def best(opts: list[Option]) -> Option | None:
    usable = [o for o in opts if o.available and o.net is not None]
    return max(usable, key=lambda o: o.net) if usable else None


def full_baseline(checklist, case, threshold: Decimal, s: MoneySettings):  # noqa: ANN001, ANN201
    """The rules' baseline (evidence_rules.economic_baseline) refined by the calculator: when the rules say SETTLE,
    the settling option with the best expected net wins: a partial offer if PayPal lists one and it beats a full
    refund (or a full refund isn't allowed). Returns (Baseline, options, suggested offer or None)."""
    from app.rules.evidence_rules import Baseline, economic_baseline

    base = economic_baseline(checklist, case.amount, threshold, can_fight=case.can_fight,
                             can_settle=case.can_accept_claim or case.can_offer)
    opts = options(checklist.rule_score, case.amount, case.currency, can_fight=case.can_fight,
                   can_accept_claim=case.can_accept_claim, offer_types=case.offer_types, s=s)
    by = {o.action: o for o in opts}
    partial, full = by.get("SETTLE_PARTIAL"), by["SETTLE"]
    if base.action == "SETTLE" and partial is not None and partial.available and partial.net is not None and (
            not full.available or partial.net > full.net):
        why = (base.why + f"; a partial refund offer of {partial.offer_amount} {case.currency} has a better expected "
               f"outcome ({partial.net}) than " + (f"a full refund ({full.net})" if full.available
                                                     else "anything else PayPal allows now"))
        return Baseline("SETTLE_PARTIAL", why), opts, partial.offer_amount
    return base, opts, None
