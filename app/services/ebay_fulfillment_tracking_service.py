"""eBay fulfillment tracking upload + CJ tracking integration placeholder。

設計:
  - tracking upload: POST /sell/fulfillment/v1/order/{orderId}/shipping_fulfillment
  - 失敗しても order sync を止めない（DB 状態は最後に commit、API 失敗時は graceful）
  - 将来 webhook（CJ から「shipped」push）を受けるために、
    apply_tracking_to_order() を service として独立させ、
    HTTP 経路でも webhook 経路でも同じ entry-point を使えるようにしてある。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import FulfillmentStatus, Order, OrderItem
from app.services.ebay_auth_service import EbayAuthError, get_access_token
from app.services.ebay_listing_service import ListingError, _ebay_request

logger = logging.getLogger(__name__)


# ── eBay tracking upload ─────────────
async def upload_tracking_to_ebay(
    *,
    token: str,
    ebay_order_id: str,
    tracking_number: str,
    carrier_code: str,
    line_items: list[dict[str, Any]],
    shipped_date: datetime | None = None,
) -> dict[str, Any]:
    iso = (shipped_date or datetime.now(timezone.utc)).astimezone(
        timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    body = {
        "lineItems": line_items,
        "shippedDate": iso,
        "shippingCarrierCode": carrier_code,
        "trackingNumber": tracking_number,
    }
    return await _ebay_request(
        "POST",
        f"/sell/fulfillment/v1/order/{ebay_order_id}/shipping_fulfillment",
        token=token,
        json_body=body,
    )


# ── DB-side: tracking 反映 ───────────
async def apply_tracking_to_order(
    session: AsyncSession,
    *,
    order_id_or_ebay_id: str,
    tracking_number: str,
    carrier_code: str,
    upload_to_ebay: bool = True,
) -> dict[str, Any]:
    """webhook / endpoint 共通の入口。upload_to_ebay=False なら DB だけ更新。"""
    order = None
    if order_id_or_ebay_id.isdigit():
        order = await session.get(Order, int(order_id_or_ebay_id))
    if order is None:
        result = await session.execute(
            select(Order).where(Order.ebay_order_id == order_id_or_ebay_id)
        )
        order = result.scalar_one_or_none()
    if order is None:
        raise ListingError(f"order not found: {order_id_or_ebay_id}")

    items_result = await session.execute(
        select(OrderItem).where(OrderItem.order_id == order.id)
    )
    items = list(items_result.scalars().all())
    line_items_payload = [
        {"lineItemId": it.sku, "quantity": it.quantity}
        for it in items if it.sku
    ]

    upload_status: dict[str, Any] = {"uploaded": False}
    if upload_to_ebay:
        try:
            token = await get_access_token()
            await upload_tracking_to_ebay(
                token=token,
                ebay_order_id=order.ebay_order_id,
                tracking_number=tracking_number,
                carrier_code=carrier_code,
                line_items=line_items_payload,
            )
            upload_status = {"uploaded": True}
            logger.info(
                "tracking uploaded ebay_order_id=%s carrier=%s tn=%s",
                order.ebay_order_id, carrier_code, tracking_number,
            )
        except (ListingError, EbayAuthError) as e:
            upload_status = {"uploaded": False, "error": str(e)[:500]}
            logger.warning(
                "tracking upload failed (graceful) ebay_order_id=%s err=%s",
                order.ebay_order_id, e,
            )
        except Exception as e:
            upload_status = {"uploaded": False, "error": str(e)[:500]}
            logger.warning(
                "tracking upload unexpected error (graceful) "
                "ebay_order_id=%s err=%s",
                order.ebay_order_id, e,
            )

    # DB 反映は upload 失敗でも実行（運用上、トラッキングを記録しておくのは有益）
    order.tracking_number = tracking_number
    order.shipping_carrier = carrier_code
    order.shipped_at = datetime.now(timezone.utc)
    order.fulfillment_status = FulfillmentStatus.SHIPPED.value
    await session.commit()

    return {
        "success": upload_status["uploaded"],
        "ebay_order_id": order.ebay_order_id,
        "tracking_number": tracking_number,
        "carrier": carrier_code,
        "shipped_at": order.shipped_at.isoformat(),
        "ebay_upload": upload_status,
    }


# ── CJ tracking integration ──────────
async def sync_cj_tracking_for_order(
    session: AsyncSession, order_id: int
) -> dict[str, Any]:
    """CJ 注文の状態を引き直し、出荷済みなら tracking を eBay に反映する。

    実体は cj_order_service.sync_cj_order()（循環 import を避けて関数内 import）。
    """
    order = await session.get(Order, order_id)
    if order is None:
        raise ListingError(f"order not found id={order_id}")

    from app.services.cj_order_service import (
        CJOrderError,
        get_cj_orders_for_ebay_order,
        sync_cj_order,
    )

    try:
        cj_orders = await get_cj_orders_for_ebay_order(session, order.ebay_order_id)
    except CJOrderError as e:
        logger.info(
            "cj tracking sync skipped ebay_order_id=%s reason=%s",
            order.ebay_order_id, e,
        )
        return {"success": False, "skipped": True, "reason": str(e)}

    # 分割発注なら小包ごとに同期する
    results = [await sync_cj_order(session, cj) for cj in cj_orders]
    return {
        "success": all(r.get("success") for r in results),
        "parcel_count": len(results),
        "parcels": results,
    }
