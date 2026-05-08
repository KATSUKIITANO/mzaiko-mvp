"""固定テンプレ Copy Provider（依存ゼロ・常に成功）。

AI provider 失敗時の fallback、および dry_run のデフォルトとして使う。
"""
from __future__ import annotations

import html
import logging
import re
from typing import Any

from app.db.models import Product
from app.services.ai.base import CopyProvider

logger = logging.getLogger(__name__)


TITLE_MAX_LENGTH: int = 80
SUBTITLE_MAX_LENGTH: int = 55

CONDITION_NOTE: str = "Brand new, unused. Original packaging."
SHIPPING_NOTE: str = (
    "Ships from Japan via tracked international shipping. "
    "Estimated delivery: 3-7 business days."
)
RETURN_NOTE: str = (
    "Returns accepted within 30 days. "
    "Buyer pays return shipping. Full refund on receipt."
)

_BAD_TITLE_CHARS = re.compile(r"[!#$%^&*\[\]{}<>|\\\"`~【】「」『』〈〉《》]")
_MULTI_SPACE = re.compile(r"\s+")
_STOPWORDS: set[str] = {
    "the", "a", "an", "and", "or", "of", "for", "to", "in",
    "on", "at", "by", "with", "is", "are", "was", "were",
}


def _clean_title(title: str) -> str:
    cleaned = _BAD_TITLE_CHARS.sub(" ", title or "")
    cleaned = _MULTI_SPACE.sub(" ", cleaned).strip()
    return cleaned


def _build_optimized_title(product: Product) -> str:
    base = _clean_title(product.title or "")
    suffix = " | Brand New from Japan"
    available = TITLE_MAX_LENGTH - len(suffix)
    if len(base) > available:
        base = base[:available].rstrip()
    return (base + suffix)[:TITLE_MAX_LENGTH]


def _build_subtitle(product: Product) -> str:
    sub = "Tracked shipping from Japan, brand new, fast delivery"
    return sub[:SUBTITLE_MAX_LENGTH]


def _extract_keywords(product: Product, limit: int = 12) -> list[str]:
    base = _clean_title(product.title or "").lower()
    tokens = [t for t in re.split(r"[^a-z0-9]+", base) if t]
    seen: set[str] = set()
    out: list[str] = []
    for t in tokens:
        if t in _STOPWORDS or len(t) < 2:
            continue
        if t in seen:
            continue
        seen.add(t)
        out.append(t)
        if len(out) >= limit:
            break
    return out


def _build_bullets(product: Product) -> list[str]:
    bullets: list[str] = ["Brand new, unused, in original packaging"]
    if product.package_weight_gram:
        bullets.append(f"Package weight approx. {product.package_weight_gram} g")
    if product.package_length_cm and product.package_width_cm and product.package_height_cm:
        bullets.append(
            f"Package size: {product.package_length_cm} x "
            f"{product.package_width_cm} x {product.package_height_cm} cm"
        )
    bullets.extend([
        "Ships from Japan with tracking number",
        "Carefully packaged to prevent damage in transit",
        "30-day returns accepted",
    ])
    return bullets


def _build_html_description(
    product: Product,
    bullets: list[str],
) -> str:
    title = html.escape((product.title or "").strip())
    bullets_html = "".join(f"<li>{html.escape(b)}</li>" for b in bullets)
    parts = [
        f"<h2>{title}</h2>" if title else "",
        "<h3>Features</h3>",
        f"<ul>{bullets_html}</ul>",
        "<h3>Package contents</h3>",
        "<p>1 x product as shown in the listing photos. Original packaging.</p>",
        "<h3>Shipping</h3>",
        f"<p>{html.escape(SHIPPING_NOTE)}</p>",
        "<h3>Returns</h3>",
        f"<p>{html.escape(RETURN_NOTE)}</p>",
        "<h3>Contact</h3>",
        "<p>Please contact us via eBay messages for any questions before purchase.</p>",
    ]
    return "".join(p for p in parts if p)


class TemplateProvider(CopyProvider):
    name = "template"

    async def generate(self, product: Product) -> dict[str, Any]:
        bullets = _build_bullets(product)
        result = {
            "optimized_title": _build_optimized_title(product),
            "subtitle": _build_subtitle(product),
            "html_description": _build_html_description(product, bullets),
            "bullet_points": bullets,
            "seo_keywords": _extract_keywords(product),
            "condition_note": CONDITION_NOTE,
            "shipping_note": SHIPPING_NOTE,
            "return_note": RETURN_NOTE,
        }
        logger.info(
            "copy generated provider=template product_id=%s",
            getattr(product, "id", None),
        )
        return result
