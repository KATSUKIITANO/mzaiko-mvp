"""eBay Inventory API + Offer API による出品 + revise + end サービス。

出品（list_product）:
  1. PUT  /sell/inventory/v1/inventory_item/{sku}    createOrReplaceInventoryItem
  2. POST /sell/inventory/v1/offer                    createOffer
  3. POST /sell/inventory/v1/offer/{offerId}/publish  publishOffer
  publish 失敗時は best-effort で DELETE offer + DELETE inventory_item

revise（revise_listing）:
  1. PUT  /sell/inventory/v1/inventory_item/{sku}    （title/desc/img/qty 更新）
  2. PUT  /sell/inventory/v1/offer/{offerId}          （price 更新）
  partial failure 時は warning のみ（rollback なし）

end listing:
  POST /sell/inventory/v1/offer/{offerId}/withdraw

将来 bulk listing / 定期同期は同じ `_ebay_request()` で拡張可能。
"""
from __future__ import annotations

import html
import logging
from datetime import datetime, timezone

import httpx
from sqlalchemy.ext.asyncio import AsyncSession
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.config import settings
from app.db.models import (
    Listing,
    ListingStatus,
    MonitoringStatus,
    Product,
    SyncStatus,
)
from app.schemas.listing import (
    ListingDetail,
    ListItemRequest,
    ReviseItemRequest,
)
from app.scraper.amazon import scrape_amazon
from app.services.ebay_auth_service import get_access_token

logger = logging.getLogger(__name__)


TITLE_MAX_LENGTH: int = 80
QUANTITY_DEFAULT: int = 1
CONDITION_FIXED: str = "NEW"

OOS_ACTION_SET_ZERO: str = "set_zero"
OOS_ACTION_END_LISTING: str = "end_listing"


# ── exceptions ───────────────────────
class ListingError(Exception):
    pass


class RiskBlockedError(ListingError):
    pass


# ─────────────────────────────────────
# list_product（出品）
# ─────────────────────────────────────
async def list_product(
    product_id: int,
    request: ListItemRequest,
    session: AsyncSession,
) -> ListingDetail:
    product = await session.get(Product, product_id)
    if product is None:
        raise ListingError(f"product not found: id={product_id}")

    _enforce_risk_policy(product)

    sku = request.sku or _generate_sku(product_id)
    title = _truncate_title(product.title)
    description = _build_description(product)
    image_urls = [product.image_url] if product.image_url else []
    marketplace_id = request.marketplace_id or settings.ebay_listing_marketplace_id
    currency = request.currency or settings.ebay_listing_currency
    category_id = request.category_id or settings.ebay_listing_category_id
    markup = (
        request.markup_percent
        if request.markup_percent is not None
        else settings.ebay_listing_markup_percent
    )
    price = _calculate_price(product.selling_price or 0, markup, currency)

    inventory_payload = _build_inventory_payload(
        title, description, image_urls, QUANTITY_DEFAULT
    )
    offer_payload = _build_offer_payload(
        sku=sku, price=price, currency=currency,
        marketplace_id=marketplace_id, category_id=category_id,
        quantity=QUANTITY_DEFAULT,
    )

    listing = Listing(
        product_id=product_id, sku=sku, title=title, description=description,
        listing_price=price, currency=currency, quantity=QUANTITY_DEFAULT,
        image_url=product.image_url, marketplace_id=marketplace_id,
        category_id=category_id,
        listing_status=ListingStatus.PENDING.value,
        sync_status=SyncStatus.PENDING.value,
        dry_run=request.dry_run,
        request_payload={"inventory_item": inventory_payload, "offer": offer_payload},
    )
    session.add(listing)
    await session.commit()
    await session.refresh(listing)

    logger.info(
        "listing start id=%d product_id=%d sku=%s dry_run=%s",
        listing.id, product_id, sku, request.dry_run,
    )

    if request.dry_run:
        listing.listing_status = ListingStatus.DRY_RUN.value
        listing.sync_status = SyncStatus.DRY_RUN.value
        listing.last_synced_at = datetime.now(timezone.utc)
        await session.commit()
        await session.refresh(listing)
        logger.info("listing dry_run completed id=%d", listing.id)
        return ListingDetail.model_validate(listing)

    _validate_listing_settings()
    token = await get_access_token()

    try:
        await _put_inventory_item(token, sku, inventory_payload)
    except Exception as e:
        await _mark_failed(session, listing, f"inventory create failed: {e}")
        raise

    listing.listing_status = ListingStatus.INVENTORY_CREATED.value
    await session.commit()
    logger.info("listing inventory_created id=%d sku=%s", listing.id, sku)

    try:
        offer_response = await _create_offer(token, offer_payload)
        offer_id = offer_response.get("offerId")
        if not offer_id:
            raise ListingError(f"createOffer 応答に offerId なし: {offer_response}")
    except Exception as e:
        await _safe_delete_inventory_item(token, sku)
        await _mark_failed(session, listing, f"offer create failed: {e}")
        raise

    listing.ebay_offer_id = offer_id
    listing.listing_status = ListingStatus.OFFER_CREATED.value
    await session.commit()
    logger.info(
        "listing offer_created id=%d sku=%s offer_id=%s",
        listing.id, sku, offer_id,
    )

    try:
        publish_response = await _publish_offer(token, offer_id)
        ebay_listing_id = publish_response.get("listingId")
        if not ebay_listing_id:
            raise ListingError(f"publishOffer 応答に listingId なし: {publish_response}")
        listing.response_payload = publish_response
    except Exception as e:
        logger.warning(
            "listing publish failed id=%d offer_id=%s err=%s — rolling back",
            listing.id, offer_id, e,
        )
        await _safe_delete_offer(token, offer_id)
        await _safe_delete_inventory_item(token, sku)
        listing.listing_status = ListingStatus.ROLLED_BACK.value
        listing.error_message = f"publish failed: {e}"
        await session.commit()
        raise

    listing.ebay_listing_id = ebay_listing_id
    listing.listing_status = ListingStatus.PUBLISHED.value
    listing.sync_status = SyncStatus.OK.value
    listing.last_synced_at = datetime.now(timezone.utc)
    await session.commit()
    await session.refresh(listing)
    logger.info(
        "listing published id=%d sku=%s offer_id=%s listing_id=%s",
        listing.id, sku, offer_id, ebay_listing_id,
    )
    return ListingDetail.model_validate(listing)


