"""eBay 本番出品オーケストレーター（preview → inventory → offer → publish）。

3段階構造の publish フェーズを担う。
- 既存 build_preview() で payload を組み立て
- 既存 ebay_auth_service の token を再利用、401 時は invalidate → 1 回 retry
- inventory_service / offer_service は薄いラッパ（HTTP layer は ebay_listing_service._ebay_request）
- 各ステップ後に Product を逐次更新（途中失敗時に追跡可能）
- publish 失敗時は offer_id を保存（rollback しない＝再試行できる）
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import Product
from app.services.ebay_auth_service import (
    EbayAuthError,
    get_access_token,
    invalidate_token_cache,
)
from app.services.ebay_inventory_service import (
    safe_delete_inventory_item,
    upsert_inventory_item,
)
from app.services.cj_mapping_service import record_listing_mapping
from app.services.ebay_listing_cj_service import _fetch_cj_image_urls
from app.services.ebay_listing_service import ListingError
from app.services.ebay_offer_service import (
    create_offer,
    publish_offer,
    safe_withdraw_offer,
)
from app.services.ebay_payload_builder import build_preview

logger = logging.getLogger(__name__)


_MARKETPLACE_DOMAIN: dict[str, str] = {
    "EBAY_US": "ebay.com",
    "EBAY_GB": "ebay.co.uk",
    "EBAY_DE": "ebay.de",
    "EBAY_FR": "ebay.fr",
    "EBAY_IT": "ebay.it",
    "EBAY_AU": "ebay.com.au",
    "EBAY_CA": "ebay.ca",
    "EBAY_JP": "ebay.co.jp",
}


def _build_listing_url(listing_id: str, marketplace_id: str) -> str:
    domain = _MARKETPLACE_DOMAIN.get(marketplace_id, "ebay.com")
    return f"https://www.{domain}/itm/{listing_id}"


async def _call_with_token_refresh(
    fn: Callable[[str], Awaitable[Any]],
    *,
    label: str,
) -> Any:
    """401 を検知して token cache を invalidate → 1 回だけ retry。"""
    token = await get_access_token()
    try:
        return await fn(token)
    except ListingError as e:
        if "401" not in str(e):
            raise
        logger.warning(
            "ebay 401 detected step=%s → invalidate token & retry once", label
        )
        invalidate_token_cache()
        token = await get_access_token()
        return await fn(token)


async def _update_product(
    session: AsyncSession, product: Product, **fields: Any
) -> None:
    for k, v in fields.items():
        setattr(product, k, v)
    await session.commit()


async def publish_listing(
    *,
    product_id: int,
    cj_product_id: str,
    marketplace_id: str | None,
    session: AsyncSession,
    dry_run: bool = False,
) -> dict[str, Any]:
    product = await session.get(Product, product_id)
    if product is None:
        raise ListingError(f"product not found: id={product_id}")

    raw_urls = await _fetch_cj_image_urls(cj_product_id)
    preview = build_preview(product=product, image_urls=raw_urls)

    sku = preview["sku"]
    # 売れたときに CJ へ発注できるよう SKU ↔ CJ 商品を記録しておく
    await record_listing_mapping(
        session, sku=sku, cj_product_id=cj_product_id, product_id=product_id
    )
    inventory_payload = preview["inventory_payload"]
    offer_payload = preview["offer_payload"]
    publish_payload = preview["publish_payload"]
    effective_marketplace = (
        marketplace_id or settings.ebay_listing_marketplace_id
    )
    if marketplace_id:
        offer_payload["marketplaceId"] = marketplace_id

    if dry_run:
        logger.info(
            "publish dry_run sku=%s images=%d marketplace=%s",
            sku, len(preview["images"]), effective_marketplace,
        )
        return {
            "success": True,
            "sku": sku,
            "offer_id": None,
            "listing_id": None,
            "listing_url": None,
            "published": False,
            "dry_run": True,
            "inventory_payload": inventory_payload,
            "offer_payload": offer_payload,
            "publish_payload": publish_payload,
        }

    # Step 2: PUT inventory_item ──────────
    try:
        await _call_with_token_refresh(
            lambda t: upsert_inventory_item(t, sku, inventory_payload),
            label="inventory",
        )
    except (ListingError, EbayAuthError) as e:
        logger.error("inventory create failed sku=%s err=%s", sku, e)
        await _update_product(
            session, product, ebay_sku=sku,
            ebay_listing_status="INVENTORY_FAILED",
        )
        raise

    await _update_product(
        session, product, ebay_sku=sku, ebay_listing_status="INVENTORY_CREATED",
    )

    # Step 3: POST offer ──────────────────
    try:
        offer_id = await _call_with_token_refresh(
            lambda t: create_offer(t, offer_payload),
            label="offer",
        )
    except (ListingError, EbayAuthError) as e:
        logger.error(
            "offer create failed sku=%s err=%s (inventory item kept)", sku, e
        )
        await _update_product(
            session, product, ebay_listing_status="OFFER_FAILED"
        )
        raise

    await _update_product(
        session, product, ebay_offer_id=offer_id,
        ebay_listing_status="OFFER_CREATED",
    )

    # Step 4: POST publish ────────────────
    try:
        listing_id = await _call_with_token_refresh(
            lambda t: publish_offer(t, offer_id),
            label="publish",
        )
    except (ListingError, EbayAuthError) as e:
        logger.error(
            "publish failed sku=%s offer_id=%s err=%s "
            "(offer_id kept for retry)",
            sku, offer_id, e,
        )
        await _update_product(
            session, product, ebay_listing_status="PUBLISH_FAILED"
        )
        raise

    listing_url = _build_listing_url(listing_id, effective_marketplace)
    await _update_product(
        session, product,
        ebay_listing_id=listing_id,
        ebay_listing_status="PUBLISHED",
        ebay_published_at=datetime.now(timezone.utc),
    )

    return {
        "success": True,
        "sku": sku,
        "offer_id": offer_id,
        "listing_id": listing_id,
        "listing_url": listing_url,
        "published": True,
        "dry_run": False,
    }


async def rollback_listing(
    *, sku: str, offer_id: str | None = None
) -> None:
    """手動 rollback ヘルパ（オプション）。
    publish を諦めて inventory / offer をクリーンアップしたい時に使う。
    """
    token = await get_access_token()
    if offer_id:
        await safe_withdraw_offer(token, offer_id)
    await safe_delete_inventory_item(token, sku)
