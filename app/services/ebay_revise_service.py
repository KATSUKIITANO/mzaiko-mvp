"""eBay listing revise / auto-stop システム。

スケジューラから 1 商品ずつ呼ばれ、現在の Product の状態に応じて
eBay listing を revise（quantity / price）または withdraw する。

判定優先順位:
  C: risk_level == 'high'        → withdraw（即停止）
  A: is_profitable == False       → withdraw
  D: in_stock == False            → quantity=0（停止ではない、再入荷待ち）
  B: profit_rate < MIN_PROFIT_RATE
       AUTO_REVISE_PRICE=true     → revise price + quantity 同期
       AUTO_REVISE_PRICE=false    → withdraw

graceful: eBay 失敗しても scheduler を止めない（caller 側で握り潰す前提だが、
本サービスでも DB に sync_failed を記録して再試行できるようにする）。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import Product
from app.services.ebay_auth_service import EbayAuthError, get_access_token
from app.services.ebay_inventory_service import get_inventory_item
from app.services.ebay_listing_service import (
    ListingError,
    _ebay_request,
)
from app.services.ebay_offer_service import (
    safe_withdraw_offer,
    update_offer,
)
from app.services.ebay_payload_builder import calculate_price

logger = logging.getLogger(__name__)


# ── low-level revisers ──────────────
async def revise_quantity(token: str, sku: str, quantity: int) -> None:
    """既存 inventory_item の quantity だけ差し替えて PUT。

    eBay 仕様: PUT /inventory_item/{sku} は createOrReplace なので、
    既存フィールドを保つために GET → merge → PUT する。
    """
    current = await get_inventory_item(token, sku)
    if current is None:
        raise ListingError(f"inventory_item not found sku={sku}")

    payload = dict(current)
    avail = dict(payload.get("availability") or {})
    ship_avail = dict(avail.get("shipToLocationAvailability") or {})
    ship_avail["quantity"] = quantity
    avail["shipToLocationAvailability"] = ship_avail
    payload["availability"] = avail

    await _ebay_request(
        "PUT",
        f"/sell/inventory/v1/inventory_item/{sku}",
        token=token,
        json_body=payload,
    )
    logger.info("quantity updated sku=%s quantity=%d", sku, quantity)


async def revise_price(
    token: str, offer_id: str, *, price: str, currency: str
) -> None:
    """offer の pricingSummary.price を差し替えて PUT。"""
    payload = {
        "pricingSummary": {"price": {"currency": currency, "value": price}},
    }
    await update_offer(token, offer_id, payload)
    logger.info(
        "listing revised offer_id=%s price=%s %s", offer_id, price, currency
    )


# ── high-level: per-product sync ────
def _decide_action(product: Product) -> tuple[str, str]:
    """(action, reason) を返す。"""
    if product.risk_level == "high":
        return "withdraw", "risk_high"
    if not product.is_profitable:
        return "withdraw", "not_profitable"
    if not product.in_stock:
        return "quantity_zero", "out_of_stock"
    rate = product.current_profit_rate
    if rate is not None and rate < settings.min_profit_rate:
        if settings.auto_revise_price:
            return "revise_price", "low_profit_rate"
        return "withdraw", "low_profit_rate"
    return "noop", "all_ok"


async def _do_withdraw(product: Product, *, reason: str) -> None:
    if not product.ebay_offer_id:
        raise ListingError("ebay_offer_id missing; cannot withdraw")
    token = await get_access_token()
    await safe_withdraw_offer(token, product.ebay_offer_id)
    logger.info(
        "listing withdrawn sku=%s offer_id=%s reason=%s",
        product.ebay_sku, product.ebay_offer_id, reason,
    )


async def _do_quantity_zero(product: Product) -> None:
    if not product.ebay_sku:
        raise ListingError("ebay_sku missing; cannot revise quantity")
    token = await get_access_token()
    await revise_quantity(token, product.ebay_sku, 0)


async def _do_revise_price(product: Product) -> dict[str, Any]:
    """price revise + quantity 同期。

    rollback safety: price 失敗しても quantity 同期は試行する。
    quantity 同期は復活/減算で必要（再入荷など）。
    """
    if not product.ebay_sku or not product.ebay_offer_id:
        raise ListingError("ebay_sku / ebay_offer_id missing")

    token = await get_access_token()
    currency = settings.ebay_listing_currency
    new_price = calculate_price(
        product.selling_price or 0,
        settings.ebay_listing_markup_percent,
        currency,
    )

    price_ok = True
    price_err: str | None = None
    try:
        await revise_price(
            token, product.ebay_offer_id, price=new_price, currency=currency,
        )
    except (ListingError, EbayAuthError) as e:
        price_ok = False
        price_err = str(e)
        logger.warning(
            "price revise failed sku=%s err=%s; "
            "continuing with quantity sync",
            product.ebay_sku, e,
        )

    quantity = 1 if product.in_stock else 0
    quantity_ok = True
    try:
        await revise_quantity(token, product.ebay_sku, quantity)
    except (ListingError, EbayAuthError) as e:
        quantity_ok = False
        logger.warning(
            "quantity sync failed sku=%s err=%s",
            product.ebay_sku, e,
        )
        if not price_ok:
            raise

    return {
        "price_revised": price_ok,
        "price_error": price_err,
        "quantity_revised": quantity_ok,
        "new_price": new_price,
        "currency": currency,
    }


async def sync_ebay_listing(
    product: Product, session: AsyncSession
) -> dict[str, Any]:
    """1商品分のeBay同期。失敗してもDBに sync_failed を記録して例外は raise しない。"""
    sku = product.ebay_sku

    if not settings.auto_stop_enabled:
        return {
            "success": True,
            "action": "skipped",
            "sku": sku,
            "reason": "auto_stop_disabled",
        }

    if product.ebay_listing_status != "PUBLISHED":
        return {
            "success": True,
            "action": "noop",
            "sku": sku,
            "reason": "not_published",
        }

    action, reason = _decide_action(product)
    extra: dict[str, Any] = {}

    try:
        if action == "withdraw":
            await _do_withdraw(product, reason=reason)
            product.ebay_listing_status = "WITHDRAWN"
        elif action == "quantity_zero":
            await _do_quantity_zero(product)
        elif action == "revise_price":
            extra = await _do_revise_price(product)
        # noop: 何もしない

        product.ebay_last_sync_at = datetime.now(timezone.utc)
        product.ebay_sync_status = action
        product.ebay_sync_error = None
        await session.commit()
        logger.info(
            "sync ok sku=%s action=%s reason=%s", sku, action, reason
        )
        return {
            "success": True,
            "action": action,
            "sku": sku,
            "reason": reason,
            **extra,
        }

    except Exception as e:
        msg = str(e)[:500]
        logger.error(
            "sync failed sku=%s action=%s reason=%s err=%s",
            sku, action, reason, e,
        )
        product.ebay_last_sync_at = datetime.now(timezone.utc)
        product.ebay_sync_status = "sync_failed"
        product.ebay_sync_error = msg
        await session.commit()
        return {
            "success": False,
            "action": action,
            "sku": sku,
            "reason": reason,
            "error": msg,
        }


# ── 単独 endpoint 用：手動 withdraw ─
async def withdraw_by_sku(
    sku: str, session: AsyncSession
) -> dict[str, Any]:
    from sqlalchemy import select

    result = await session.execute(
        select(Product).where(Product.ebay_sku == sku)
    )
    product = result.scalar_one_or_none()
    if product is None:
        raise ListingError(f"product not found by sku={sku}")
    if not product.ebay_offer_id:
        raise ListingError(f"offer_id missing for sku={sku}")

    try:
        await _do_withdraw(product, reason="manual")
        product.ebay_listing_status = "WITHDRAWN"
        product.ebay_last_sync_at = datetime.now(timezone.utc)
        product.ebay_sync_status = "withdraw"
        product.ebay_sync_error = None
        await session.commit()
        return {
            "success": True,
            "action": "withdraw",
            "sku": sku,
            "reason": "manual",
        }
    except Exception as e:
        msg = str(e)[:500]
        product.ebay_last_sync_at = datetime.now(timezone.utc)
        product.ebay_sync_status = "sync_failed"
        product.ebay_sync_error = msg
        await session.commit()
        return {
            "success": False,
            "action": "withdraw",
            "sku": sku,
            "reason": "manual",
            "error": msg,
        }