# ─────────────────────────────────────
# revise_listing（出品済みリスティング更新）
# ─────────────────────────────────────
async def revise_listing(
    listing_id: int,
    request: ReviseItemRequest,
    session: AsyncSession,
) -> ListingDetail:
    listing = await session.get(Listing, listing_id)
    if listing is None:
        raise ListingError(f"listing not found: id={listing_id}")

    if listing.listing_status != ListingStatus.PUBLISHED.value:
        raise ListingError(
            f"revise 対象は published のみ: 現在 listing_status={listing.listing_status}"
        )
    if not listing.ebay_offer_id:
        raise ListingError("ebay_offer_id 未設定 — revise 不可")
    if listing.product_id is None:
        raise ListingError("product 紐付けが切れています — revise 不可")

    product = await session.get(Product, listing.product_id)
    if product is None:
        raise ListingError(f"product not found: id={listing.product_id}")

    _enforce_risk_policy(product)

    # ── Amazon 再取得（任意）
    if request.refetch_amazon:
        await _refetch_amazon_into_product(product, session)

    # ── 在庫切れ → end_listing or quantity=0
    if not product.in_stock:
        if request.out_of_stock_action == OOS_ACTION_END_LISTING:
            logger.info(
                "revise: out-of-stock → end_listing listing_id=%d sku=%s",
                listing.id, listing.sku,
            )
            return await _end_listing(listing, session, dry_run=request.dry_run)
        elif request.out_of_stock_action == OOS_ACTION_SET_ZERO:
            quantity = 0
            logger.info(
                "revise: out-of-stock → quantity=0 listing_id=%d sku=%s",
                listing.id, listing.sku,
            )
        else:
            raise ListingError(
                f"out_of_stock_action='{request.out_of_stock_action}' は無効"
                f"（'set_zero' または 'end_listing' を指定）"
            )
    else:
        quantity = QUANTITY_DEFAULT

    # ── 価格再計算（Amazon refetch があれば最新の selling_price を使用）
    markup = (
        request.markup_percent
        if request.markup_percent is not None
        else settings.ebay_listing_markup_percent
    )
    price = _calculate_price(product.selling_price or 0, markup, listing.currency)

    # ── 新ペイロード
    title = _truncate_title(product.title)
    description = _build_description(product)
    image_urls = [product.image_url] if product.image_url else []
    inventory_payload = _build_inventory_payload(
        title, description, image_urls, quantity
    )
    offer_payload = _build_offer_payload(
        sku=listing.sku,
        price=price,
        currency=listing.currency,
        marketplace_id=listing.marketplace_id,
        category_id=listing.category_id or settings.ebay_listing_category_id,
        quantity=quantity,
    )

    # ── DB 先行更新（送信内容を保存）
    listing.title = title
    listing.description = description
    listing.image_url = product.image_url
    listing.listing_price = price
    listing.quantity = quantity
    listing.request_payload = {
        "inventory_item": inventory_payload,
        "offer": offer_payload,
    }
    await session.commit()

    logger.info(
        "revise start id=%d sku=%s offer_id=%s qty=%d price=%s dry_run=%s",
        listing.id, listing.sku, listing.ebay_offer_id,
        quantity, price, request.dry_run,
    )

    if request.dry_run:
        listing.sync_status = SyncStatus.DRY_RUN.value
        listing.last_synced_at = datetime.now(timezone.utc)
        await session.commit()
        await session.refresh(listing)
        return ListingDetail.model_validate(listing)

    _validate_listing_settings()
    token = await get_access_token()

    # ── PUT inventory_item
    inv_ok = False
    inv_err: str | None = None
    try:
        await _update_inventory_item(token, listing.sku, inventory_payload)
        inv_ok = True
        logger.info(
            "revise inventory_item ok id=%d sku=%s", listing.id, listing.sku
        )
    except Exception as e:
        inv_err = str(e)
        logger.warning(
            "revise inventory_item failed id=%d sku=%s err=%s",
            listing.id, listing.sku, e,
        )

    # ── PUT offer
    offer_ok = False
    offer_err: str | None = None
    try:
        await _update_offer(token, listing.ebay_offer_id, offer_payload)
        offer_ok = True
        logger.info(
            "revise offer ok id=%d offer_id=%s",
            listing.id, listing.ebay_offer_id,
        )
    except Exception as e:
        offer_err = str(e)
        logger.warning(
            "revise offer failed id=%d offer_id=%s err=%s",
            listing.id, listing.ebay_offer_id, e,
        )

    # ── sync_status 決定
    now = datetime.now(timezone.utc)
    listing.last_synced_at = now

    if inv_ok and offer_ok:
        listing.sync_status = SyncStatus.OK.value
        listing.sync_error_message = None
        listing.revised_at = now
        logger.info(
            "revise complete id=%d sku=%s qty=%d price=%s",
            listing.id, listing.sku, quantity, price,
        )
    elif inv_ok or offer_ok:
        listing.sync_status = SyncStatus.PARTIAL.value
        listing.sync_error_message = "; ".join(
            m for m in (
                f"inventory: {inv_err}" if inv_err else None,
                f"offer: {offer_err}" if offer_err else None,
            )
            if m
        )
        # revised_at は更新しない（不完全な状態）
        logger.warning(
            "revise partial id=%d inv_ok=%s offer_ok=%s msg=%s",
            listing.id, inv_ok, offer_ok, listing.sync_error_message,
        )
    else:
        listing.sync_status = SyncStatus.ERROR.value
        listing.sync_error_message = (
            f"inventory: {inv_err}; offer: {offer_err}"
        )
        logger.warning(
            "revise error id=%d msg=%s",
            listing.id, listing.sync_error_message,
        )

    await session.commit()
    await session.refresh(listing)
    return ListingDetail.model_validate(listing)


