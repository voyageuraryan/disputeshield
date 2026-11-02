"""Which disputes get a buyer troubleshooting session. Pure function: no DB, no PayPal."""

from __future__ import annotations

from dataclasses import dataclass

from app.catalog import item_category, item_model

NOT_AS_DESCRIBED = "MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED"
TROUBLESHOOTABLE = ("electronics",)


@dataclass(frozen=True)
class Eligibility:
    eligible: bool
    why: str  # plain sentence for the timeline
    product_name: str | None = None
    product_model: str | None = None


def check_eligibility(reason: str | None, dispute_status: str | None, has_order: bool,
                      items: list[dict] | None) -> Eligibility:
    """Eligible when the buyer says the item is not as described, the dispute is linked to a store order
    with at least one electronics item, and the dispute is still open."""
    if dispute_status == "RESOLVED":
        return Eligibility(False, "Not eligible for buyer troubleshooting: the dispute is resolved")
    if reason != NOT_AS_DESCRIBED:
        return Eligibility(False, "Not eligible for buyer troubleshooting: the buyer didn't report a faulty or "
                                  "different item (reason is not 'not as described')")
    if not has_order:
        return Eligibility(False, "Not eligible for buyer troubleshooting: no store order is linked to this dispute")
    for item in items or []:
        if item_category(item) in TROUBLESHOOTABLE:
            name = (item or {}).get("name") or "the item"
            return Eligibility(True, f"Eligible for buyer troubleshooting: {name} is an electrical/electronic item",
                               product_name=name, product_model=item_model(item))
    return Eligibility(False, "Not eligible for buyer troubleshooting: the order has no electrical/electronic item")


def dispute_eligibility(dispute, order) -> Eligibility:
    return check_eligibility(dispute.reason, dispute.status, order is not None, order.items if order else None)
