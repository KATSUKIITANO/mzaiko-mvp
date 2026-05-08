"""eBay Inventory Item の HTML description 生成。

Product から title / features / shipping / condition を含む
HTML 文字列を組み立てる。送信先は eBay の inventory_item.product.description。
"""
from __future__ import annotations

import html
import logging

from app.db.models import Product

logger = logging.getLogger(__name__)


CONDITION_LABEL: str = "Brand new, unused"
SHIPPING_FROM: str = "Japan"
SHIPPING_LEAD_TIME: str = "3-7 business days (international shipping)"
SUPPORT_NOTE: str = (
    "If you have any questions, please contact us via eBay messages."
)


def _safe(text: str | None) -> str:
    return html.escape((text or "").strip())


def build_html_description(product: Product) -> str:
    """Product から eBay 出品用の HTML description を生成する。

    含む要素:
      - title (h2)
      - features (ul)
      - shipping
      - condition
      - support note
    """
    title = _safe(product.title)
    weight = product.package_weight_gram
    dims = None
    if product.package_length_cm and product.package_width_cm and product.package_height_cm:
        dims = (
            f"{product.package_length_cm} x "
            f"{product.package_width_cm} x "
            f"{product.package_height_cm} cm"
        )

    features: list[str] = []
    if weight:
        features.append(f"Package weight: approx. {weight} g")
    if dims:
        features.append(f"Package size: {dims}")
    features.append("Carefully packed to prevent damage in transit")
    features.append("Tracking number provided after shipment")

    features_html = "".join(f"<li>{html.escape(f)}</li>" for f in features)

    parts = [
        f"<h2>{title}</h2>" if title else "",
        f"<ul>{features_html}</ul>",
        f"<p><b>Condition:</b> {html.escape(CONDITION_LABEL)}</p>",
        (
            f"<p><b>Shipping:</b> Ships from {html.escape(SHIPPING_FROM)}. "
            f"{html.escape(SHIPPING_LEAD_TIME)}</p>"
        ),
        f"<p>{html.escape(SUPPORT_NOTE)}</p>",
    ]
    return "".join(p for p in parts if p)