async def _refetch_amazon_into_product(
    product: Product,
    session: AsyncSession,
) -> None:
    """revise 直前に Amazon を再スクレイプして Product を最新化する。"""
    try:
        data = await scrape_amazon(product.url)
        product.title = data["title"] or product.title
        product.price = data["price"]
        product.in_stock = data["in_stock"]
        product.image_url = data["image_url"]
        await session.commit()
        await session.refresh(product)
        logger.info(
            "revise amazon refetch ok product_id=%d in_stock=%s price=%d",
            product.id, product.in_stock, product.price,
        )
    except Exception as e:
        # 失敗してもDB上の最新値で revise を試みる
        await session.rollback()
        logger.warning(
            "revise amazon refetch failed product_id=%d err=%s (continuing with DB state)",
            product.id, e,
        )


async def _end_listing(
    listing: Listing,
    session: AsyncSession,
    *,
    dry_run: bool,
) -> ListingDetail:
    """withdraw で出品を取り下げる。dry_run 時は DB のみ。"""
    now = datetime.now(timezone.utc)

    if dry_run:
        listing.quantity = 0
        listing.sync_status = SyncStatus.DRY_RUN.value
        listing.last_synced_at = now
        await session.commit()
        await session.refresh(listing)
        logger.info(
            "revise end_listing dry_run id=%d sku=%s", listing.id, listing.sku
        )
        return ListingDetail.model_validate(listing)

    token = await get_access_token()
    try:
        await _withdraw_offer(token, listing.ebay_offer_id)
    except Exception as e:
        listing.sync_status = SyncStatus.ERROR.value
        listing.sync_error_message = f"end_listing failed: {e}"
        listing.last_synced_at = now
        await session.commit()
        logger.warning(
            "revise end_listing failed id=%d sku=%s err=%s",
            listing.id, listing.sku, e,
        )
        raise ListingError(f"end_listing failed: {e}")

    listing.listing_status = ListingStatus.ENDED.value
    listing.quantity = 0
    listing.sync_status = SyncStatus.OK.value
    listing.sync_error_message = None
    listing.revised_at = now
    listing.last_synced_at = now
    await session.commit()
    await session.refresh(listing)
    logger.info(
        "revise end_listing ok id=%d sku=%s offer_id=%s",
        listing.id, listing.sku, listing.ebay_offer_id,
    )
    return ListingDetail.model_validate(listing)


