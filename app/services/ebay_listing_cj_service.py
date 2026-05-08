"""CJ Dropshipping 画像 → eBay 出品 統合サービス。

責務:
  - CJ から画像URL一覧を取得（失敗時は graceful fallback で []）
  - URL検証（空・重複・非http/https を除外）
  - 画像枚数制御（settings.ebay_max_image_count）
  - eBay Inventory Item / Offer payload を構築
  - dry_run=true → payload のみ返却
  - dry_run=false → eBay Inventory API + Offer API + Publish 実行
  - publish 失敗時は best-effort で offer / inventory_item を削除

既存 ebay_listing_service.py のヘルパ（_truncate_title / _build_description /
_build_inventory_payload / _build_offer_payload / _ebay_request 系）と
ebay_auth_service.get_access_token を再利用する。
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import Product
from app.scraper.cj import CJError, cj_product_detail, extract_image_urls
from app.services.ebay_auth_service import get_access_token
from app.services.ebay_listing_service import (
    QUANTITY_DEFAULT,
    ListingError,
    _build_description,
    _build_inventory_payload,
    _build_offer_payload,
    _calculate_price,
    _create_offer,
    _generate_sku,
    _publish_offer,
    _put_inventory_item,
    _safe_delete_inventory_item,
    _safe_delete_offer,
    _truncate_title,
)
from app.services.ebay_payload_builder import build_preview

logger = logging.getLogger(__name__)


def _filter_image_urls(urls: list[str], max_count: int) -> list[str]:
    """空・重複・非http(s) を除外し、上限枚数までトリム。"""
    seen: set[str] = set()
    out: list[str] = []
    for raw in urls:
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


async def _fetch_cj_image_urls(cj_product_id: str) -> list[str]:
    """CJ画像取得。失敗時は空配列を返す（graceful fallback）。"""
    try:
        detail = await cj_product_detail(cj_product_id)
    except CJError as e:
        logger.warning(
            "image fetch failed cj_pid=%s err=%s", cj_product_id, e
        )
        return []
    except Exception as e:
        logger.warning(
            "image fetch unexpected error cj_pid=%s err=%s", cj_product_id, e
        )
        return []

    urls = extract_image_urls(detail)
    logger.info(
        "image extracted count=%d cj_pid=%s", len(urls), cj_product_id
    )
    return urls


async def generate_preview(
    *,
    product_id: int,
    cj_product_id: str,
    session: AsyncSession,
) -> dict[str, Any]:
    """eBay 未接続でも最終 payload を完全確認できる preview を返す。

    eBay API は一切呼ばない。auth token / sandbox 不要。
    """
    product = await session.get(Product, product_id)
    if product is None:
        raise ListingError(f"product not found: id={product_id}")

    raw_urls = await _fetch_cj_image_urls(cj_product_id)
    return build_preview(product=product, image_urls=raw_urls)


async def create_listing_with_cj_images(
    *,
    product_id: int,
    cj_product_id: str,
    session: AsyncSession,
    dry_run: bool = True,
) -> dict[str, Any]:
    """CJ画像を取得して eBay 出品（または dry_run で payload のみ返却）。"""
    product = await session.get(Product, product_id)
    if product is None:
        raise ListingError(f"product not found: id={product_id}")

    sku = _generate_sku(product_id)
    title = _truncate_title(product.title or "")
    description = _build_description(product)

    raw_urls = await _fetch_cj_image_urls(cj_product_id)
    image_urls = _filter_image_urls(raw_urls, settings.ebay_max_image_count)
    logger.info(
        "image attached count=%d sku=%s cj_pid=%s",
        len(image_urls), sku, cj_product_id,
    )

    inventory_payload = _build_inventory_payload(
        title, description, image_urls, QUANTITY_DEFAULT
    )

    markup = settings.ebay_listing_markup_percent
    currency = settings.ebay_listing_currency
    marketplace_id = settings.ebay_listing_marketplace_id
    category_id = settings.ebay_listing_category_id
    price = _calculate_price(product.selling_price or 0, markup, currency)
    offer_payload = _build_offer_payload(
        sku=sku,
        price=price,
        currency=currency,
        marketplace_id=marketplace_id,
        category_id=category_id,
        quantity=QUANTITY_DEFAULT,
    )

    if dry_run:
        logger.info(
            "dry_run sku=%s images=%d cj_pid=%s",
            sku, len(image_urls), cj_product_id,
        )
        return {
            "success": True,
            "image_count": len(image_urls),
            "inventory_item_id": sku,
            "offer_id": None,
            "published": False,
            "dry_run": True,
            "inventory_payload": inventory_payload,
            "offer_payload": offer_payload,
        }

    token = await get_access_token()
    await _put_inventory_item(token, sku, inventory_payload)
    logger.info("ebay inventory create success sku=%s", sku)

    offer_resp = await _create_offer(token, offer_payload)
    offer_id = offer_resp.get("offerId", "")
    logger.info("ebay offer create success sku=%s offer_id=%s", sku, offer_id)

    published = False
    try:
        await _publish_offer(token, offer_id)
        published = True
    except Exception as e:
        logger.warning(
            "publish failed; rolling back offer_id=%s sku=%s err=%s",
            offer_id, sku, e,
        )
        await _safe_delete_offer(token, offer_id)
        await _safe_delete_inventory_item(token, sku)
        raise ListingError(f"publish failed: {e}")

    return {
        "success": True,
        "image_count": len(image_urls),
        "inventory_item_id": sku,
        "offer_id": offer_id,
        "published": published,
        "dry_run": False,
    }
