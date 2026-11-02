"""Demo store catalog. Prices live server-side only; the browser never sends amounts."""

from decimal import Decimal

from pydantic import BaseModel


class Product(BaseModel):
    sku: str
    name: str
    price: Decimal
    currency: str = "USD"
    category: str  # store category: electronics / accessory / digital / home (drives troubleshooting eligibility)
    paypal_category: str  # Orders v2 item category: PHYSICAL_GOODS / DIGITAL_GOODS
    model: str | None = None  # manufacturer model, shown to the buyer in troubleshooting
    listing: str  # product listing text shown to buyers (evidence for NOT_AS_DESCRIBED)
    image: str  # emoji used as a placeholder image


PRODUCTS: dict[str, Product] = {
    p.sku: p
    for p in [
        Product(
            sku="LAMP-01",
            name="Smart LED desk lamp",
            price=Decimal("89.00"),
            category="electronics",
            paypal_category="PHYSICAL_GOODS",
            model="Lumina LD-200",
            listing=(
                "Lumina LD-200 smart LED desk lamp. 10 W dimmable LED panel (800 lm), 5 colour temperatures "
                "(2700-6500 K), touch control on the base, 45-minute auto-off timer, USB-C phone charging port "
                "(5 V / 1.5 A). Aluminium arm, 42 cm tall. In the box: lamp, 12 V / 2 A power adapter (US plug), "
                "quick-start guide. 1-year limited warranty. Ships in 2-3 business days with tracking."
            ),
            image="💡",
        ),
        Product(
            sku="EARBUDS-01",
            name="Wireless earbuds",
            price=Decimal("59.00"),
            category="electronics",
            paypal_category="PHYSICAL_GOODS",
            model="SoundPeak E5",
            listing=(
                "SoundPeak E5 true wireless earbuds. Bluetooth 5.3, active noise cancellation, 6 h playback "
                "(24 h with the charging case), IPX4 sweat resistant, USB-C fast charge (10 min = 1 h). "
                "In the box: earbuds, charging case, USB-C cable, 3 sizes of ear tips (S/M/L), quick-start guide. "
                "1-year limited warranty. Ships in 2-3 business days with tracking."
            ),
            image="🎧",
        ),
        Product(
            sku="CASE-01",
            name="Clear phone case",
            price=Decimal("12.00"),
            category="accessory",
            paypal_category="PHYSICAL_GOODS",
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
            category="digital",
            paypal_category="DIGITAL_GOODS",
            listing="PDF ebook, 120 pages. Download link emailed immediately after purchase. No refunds once downloaded.",
            image="📘",
        ),
        Product(
            sku="MUG-01",
            name="Ceramic mug",
            price=Decimal("45.00"),
            category="home",
            paypal_category="PHYSICAL_GOODS",
            listing=(
                "Wheel-thrown stoneware mug, 350 ml, speckled glaze. Dishwasher safe. "
                "Ships in 3-5 business days with tracking; signature not required."
            ),
            image="☕",
        ),
    ]
}


CATEGORIES = ("electronics", "accessory", "digital", "home")


def order_item(product: Product, quantity: int) -> dict:
    """The Order.items entry stored at order time (category + model travel with the order)."""
    return {"sku": product.sku, "name": product.name, "quantity": quantity, "unit_amount": str(product.price),
            "category": product.category, "model": product.model, "paypal_category": product.paypal_category}


def item_category(item: dict | None) -> str | None:
    """Store category of an order line. Orders saved before categories existed carried PayPal's
    PHYSICAL_GOODS/DIGITAL_GOODS in "category", so fall back to the catalog by SKU."""
    item = item or {}
    cat = item.get("category")
    if cat in CATEGORIES:
        return cat
    product = PRODUCTS.get(item.get("sku") or "")
    return product.category if product else None


def item_model(item: dict | None) -> str | None:
    item = item or {}
    product = PRODUCTS.get(item.get("sku") or "")
    return item.get("model") or (product.model if product else None)


def digital_only(items: list[dict] | None) -> bool:
    """True when every line item is a digital product (nothing to ship or track)."""
    cats = [getattr(PRODUCTS.get((i or {}).get("sku") or ""), "paypal_category", None) for i in items or []]
    return bool(cats) and all(c == "DIGITAL_GOODS" for c in cats)
