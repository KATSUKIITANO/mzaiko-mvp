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
    published かどうかは eBay の offer status を正とする（DB の状態より優先）
  - ローカル DB に行が無い listing（このアプリ以外で出した出品など）でも、
    SKU が eBay にあれば操作できる
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
from app.services.cj_mapping_service import get_mapping
from app.services.ebay_auth_service import get_access_token
from app.services.ebay_inventory_service import (
    get_inventory_item,
    upsert_inventory_item,
)
from app.services.ebay_listing_cj_service import _fetch_cj_image_urls
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

    token = await get_access_token()
    sku, product, listing, item = await _resolve_target(session, token, ref)

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


async def set_primary_image_url(
    session: AsyncSession,
    *,
    ref: str,
    image_url: str,
    replace_existing: bool = True,
    dry_run: bool = False,
    republish: bool = True,
) -> dict[str, Any]:
    """トップ画像を**新しい URL** に差し替える（パッケージ変更などで使う）。

    replace_existing=True  … 今の1枚目を捨てて新画像を1枚目にする（既定）
    replace_existing=False … 新画像を先頭に差し込み、今の1枚目は2枚目へ送る

    eBay Inventory API の imageUrls は**公開アクセス可能な URL** しか受け付けない。
    ローカル画像は直接渡せないため、CJ の商品画像 URL などを指定する。
    """
    url = (image_url or "").strip()
    if not url.startswith("https://"):
        raise ListingError(
            f"画像URLは https:// である必要がある: url={url[:120]}"
        )

    token = await get_access_token()
    sku, product, listing, item = await _resolve_target(session, token, ref)

    images = _extract_image_urls(item)
    old_top = images[0] if images else None

    if url in images:
        # 既にある画像なら並べ替えで済む（重複登録を避ける）
        reordered = _move_to_front(images, images.index(url) + 1)
    elif replace_existing and images:
        reordered = [url] + images[1:]
    else:
        reordered = [url] + images

    reordered = _dedupe(reordered)[: settings.ebay_max_image_count]
    changed = reordered[0] != old_top

    result: dict[str, Any] = {
        "success": True,
        "sku": sku,
        "ref": ref,
        "position": 1,
        "changed": changed,
        "dry_run": dry_run,
        "image_count": len(reordered),
        "old_primary": old_top,
        "new_primary": reordered[0],
        "image_urls": reordered,
        "replaced": replace_existing and old_top is not None and old_top not in reordered,
        "republished": False,
        "listing_id": None,
    }

    if not changed:
        result["reason"] = "already_primary"
        logger.info("primary image already set sku=%s url=%s", sku, url)
        return result

    if dry_run:
        logger.info(
            "primary image url dry_run sku=%s %s → %s", sku, old_top, url
        )
        return result

    await upsert_inventory_item(token, sku, _build_update_payload(item, reordered))
    logger.info(
        "primary image replaced sku=%s images=%d new_top=%s dropped=%s",
        sku, len(reordered), url, result["replaced"],
    )

    if republish:
        listing_id = await _republish(session, token, sku, product, listing)
        result["republished"] = listing_id is not None
        result["listing_id"] = listing_id

    await _mark_revised(session, product, listing, 1)
    return result


async def refresh_images_from_cj(
    session: AsyncSession,
    *,
    ref: str,
    cj_product_id: str | None = None,
    dry_run: bool = False,
    republish: bool = True,
) -> dict[str, Any]:
    """CJ の現在の商品画像で listing の画像を入れ替える。

    パッケージ変更で CJ 側の画像が差し替わったときに使う。
    cj_product_id 省略時は cj_product_mappings から SKU で引く。
    """
    token = await get_access_token()
    sku, product, listing, item = await _resolve_target(session, token, ref)

    pid = cj_product_id
    if not pid:
        mapping = await get_mapping(session, sku)
        if mapping is None:
            raise ListingError(
                f"CJ商品が紐付いていない: sku={sku}"
                "（cj_product_id を指定するか /cj/mappings で登録する）"
            )
        pid = mapping.cj_product_id

    raw_urls = await _fetch_cj_image_urls(pid)
    new_images = _dedupe(
        [u for u in raw_urls if isinstance(u, str) and u.strip().startswith("https://")]
    )[: settings.ebay_max_image_count]

    if not new_images:
        raise ListingError(f"CJから画像を取得できなかった: cj_pid={pid}")

    current = _extract_image_urls(item)
    changed = new_images != current

    result: dict[str, Any] = {
        "success": True,
        "sku": sku,
        "ref": ref,
        "cj_product_id": pid,
        "changed": changed,
        "dry_run": dry_run,
        "image_count": len(new_images),
        "old_primary": current[0] if current else None,
        "new_primary": new_images[0],
        "old_image_urls": current,
        "image_urls": new_images,
        "republished": False,
        "listing_id": None,
    }

    if not changed:
        result["reason"] = "no_change"
        logger.info("cj images unchanged sku=%s cj_pid=%s", sku, pid)
        return result

    if dry_run:
        logger.info(
            "cj image refresh dry_run sku=%s cj_pid=%s %d→%d images",
            sku, pid, len(current), len(new_images),
        )
        return result

    await upsert_inventory_item(token, sku, _build_update_payload(item, new_images))
    logger.info(
        "cj images refreshed sku=%s cj_pid=%s %d→%d images",
        sku, pid, len(current), len(new_images),
    )

    if republish:
        listing_id = await _republish(session, token, sku, product, listing)
        result["republished"] = listing_id is not None
        result["listing_id"] = listing_id

    await _mark_revised(session, product, listing, 1)
    return result