# ─────────────────────────────────────
# policy gates
# ─────────────────────────────────────
def _enforce_risk_policy(product: Product) -> None:
    if product.risk_level != "low":
        raise RiskBlockedError(
            f"出品/revise禁止: risk_level={product.risk_level}（low 以外不可）"
        )
    if product.monitoring_status == MonitoringStatus.DANGER.value:
        raise RiskBlockedError("出品/revise禁止: monitoring_status=danger")


def _validate_listing_settings() -> None:
    missing: list[str] = []
    if not settings.ebay_listing_category_id:
        missing.append("EBAY_LISTING_CATEGORY_ID")
    if not settings.ebay_merchant_location_key:
        missing.append("EBAY_MERCHANT_LOCATION_KEY")
    if not settings.ebay_fulfillment_policy_id:
        missing.append("EBAY_FULFILLMENT_POLICY_ID")
    if not settings.ebay_payment_policy_id:
        missing.append("EBAY_PAYMENT_POLICY_ID")
    if not settings.ebay_return_policy_id:
        missing.append("EBAY_RETURN_POLICY_ID")
    if missing:
        raise ListingError(
            f"出品に必要な設定が未指定です: {', '.join(missing)}"
        )


# ─────────────────────────────────────
# helpers
# ─────────────────────────────────────
def _generate_sku(product_id: int) -> str:
    return f"{settings.ebay_sku_prefix}{product_id}"


def _truncate_title(title: str) -> str:
    cleaned = (title or "").strip()
    return cleaned[:TITLE_MAX_LENGTH]


def _build_description(product: Product) -> str:
    safe_title = html.escape(product.title or "")
    return (
        f"<p><b>{safe_title}</b></p>"
        f"<p>Brand new product. Shipping from Japan.</p>"
        f"<p>Please contact us via eBay messages if you have any questions.</p>"
    )


def _calculate_price(
    selling_price_jpy: int,
    markup_percent: float,
    currency: str,
) -> str:
    final_jpy = selling_price_jpy * (1 + markup_percent / 100.0)
    if currency.upper() == "JPY":
        return str(int(final_jpy))
    final_usd = final_jpy / settings.fx_rate_usd_jpy
    return f"{final_usd:.2f}"


# ── payload builders ────────────────
def _build_inventory_payload(
    title: str,
    description: str,
    image_urls: list[str],
    quantity: int,
) -> dict:
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


