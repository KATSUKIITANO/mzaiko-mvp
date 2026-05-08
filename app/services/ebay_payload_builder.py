"""eBay Inventory / Offer / Publish の payload ビルダー。

preview / validate / publish の3段階構造のうち、
preview の payload 組み立てを担う。
外部 API を呼ばないので auth token / sandbox 不要。

将来本番化（publish）時は build_preview() の inventory / offer payload を
そのまま PUT/POST に流用できる構造にしてある。
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from app.config import settings
from app.db.models import Product
from app.services.ebay_description_service import build_html_description
from app.services.ebay_validator import (
    IMAGE_HARD_MAX,
    TITLE_MAX_LENGTH,
    validate_payloads,
)

logger = logging.getLogger(__name__)


CONDITION_FIXED: str = "NEW"
QUANTITY_DEFAULT: int = 1
SHIPPING_POLICY_PLACEHOLDER: str = "TEMP"


# ── helpers ─────────────────────────
def build_sku(product_id: int) -> str:
    """SKU生成。形式: MZ-{product_id}-{unix_seconds}。"""
    return f"MZ-{product_id}-{int(time.time())}"


def truncate_title(title: str) -> str:
    return (title or "").strip()[:TITLE_MAX_LENGTH]


def filter_image_urls(
    urls: list[str], max_count: int = IMAGE_HARD_MAX
) -> list[str]:
    """空・重複・非http(s) を除外し、上限枚数までトリム。"""
    seen: set[str] = set()
    out: list[str] = []
    for raw in urls or []:
        if not isinstance(raw, str):
            continue
        u = raw.strip()
        if not u:
            continue
        if not (u.startswith("http://") or u.startswith("https://")):
            logger.warning("invalid image url skipped url=%s", u)
            continue
        if u in seen:
            continue
        seen.add(u)
        out.append(u)
        if len(out) >= max_count:
            break
    return out


def calculate_price(
    selling_price_jpy: int, markup_percent: float, currency: str
) -> str:
    final_jpy = (selling_price_jpy or 0) * (1 + markup_percent / 100.0)
    if currency.upper() == "JPY":
        return str(int(final_jpy))
    final_usd = final_jpy / settings.fx_rate_usd_jpy
    return f"{final_usd:.2f}"


# ── payload pieces ──────────────────
def build_shipping_template() -> dict[str, Any]:
    """shipping policy 仮固定。将来 EBAY_FULFILLMENT_POLICY_ID へ差し替え。"""
    return {"shippingPolicyId": SHIPPING_POLICY_PLACEHOLDER}


def build_inventory_payload(
    *, title: str, description: str, image_urls: list[str], quantity: int
) -> dict[str, Any]:
    return {
        "availability": {
            "shipToLocationAvailability": {"quantity": quantity},
        },
        "condition": CONDITION_FIXED,
        "product": {
            "title": title,
            "description": description,
            "imageUrls": image_urls,
        },
    }


def build_offer_payload(
    *,
    sku: str,
    price: str,
    currency: str,
    marketplace_id: str,
    category_id: str,
    quantity: int,
    shipping: dict[str, Any] | None = None,
) -> dict[str, Any]:
    listing_policies = {
        "fulfillmentPolicyId": settings.ebay_fulfillment_policy_id,
        "paymentPolicyId": settings.ebay_payment_policy_id,
        "returnPolicyId": settings.ebay_return_policy_id,
    }
    if shipping:
        listing_policies.update(shipping)

    return {
        "sku": sku,
        "marketplaceId": marketplace_id,
        "format": "FIXED_PRICE",
        "availableQuantity": quantity,
        "categoryId": category_id,
        "merchantLocationKey": settings.ebay_merchant_location_key,
        "pricingSummary": {
            "price": {"currency": currency, "value": price},
        },
        "listingPolicies": listing_policies,
    }


def build_publish_payload(*, sku: str, offer_id: str | None = None) -> dict[str, Any]:
    """publish API は body 不要だが、将来拡張のため meta を含む dict を返す。

    実 API 呼び出しでは offer_id を URL パスに使い body は空。
    preview 表示用の構造として保持する。
    """
    return {
        "sku": sku,
        "offerId": offer_id,
        "endpoint": "POST /sell/inventory/v1/offer/{offerId}/publish",
        "body": {},
    }


# ── high-level: preview ─────────────
def build_preview(
    *,
    product: Product,
    image_urls: list[str],
) -> dict[str, Any]:
    """eBay 出品 preview を組み立てる。

    返却:
      {
        "sku", "inventory_payload", "offer_payload", "publish_payload",
        "images", "validation"
      }
    """
    sku = build_sku(product.id)
    title = truncate_title(product.title or "")
    description = build_html_description(product)
    images = filter_image_urls(image_urls, settings.ebay_max_image_count)

    markup = settings.ebay_listing_markup_percent
    currency = settings.ebay_listing_currency
    marketplace_id = settings.ebay_listing_marketplace_id
    category_id = settings.ebay_listing_category_id
    price = calculate_price(product.selling_price or 0, markup, currency)

    inventory_payload = build_inventory_payload(
        title=title,
        description=description,
        image_urls=images,
        quantity=QUANTITY_DEFAULT,
    )
    offer_payload = build_offer_payload(
        sku=sku,
        price=price,
        currency=currency,
        marketplace_id=marketplace_id,
        category_id=category_id,
        quantity=QUANTITY_DEFAULT,
        shipping=build_shipping_template(),
    )
    publish_payload = build_publish_payload(sku=sku)

    validation = validate_payloads(
        title=title,
        description=description,
        image_urls=images,
        price_str=price,
        currency=currency,
    )

    payload_size = (
        len(json.dumps(inventory_payload))
        + len(json.dumps(offer_payload))
        + len(json.dumps(publish_payload))
    )
    logger.info(
        "preview generated sku=%s images=%d payload_size=%d",
        sku, len(images), payload_size,
    )

    return {
        "sku": sku,
        "inventory_payload": inventory_payload,
        "offer_payload": offer_payload,
        "publish_payload": publish_payload,
        "images": images,
        "validation": validation,
    }
