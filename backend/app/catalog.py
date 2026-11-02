"""Demo store catalog. Prices live server-side only; the browser never sends amounts."""

from decimal import Decimal

from pydantic import BaseModel


class Product(BaseModel):
    sku: str
    name: str
    price: Decimal
    currency: str = "USD"
    category: str  # Orders v2 item category: PHYSICAL_GOODS / DIGITAL_GOODS
    listing: str  # product listing text shown to buyers (evidence for NOT_AS_DESCRIBED)
    image: str  # emoji used as a placeholder image


PRODUCTS: dict[str, Product] = {
    p.sku: p
    for p in [
        Product(
            sku="LAMP-01",
            name="Handmade walnut lamp",
            price=Decimal("89.00"),
            category="PHYSICAL_GOODS",
            listing=(
                "Hand-turned walnut table lamp, 38 cm tall, linen shade, E27 bulb (not included). "
                "Each piece is unique; grain and colour vary. Ships in 2-3 business days with tracking."
            ),
            image="🪔",
        ),
        Product(
            sku="CASE-01",
            name="Clear phone case",
            price=Decimal("12.00"),
            category="PHYSICAL_GOODS",
            listing=(
                "Transparent TPU phone case for iPhone 15. Slim, shock-absorbing corners. "
                "Ships by standard mail within 5 business days (untracked)."
            ),
            image="📱",
        ),
        Product(
            sku="EBOOK-01",
            name="Ebook: Pottery for Beginners",
            price=Decimal("9.00"),
            category="DIGITAL_GOODS",
            listing="PDF ebook, 120 pages. Download link emailed immediately after purchase. No refunds once downloaded.",
            image="📘",
        ),
        Product(
            sku="MUG-01",
            name="Ceramic mug",
            price=Decimal("45.00"),
            category="PHYSICAL_GOODS",
            listing=(
                "Wheel-thrown stoneware mug, 350 ml, speckled glaze. Dishwasher safe. "
                "Ships in 3-5 business days with tracking; signature not required."
            ),
            image="☕",
        ),
    ]
}


def digital_only(items: list[dict] | None) -> bool:
    """True when every line item is a digital product (nothing to ship or track)."""
    cats = [getattr(PRODUCTS.get((i or {}).get("sku") or ""), "category", None) for i in items or []]
    return bool(cats) and all(c == "DIGITAL_GOODS" for c in cats)
