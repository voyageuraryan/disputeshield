// Phase 13: pure offer logic for the Options card. Mirrors backend/app/rules/money.py (same formulas), so the
// card updates as the merchant edits the offer; the server re-checks every bound on approval.
import type { CaseFile, MoneyOption, SendAction } from "../api";

// Half away from zero, like the backend's Decimal ROUND_HALF_UP (Math.round(-146.845 * 100) would give -146.84).
const round2 = (n: number) => (Math.sign(n) * Math.round(Math.abs(n) * 100 + 1e-9)) / 100;

/** P(buyer accepts) = 0.1 + 0.8 * offer/amount, clamped to 0.05..0.95 (an assumption until real offers are recorded). */
export function acceptProbability(offer: number, amount: number): number {
  if (amount <= 0) return 0;
  return Math.min(0.95, Math.max(0.05, 0.1 + 0.8 * (offer / amount)));
}

/** Expected net of a partial offer: -offer * P(accept) + (1 - P(accept)) * fight. */
export function partialNet(offer: number, amount: number, fightNet: number): number {
  const p = acceptProbability(offer, amount);
  return round2(-offer * p + (1 - p) * fightNet);
}

/** Problems with an edited offer (empty = fine). Bounds come from the server (10%..90% and the max-offer setting). */
export function offerProblems(text: string, amount: number, bounds: { min: string; max: string } | null | undefined): string[] {
  if (!bounds) return ["PayPal doesn't allow a partial offer for this case now."];
  if (!/^\d+(\.\d{1,2})?$/.test(text.trim())) return ["Enter an amount like 25.00."];
  const v = Number(text);
  const out: string[] = [];
  if (v > amount) out.push("The offer can't be more than the disputed amount.");
  if (v < Number(bounds.min)) out.push(`The offer must be at least ${bounds.min} (10% of the amount).`);
  if (v > Number(bounds.max)) out.push(`The offer can be at most ${bounds.max} (your max-offer setting).`);
  return out;
}

/** Actions the merchant can send now (PayPal's links and offer types). */
export function sendable(c: Pick<CaseFile, "can_fight" | "can_accept_claim" | "can_offer" | "can_replace" | "can_settle">): SendAction[] {
  const out: SendAction[] = [];
  if (c.can_fight) out.push("FIGHT");
  if (c.can_accept_claim ?? c.can_settle) out.push("SETTLE");
  if (c.can_offer) out.push("SETTLE_PARTIAL");
  if (c.can_replace) out.push("REPLACEMENT");
  return out;
}

/** The note the server will write if the merchant leaves it empty (backend resolution.offer_note). */
export function offerNotePreview(action: SendAction, disputeId: string, offer: string, amount: string, currency: string): string {
  if (action === "REPLACEMENT")
    return `We're sorry this order didn't work out. To resolve dispute ${disputeId}, we offer to send you a replacement item at no cost.`;
  return `We're sorry this order didn't work out. To resolve dispute ${disputeId}, we offer a refund of ${Number(offer).toFixed(2)} ${currency} (the disputed amount is ${amount} ${currency}).`;
}

/** Options with the partial row recomputed for the merchant's edited offer; best = highest net among available. */
export function withOffer(options: MoneyOption[], offerText: string, amount: number): { rows: MoneyOption[]; best: string | null } {
  const fight = options.find((o) => o.action === "FIGHT");
  const offer = Number(offerText);
  const rows = options.map((o) =>
    o.action === "SETTLE_PARTIAL" && o.available && fight?.net != null && Number.isFinite(offer) && offer > 0
      ? {
          ...o,
          offer_amount: offer.toFixed(2),
          net: partialNet(offer, amount, Number(fight.net)).toFixed(2),
          chance: acceptProbability(offer, amount).toFixed(4),
          why: `offer ${offer.toFixed(2)} (${Math.round((offer / amount) * 100)}% of the amount); ~${Math.round(acceptProbability(offer, amount) * 100)}% chance the buyer accepts (estimate); if they decline, the case goes on and is analysed again`,
        }
      : o,
  );
  const usable = rows.filter((o) => o.available && o.net != null);
  const best = usable.length ? usable.reduce((a, b) => (Number(b.net) > Number(a.net) ? b : a)).action : null;
  return { rows, best };
}
