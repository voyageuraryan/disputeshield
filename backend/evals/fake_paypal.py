"""A PayPal client that answers the investigator's reads from a golden (no network). Used for the shadow
investigation whose tool calls the agentic metrics score (ToolCorrectness, StepEfficiency, TaskCompletion).

Serves only GETs the investigator makes: /v1/customer/disputes/{id}, /v2/payments/captures/{id},
/v2/checkout/orders/{id}. Anything else raises, so an unexpected PayPal call shows up as a failure.
"""

from __future__ import annotations

import re
from typing import Any

from evals.goldens.schema import Golden

TRACKER_REF = re.compile(r"^paypal:order/([A-Za-z0-9]+)/tracker/([A-Za-z0-9]+)$")
CAPTURE_REF = re.compile(r"^paypal:capture/([A-Za-z0-9]+)$")


def capture_id(g: Golden) -> str | None:
    return next((m.group(1) for e in g.case_file.evidence if (m := CAPTURE_REF.match(e.source_ref))), None)


def paypal_order_id(g: Golden) -> str | None:
    return next((m.group(1) for e in g.case_file.evidence if (m := TRACKER_REF.match(e.source_ref))), None)


class FakePayPal:
    def __init__(self, g: Golden):
        self.g = g
        self.requests: list[str] = []

    async def get(self, path: str, params: dict | None = None, **kwargs: Any) -> Any:
        self.requests.append(path)
        g, case = self.g, self.g.case_file
        if path == f"/v1/customer/disputes/{case.dispute_id}":
            return {"dispute_id": case.dispute_id, **g.paypal_raw}
        cap = capture_id(g)
        if cap and path == f"/v2/payments/captures/{cap}":
            inv = next(e for e in case.evidence if e.source_ref == f"paypal:capture/{cap}")
            status = next((f.split(":", 1)[1] for f in inv.flags if f.startswith("seller_protection:")), "UNKNOWN")
            cats = [f.split(":", 1)[1] for f in inv.flags if f.startswith("protected:")]
            return {"id": cap, "status": "COMPLETED", "amount": {"value": str(case.amount), "currency_code": case.currency},
                    "create_time": "2026-10-10T10:00:00Z", "invoice_id": "INV-1",
                    "seller_protection": {"status": status, "dispute_categories": cats}}
        oid = paypal_order_id(g)
        if oid and path == f"/v2/checkout/orders/{oid}":
            trackers = []
            for e in case.evidence:
                if TRACKER_REF.match(e.source_ref):
                    trackers.append({"id": f"{cap or 'CAP'}-9400111899223344556677",
                                     "status": "CANCELLED" if "tracker_cancelled" in e.flags else "SHIPPED"})
            return {"id": oid, "purchase_units": [{"shipping": {"trackers": trackers}}]}
        raise ValueError(f"the eval PayPal stub doesn't serve GET {path}")
