"""eBay auto repricing system。

published 商品を bulk で巡回し、現在の Product 状態に応じて：
  - 利益率 < MIN_PROFIT_RATE → withdraw
  - Amazon在庫切れ → quantity=0
  - 利益率維持できる場合のみ revise_price
を実行し、すべての操作を repricing_logs に audit する。

既存 ebay_revise_service の判定ロジック（_decide_action）と low-level 操作
（revise_price / revise_quantity / safe_withdraw_offer）を再利用。

dry_run=True で実行すると eBay API は呼ばず、log だけ書き込む。
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import Product, RepricingLog
from app.services.ebay_auth_service import EbayAuthError, get_access_token
from app.services.ebay_listing_service import ListingError
from app.services.ebay_offer_service import safe_withdraw_offer
from app.services.ebay_payload_builder import calculate_price
from app.services.ebay_revise_service import (
    _decide_action,
    revise_price,
    revise_quantity,
)

logger = logging.getLogger(__name__)


async def _log(
    session: AsyncSession,
    *,
    product: Product | None,
    action: str,
    reason: str,
    old_price: str | None = None,
    new_price: str | None = None,
    currency: str | None = None,
    success: bool = True,
    error: str | None = None,
    dry_run: bool = False,
) -> None:
    session.add(RepricingLog(
        product_id=product.id if product else None,
        sku=product.ebay_sku if product else None,
        action=action,
        reason=reason,
        old_price=old_price,
        new_price=new_price,
        currency=currency,
        success=success,
        error_message=error,
        dry_run=dry_run,
    ))
    await session.commit()


async def _reprice_one(
    session: AsyncSession, product: Product, *, dry_run: bool
) -> dict[str, Any]:
    action, reason = _decide_action(product)

    if action == "noop":
        return {"action": "noop", "reason": reason}

    currency = settings.ebay_listing_currency
    old_price: str | None = None
    new_price: str | None = None
    if product.selling_price is not None:
        new_price = calculate_price(
            product.selling_price,
            settings.ebay_listing_markup_percent,
            currency,
        )

    if dry_run:
        await _log(
            session, product=product, action=action, reason=reason,
            old_price=old_price, new_price=new_price, currency=currency,
            success=True, dry_run=True,
        )
        logger.info(
            "repricing dry_run sku=%s action=%s reason=%s new_price=%s",
            product.ebay_sku, action, reason, new_price,
        )
        return {"action": action, "reason": reason, "dry_run": True}

    if not product.ebay_sku or not product.ebay_offer_id:
        await _log(
            session, product=product, action=action, reason=reason,
            success=False, error="ebay_sku/offer_id missing",
        )
        return {"action": action, "reason": reason, "success": False}

    try:
        token = await get_access_token()
        if action == "withdraw":
            await safe_withdraw_offer(token, product.ebay_offer_id)
            product.ebay_listing_status = "WITHDRAWN"
        elif action == "quantity_zero":
            await revise_quantity(token, product.ebay_sku, 0)
        elif action == "revise_price" and new_price:
            await revise_price(
                token, product.ebay_offer_id, price=new_price, currency=currency,
            )
            await revise_quantity(
                token, product.ebay_sku, 1 if product.in_stock else 0,
            )
        await session.commit()
        await _log(
            session, product=product, action=action, reason=reason,
            old_price=old_price, new_price=new_price, currency=currency,
            success=True,
        )
        return {
            "action": action, "reason": reason, "success": True,
            "new_price": new_price,
        }

    except (ListingError, EbayAuthError) as e:
        msg = str(e)[:500]
        logger.warning(
            "repricing failed sku=%s action=%s err=%s",
            product.ebay_sku, action, e,
        )
        await session.rollback()
        await _log(
            session, product=product, action=action, reason=reason,
            old_price=old_price, new_price=new_price, currency=currency,
            success=False, error=msg,
        )
        return {"action": action, "reason": reason, "success": False, "error": msg}


async def bulk_reprice(
    session: AsyncSession, *, dry_run: bool = False, limit: int = 200
) -> dict[str, int]:
    """全 PUBLISHED 商品を巡回。"""
    counters = {
        "scanned": 0,
        "noop": 0,
        "repriced": 0,
        "withdrawn": 0,
        "quantity_zeroed": 0,
        "failed": 0,
    }

    stmt = (
        select(Product)
        .where(Product.ebay_listing_status == "PUBLISHED")
        .limit(limit)
    )
    products = (await session.execute(stmt)).scalars().all()

    for p in products:
        counters["scanned"] += 1
        try:
            result = await _reprice_one(session, p, dry_run=dry_run)
        except Exception as e:
            logger.warning(
                "reprice unexpected error sku=%s err=%s", p.ebay_sku, e,
            )
            counters["failed"] += 1
            continue

        action = result.get("action")
        if action == "noop":
            counters["noop"] += 1
        elif result.get("success") is False:
            counters["failed"] += 1
        elif action == "withdraw":
            counters["withdrawn"] += 1
        elif action == "quantity_zero":
            counters["quantity_zeroed"] += 1
        elif action == "revise_price":
            counters["repriced"] += 1

    logger.info(
        "bulk_reprice done dry_run=%s scanned=%d noop=%d repriced=%d "
        "withdrawn=%d quantity_zeroed=%d failed=%d",
        dry_run, counters["scanned"], counters["noop"], counters["repriced"],
        counters["withdrawn"], counters["quantity_zeroed"], counters["failed"],
    )
    return counters
