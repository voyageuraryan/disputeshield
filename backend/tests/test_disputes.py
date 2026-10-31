from decimal import Decimal

import pytest
import respx

from app.paypal.disputes import get_dispute, list_disputes
from tests.conftest import BASE, token_response

LIST_BODY = {
    "items": [
        {
            "dispute_id": "PP-D-27803",
            "create_time": "2026-10-20T10:00:00.000Z",
            "update_time": "2026-10-21T10:00:00.000Z",
            "reason": "MERCHANDISE_OR_SERVICE_NOT_RECEIVED",
            "status": "WAITING_FOR_SELLER_RESPONSE",
            "dispute_state": "REQUIRED_ACTION",
            "dispute_amount": {"currency_code": "USD", "value": "49.99"},
            "dispute_life_cycle_stage": "INQUIRY",
            "dispute_channel": "INTERNAL",
            "seller_response_due_date": "2026-11-05T10:00:00.000Z",
            "links": [{"href": f"{BASE}/v1/customer/disputes/PP-D-27803", "rel": "self", "method": "GET"}],
        }
    ],
    "links": [{"href": f"{BASE}/v1/customer/disputes?page=2", "rel": "next", "method": "GET"}],
}

DETAIL_BODY = {
    "dispute_id": "PP-D-27803",
    "create_time": "2026-10-20T10:00:00.000Z",
    "disputed_transactions": [
        {
            "buyer_transaction_id": "4FK54435BF0539845",
            "seller_transaction_id": "3BC38643YC807283D",
            "transaction_status": "COMPLETED",
            "gross_amount": {"currency_code": "USD", "value": "49.99"},
            "buyer": {"name": "Jane"},
        }
    ],
    "reason": "MERCHANDISE_OR_SERVICE_NOT_RECEIVED",
    "status": "WAITING_FOR_SELLER_RESPONSE",
    "dispute_amount": {"currency_code": "USD", "value": "49.99"},
    "dispute_life_cycle_stage": "INQUIRY",
    "dispute_channel": "INTERNAL",
    "seller_response_due_date": "2026-11-05T10:00:00.000Z",
    "messages": [{"posted_by": "BUYER", "content": "Never arrived"}],
    "dispute_outcome": None,
    "some_new_field": {"x": 1},
}


@respx.mock
async def test_list_disputes_parses_and_sends_params(pp):
    respx.post(f"{BASE}/v1/oauth2/token").respond(200, json=token_response())
    route = respx.get(f"{BASE}/v1/customer/disputes").respond(200, json=LIST_BODY)

    res = await list_disputes(page_size=20, dispute_state=["REQUIRED_ACTION", "UNDER_PAYPAL_REVIEW"], client=pp)

    params = dict(route.calls[0].request.url.params)
    assert params == {"page_size": "20", "dispute_state": "REQUIRED_ACTION,UNDER_PAYPAL_REVIEW"}
    d = res.items[0]
    assert d.dispute_id == "PP-D-27803"
    assert d.dispute_amount.value == Decimal("49.99")
    assert d.seller_response_due_date.year == 2026
    assert res.links[0].rel == "next"


@respx.mock
async def test_list_disputes_empty_body(pp):
    respx.post(f"{BASE}/v1/oauth2/token").respond(200, json=token_response())
    respx.get(f"{BASE}/v1/customer/disputes").respond(200, json={})
    assert (await list_disputes(client=pp)).items == []


async def test_list_disputes_validates_page_size(pp):
    with pytest.raises(ValueError):
        await list_disputes(page_size=51, client=pp)


@respx.mock
async def test_get_dispute(pp):
    respx.post(f"{BASE}/v1/oauth2/token").respond(200, json=token_response())
    respx.get(f"{BASE}/v1/customer/disputes/PP-D-27803").respond(200, json=DETAIL_BODY)

    d = await get_dispute("PP-D-27803", client=pp)
    assert d.disputed_transactions[0].seller_transaction_id == "3BC38643YC807283D"
    assert d.disputed_transactions[0].gross_amount.value == Decimal("49.99")
    assert d.messages[0]["content"] == "Never arrived"
    assert d.model_extra["some_new_field"] == {"x": 1}
    assert d.raw == DETAIL_BODY  # original JSON preserved verbatim


async def test_get_dispute_rejects_bad_id(pp):
    with pytest.raises(ValueError):
        await get_dispute("../oauth2/token", client=pp)
