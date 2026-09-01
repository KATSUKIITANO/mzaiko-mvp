"""eBay listing のトップ画像（メイン写真）を差し替えるサービス。

eBay のギャラリー画像は inventory item の `product.imageUrls` の
**先頭**が使われる。つまり「n枚目をトップにする」＝ imageUrls の n 番目を
先頭に移動して PUT し直す、という操作になる。

設計:
  - 既存の inventory item を GET して現在の並びを取得し、その並びに対して
    位置指定する（＝運用者が eBay 上で見ている「n枚目」と一致させる）
  - PUT は createOrReplace なので、GET した内容から書き込み可能フィールドだけを
    引き継いで送る（read-only フィールドを送り返して弾かれるのを防ぐ）
  - published な listing は PUT だけだと反映が遅れることがあるため、
    既定で offer を再 publish して即時反映させる（republish=True）
  - dry_run=true なら並び替え結果を返すだけで eBay API を呼ばない

位置指定は 1 始まり（1枚目 = トップ）。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import Listing, Product
from app.services.ebay_auth_service import get_access_token
from app.services.ebay_inventory_service import (
    get_inventory_item,
    upsert_inventory_item,
)
from app.services.ebay_listing_service import ListingError, _ebay_request
from app.services.ebay_offer_service import publish_offer

logger = logging.getLogger(__name__)

# PUT /inventory_item/{sku} が受け付けるフィールド（read-only は送らない）
_WRITABLE_INVENTORY_FIELDS = (
    "availability",
    "condition",
    "conditionDescription",
    "conditionDescriptors",
    "packageWeightAndSize",
    "product",
    "locale",
)

_PUBLISHED_STATUSES = ("PUBLISHED", "published")


async def set_primary_image(
    session: AsyncSession,
    *,
    ref: str,
    position: int,
    dry_run: bool = False,
    republish: bool = True,
) -> dict[str, Any]:
    """listing のトップ画像を「現在の n 枚目」に差し替える。

    ref は SKU / SKU の数字部分 / product id のいずれでも解決する。
    """
    if position < 1:
        raise ListingError(f"position は 1 以上: position={position}")

    sku, product, listing = await _resolve_target(session, ref)

    token = await get_access_token()
    item = await get_inventory_item(token, sku)
    if item is None:
        raise ListingError(f"inventory item not found: sku={sku}")

    images = _extract_image_urls(item)
    if not images:
        raise ListingError(f"imageUrls が空: sku={sku}")
    if position > len(images):
        raise ListingError(
            f"画像が {len(images)} 枚しかない: sku={sku} position={position}"
        )

    reordered = _move_to_front(images, position)
    old_top, new_top = images[0], reordered[0]
    changed = old_top != new_top

    result: dict[str, Any] = {
        "success": True,
        "sku": sku,
        "ref": ref,
        "position": position,
        "changed": changed,
        "dry_run": dry_run,
        "image_count": len(images),
        "old_primary": old_top,
        "new_primary": new_top,
        "image_urls": reordered,
        "republished": False,
        "listing_id": None,
    }

    if not changed:
        logger.info(
            "primary image already correct sku=%s position=%d url=%s",
            sku, position, new_top,
        )
        result["reason"] = "already_primary"
        return result

    if dry_run:
        logger.info(
            "primary image dry_run sku=%s position=%d %s → %s",
            sku, position, old_top, new_top,
        )
        return result

    payload = _build_update_payload(item, reordered)
    await upsert_inventory_item(token, sku, payload)
    logger.info(
        "primary image updated sku=%s position=%d images=%d new_top=%s",
        sku, position, len(reordered), new_top,
    )

    if republish:
        listing_id = await _republish(session, token, sku, product, listing)
        result["republished"] = listing_id is not None
        result["listing_id"] = listing_id

    await _mark_revised(session, product, listing, position)
    return result


async def bulk_set_primary_image(
    session: AsyncSession,
    *,
    items: list[tuple[str, int]],
    dry_run: bool = False,
    republish: bool = True,
) -> dict[str, Any]:
    """複数 listing のトップ画像をまとめて差し替える。

    1件失敗しても残りは続行し、結果を全件返す。
    """
    results: list[dict[str, Any]] = []
    succeeded = failed = 0

    for ref, position in items:
        try:
            res = await set_primary_image(
                session,
                ref=ref,
                position=position,
                dry_run=dry_run,
                republish=republish,
            )
            succeeded += 1
        except Exception as e:
            failed += 1
            res = {
                "success": False,
                "ref": ref,
                "position": position,
                "changed": False,
                "dry_run": dry_run,
                "error": str(e)[:500],
            }
            logger.warning(
                "primary image failed ref=%s position=%d err=%s", ref, position, e
            )
        results.append(res)

    logger.info(
        "primary image bulk complete total=%d ok=%d failed=%d dry_run=%s",
        len(items), succeeded, failed, dry_run,
    )
    return {
        "total": len(items),
        "succeeded": succeeded,
        "failed": failed,
        "dry_run": dry_run,
        "results": results,
    }


# ─────────────────────────────────────
# 内部ロジック
# ─────────────────────────────────────
def _move_to_front(images: list[str], position: int) -> list[str]:
    """n 番目（1始まり）を先頭へ。他の画像の相対順序は保つ。"""
    idx = position - 1
    return [images[idx]] + images[:idx] + images[idx + 1:]


def _extract_image_urls(item: dict[str, Any]) -> list[str]:
    product = item.get("product") or {}
    urls = product.get("imageUrls") or []
    return [u for u in urls if isinstance(u, str) and u.strip()]


def _build_update_payload(
    item: dict[str, Any], image_urls: list[str]
) -> dict[str, Any]:
    """GET した inventory item から書き込み可能な部分だけを引き継いで PUT body を作る。"""
    payload: dict[str, Any] = {
        key: item[key] for key in _WRITABLE_INVENTORY_FIELDS if key in item
    }
    product = dict(payload.get("product") or {})
    product["imageUrls"] = image_urls
    payload["product"] = product
    return payload


async def _resolve_target(
    session: AsyncSession, ref: str
) -> tuple[str, Product | None, Listing | None]:
    """SKU / SKU の数字部分 / product id のどれでも listing を特定する。"""
    candidates = [ref]
    if ref.isdigit():
        candidates.append(f"{settings.ebay_sku_prefix}{ref}")

    for sku in candidates:
        product = await _find_product_by_sku(session, sku)
        listing = await _find_listing_by_sku(session, sku)
        if product is not None or listing is not None:
            return sku, product, listing

    if ref.isdigit():
        product = await session.get(Product, int(ref))
        if product is not None and product.ebay_sku:
            listing = await _find_listing_by_sku(session, product.ebay_sku)
            return product.ebay_sku, product, listing

    raise ListingError(f"listing not found: ref={ref}")


async def _find_product_by_sku(session: AsyncSession, sku: str) -> Product | None:
    result = await session.execute(
        select(Product).where(Product.ebay_sku == sku)
    )
    return result.scalars().first()


async def _find_listing_by_sku(session: AsyncSession, sku: str) -> Listing | None:
    result = await session.execute(select(Listing).where(Listing.sku == sku))
    return result.scalars().first()


async def _republish(
    session: AsyncSession,
    token: str,
    sku: str,
    product: Product | None,
    listing: Listing | None,
) -> str | None:
    """published な listing を再 publish して画像差し替えを即時反映させる。

    失敗しても inventory item の更新自体は済んでいるので graceful に握り潰す。
    """
    if not _is_published(product, listing):
        logger.info("republish skipped (not published) sku=%s", sku)
        return None

    offer_id = await _find_offer_id(token, sku, product, listing)
    if not offer_id:
        logger.warning("republish skipped (offer_id not found) sku=%s", sku)
        return None

    try:
        listing_id = await publish_offer(token, offer_id)
    except Exception as e:
        logger.warning(
            "republish failed (graceful) sku=%s offer_id=%s err=%s",
            sku, offer_id, e,
        )
        return None

    logger.info(
        "republished for image refresh sku=%s offer_id=%s listing_id=%s",
        sku, offer_id, listing_id,
    )
    return listing_id or None


def _is_published(product: Product | None, listing: Listing | None) -> bool:
    if product is not None and product.ebay_listing_status in _PUBLISHED_STATUSES:
        return True
    if listing is not None and listing.listing_status in _PUBLISHED_STATUSES:
        return True
    return False


async def _find_offer_id(
    token: str,
    sku: str,
    product: Product | None,
    listing: Listing | None,
) -> str | None:
    if product is not None and product.ebay_offer_id:
        return product.ebay_offer_id
    if listing is not None and listing.ebay_offer_id:
        return listing.ebay_offer_id

    try:
        res = await _ebay_request(
            "GET", f"/sell/inventory/v1/offer?sku={sku}", token=token
        )
    except Exception as e:
        logger.warning("offer lookup failed sku=%s err=%s", sku, e)
        return None

    for offer in res.get("offers") or []:
        offer_id = offer.get("offerId")
        if offer_id:
            return str(offer_id)
    return None


async def _mark_revised(
    session: AsyncSession,
    product: Product | None,
    listing: Listing | None,
    position: int,
) -> None:
    now = datetime.now(timezone.utc)
    if product is not None:
        product.ebay_last_sync_at = now
        product.ebay_sync_status = "image_revised"
        product.ebay_sync_error = None
    if listing is not None:
        listing.revised_at = now
    if product is not None or listing is not None:
        await session.commit()
        logger.debug("primary image revision recorded position=%d", position)