def _dedupe(urls: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for u in urls:
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


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


def _sku_candidates(ref: str) -> list[str]:
    """ref から SKU 候補を作る。数字なら prefix 付きを優先。"""
    if ref.isdigit():
        prefixed = f"{settings.ebay_sku_prefix}{ref}"
        return [prefixed, ref] if prefixed != ref else [ref]
    return [ref]


async def _resolve_target(
    session: AsyncSession, token: str, ref: str
) -> tuple[str, Product | None, Listing | None, dict[str, Any]]:
    """SKU / SKU の数字部分 / product id から対象の inventory item を特定する。

    1. ローカル DB（Product.ebay_sku → Listing.sku → Product.id）で SKU を引く
    2. DB に無ければ SKU 候補をそのまま eBay に当てる
       （このアプリ以外で出した出品でも、SKU が合えば操作できる）

    DB 行は再 publish と更新記録に使うだけなので、無くても差し替えはできる。
    """
    candidates = _sku_candidates(ref)

    for sku in candidates:
        product = await _find_product_by_sku(session, sku)
        listing = await _find_listing_by_sku(session, sku)
        if product is not None or listing is not None:
            item = await get_inventory_item(token, sku)
            if item is None:
                raise ListingError(
                    f"inventory item not found on eBay: sku={sku} (ref={ref})"
                )
            return sku, product, listing, item

    if ref.isdigit():
        product = await session.get(Product, int(ref))
        if product is not None and product.ebay_sku:
            sku = product.ebay_sku
            item = await get_inventory_item(token, sku)
            if item is None:
                raise ListingError(
                    f"inventory item not found on eBay: sku={sku} (ref={ref})"
                )
            listing = await _find_listing_by_sku(session, sku)
            return sku, product, listing, item

    # DB に無い listing → SKU 候補を eBay に直接当てる
    for sku in candidates:
        item = await get_inventory_item(token, sku)
        if item is not None:
            logger.info(
                "resolved via eBay only (no local DB row) ref=%s sku=%s", ref, sku
            )
            return sku, None, None, item

    raise ListingError(
        f"listing not found: ref={ref} (tried sku={'/'.join(candidates)})"
    )


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

    published 判定は eBay の offer status を正とする（DB が古い / 行が無い場合でも
    正しく動かすため）。未 publish の offer を勝手に公開しないよう、
    eBay が UNPUBLISHED と言っている場合はスキップする。
    失敗しても inventory item の更新自体は済んでいるので graceful に握り潰す。
    """
    api_offer_id, api_status = await _fetch_offer_from_api(token, sku)

    if api_status:
        published = api_status in _PUBLISHED_STATUSES
    else:
        published = _is_published(product, listing)

    if not published:
        logger.info(
            "republish skipped (not published) sku=%s offer_status=%s",
            sku, api_status or "unknown",
        )
        return None

    offer_id = api_offer_id or _db_offer_id(product, listing)
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


def _db_offer_id(product: Product | None, listing: Listing | None) -> str | None:
    if product is not None and product.ebay_offer_id:
        return product.ebay_offer_id
    if listing is not None and listing.ebay_offer_id:
        return listing.ebay_offer_id
    return None


async def _fetch_offer_from_api(
    token: str, sku: str
) -> tuple[str | None, str | None]:
    """SKU から offer を引く → (offer_id, status)。失敗時は (None, None)。"""
    try:
        res = await _ebay_request(
            "GET", f"/sell/inventory/v1/offer?sku={sku}", token=token
        )
    except Exception as e:
        logger.warning("offer lookup failed sku=%s err=%s", sku, e)
        return None, None

    for offer in res.get("offers") or []:
        offer_id = offer.get("offerId")
        if offer_id:
            return str(offer_id), offer.get("status")
    return None, None


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