def _build_offer_payload(
    *,
    sku: str,
    price: str,
    currency: str,
    marketplace_id: str,
    category_id: str,
    quantity: int,
) -> dict:
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
        "listingPolicies": {
            "fulfillmentPolicyId": settings.ebay_fulfillment_policy_id,
            "paymentPolicyId": settings.ebay_payment_policy_id,
            "returnPolicyId": settings.ebay_return_policy_id,
        },
    }


# ─────────────────────────────────────
# HTTP layer (tenacity retry + timeout)
# ─────────────────────────────────────
@retry(
    stop=stop_after_attempt(max(1, settings.ebay_listing_max_retries + 1)),
    wait=wait_exponential(multiplier=1, min=1, max=5),
    retry=retry_if_exception_type((httpx.HTTPError,)),
    reraise=True,
)
async def _ebay_request(
    method: str,
    path: str,
    *,
    token: str,
    json_body: dict | None = None,
) -> dict:
    url = f"{settings.ebay_api_base}{path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Content-Language": "en-US",
    }

    async with httpx.AsyncClient(
        timeout=settings.ebay_listing_request_timeout
    ) as client:
        res = await client.request(
            method, url, headers=headers, json=json_body
        )

    if res.status_code >= 400:
        raise ListingError(
            f"eBay {method} {path} 失敗: status={res.status_code} "
            f"body={res.text[:500]}"
        )

    if res.status_code == 204 or not res.text:
        return {}

    try:
        return res.json()
    except ValueError:
        return {"raw": res.text}


# ─────────────────────────────────────
# eBay API wrappers
# ─────────────────────────────────────
async def _put_inventory_item(token: str, sku: str, payload: dict) -> dict:
    """PUT /inventory_item/{sku}（createOrReplace）"""
    return await _ebay_request(
        "PUT",
        f"/sell/inventory/v1/inventory_item/{sku}",
        token=token,
        json_body=payload,
    )


async def _update_inventory_item(token: str, sku: str, payload: dict) -> dict:
    """revise 用 alias（PUT は create と replace 両方を兼ねる）"""
    return await _put_inventory_item(token, sku, payload)


async def _create_offer(token: str, payload: dict) -> dict:
    return await _ebay_request(
        "POST", "/sell/inventory/v1/offer",
        token=token, json_body=payload,
    )


async def _update_offer(token: str, offer_id: str, payload: dict) -> dict:
    return await _ebay_request(
        "PUT", f"/sell/inventory/v1/offer/{offer_id}",
        token=token, json_body=payload,
    )


async def _publish_offer(token: str, offer_id: str) -> dict:
    return await _ebay_request(
        "POST", f"/sell/inventory/v1/offer/{offer_id}/publish",
        token=token,
    )


async def _withdraw_offer(token: str, offer_id: str) -> dict:
    return await _ebay_request(
        "POST", f"/sell/inventory/v1/offer/{offer_id}/withdraw",
        token=token,
    )


async def _safe_delete_offer(token: str, offer_id: str) -> None:
    try:
        await _ebay_request(
            "DELETE", f"/sell/inventory/v1/offer/{offer_id}",
            token=token,
        )
        logger.info("rollback: deleted offer offer_id=%s", offer_id)
    except Exception as e:
        logger.warning(
            "rollback: failed to delete offer offer_id=%s err=%s",
            offer_id, e,
        )


async def _safe_delete_inventory_item(token: str, sku: str) -> None:
    try:
        await _ebay_request(
            "DELETE", f"/sell/inventory/v1/inventory_item/{sku}",
            token=token,
        )
        logger.info("rollback: deleted inventory_item sku=%s", sku)
    except Exception as e:
        logger.warning(
            "rollback: failed to delete inventory_item sku=%s err=%s",
            sku, e,
        )


# ── DB helpers ──────────────────────
async def _mark_failed(
    session: AsyncSession,
    listing: Listing,
    error_message: str,
) -> None:
    listing.listing_status = ListingStatus.FAILED.value
    listing.error_message = error_message
    listing.sync_status = SyncStatus.ERROR.value
    listing.sync_error_message = error_message
    listing.last_synced_at = datetime.now(timezone.utc)
    await session.commit()
    logger.warning(
        "listing failed id=%d sku=%s err=%s",
        listing.id, listing.sku, error_message,
    )
