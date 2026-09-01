"""eBay で売れた注文を CJ Dropshipping に反映する（自動発注 + 状態同期）。

フロー:
  1. sold sync が eBay 注文を検知 → Order 行 + fulfillment task を作成
  2. place_cj_order()
       SKU → CJProductMapping で CJ 商品(pid) を解決
       → vid（発注単位）を解決（未登録なら CJ API から取得して保存）
       → 配送先を組み立て → CJ createOrderV2
       → CJOrder 行に cj_order_id / payload / status を記録
  3. sync_cj_order()（定期ワーカー）
       CJ 注文詳細をポーリング → 出荷されたら tracking を取得
       → eBay Fulfillment API へ tracking を反映（closed loop）

発注トリガ（config）:
  - CJ_AUTO_ORDER_ON_SOLD=true  → 売れた瞬間に自動発注（既定 false）
  - CJ_ORDER_ON_APPROVE=true    → fulfillment task approve 時に発注（既定 true）
  - どちらも OFF なら POST /cj/orders/{ebay_order_id}/place で手動発注

安全側の既定:
  - CJ_ORDER_DRY_RUN=true → payload を組み立てて保存するだけで CJ は呼ばない
  - 支払いは行わない（CJ 側に未払い注文として残るので人間が最終確認できる）
  - 発注失敗・住所不備・未マッピングは CJOrder に理由を残し、eBay 側の処理は止めない
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.database import AsyncSessionLocal
from app.db.models import (
    CJOrder,
    CJOrderStatus,
    FulfillmentTask,
    Order,
    OrderItem,
    OrderStatus,
)
from app.scraper.cj import (
    CJError,
    cj_create_order,
    cj_order_detail,
    extract_cj_order_id,
    extract_cj_order_num,
    extract_tracking_number,
)
from app.services.cj_mapping_service import get_mapping, resolve_variant_id
from app.services.ebay_fulfillment_tracking_service import apply_tracking_to_order

logger = logging.getLogger(__name__)

# 発注を再試行できる（＝まだ CJ に注文が無い）状態
_RETRYABLE_STATUSES = (
    CJOrderStatus.PENDING.value,
    CJOrderStatus.DRY_RUN.value,
    CJOrderStatus.UNMAPPED.value,
    CJOrderStatus.FAILED.value,
)

# 状態同期の対象（CJ 側でまだ動く可能性がある）
_SYNCABLE_STATUSES = (
    CJOrderStatus.CREATED.value,
    CJOrderStatus.SHIPPED.value,
)


class CJOrderError(Exception):
    pass


@dataclass
class CJOrderSyncMetrics:
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None
    placed: int = 0
    place_failed: int = 0
    unmapped: int = 0
    synced: int = 0
    shipped: int = 0
    tracking_pushed: int = 0
    sync_failed: int = 0

    @property
    def duration_seconds(self) -> float | None:
        if self.finished_at is None:
            return None
        return (self.finished_at - self.started_at).total_seconds()

    def as_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "duration_seconds": self.duration_seconds,
            "placed": self.placed,
            "place_failed": self.place_failed,
            "unmapped": self.unmapped,
            "synced": self.synced,
            "shipped": self.shipped,
            "tracking_pushed": self.tracking_pushed,
            "sync_failed": self.sync_failed,
        }


_last_metrics: CJOrderSyncMetrics | None = None


def get_last_metrics() -> CJOrderSyncMetrics | None:
    return _last_metrics


# ─────────────────────────────────────
# 発注
# ─────────────────────────────────────
async def place_cj_order(
    session: AsyncSession,
    order: Order,
    *,
    dry_run: bool | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """eBay 注文 1件を CJ に発注する（冪等）。

    既に CJ 注文が作られている場合は再発注せず、その内容を返す。
    force=True で失敗済み行の再試行上限を無視する。
    """
    effective_dry_run = (
        settings.cj_order_dry_run if dry_run is None else dry_run
    )

    cj_order = await _get_cj_order_by_order_id(session, order.id)

    if cj_order is not None and cj_order.cj_order_id:
        logger.info(
            "cj order already placed ebay_order_id=%s cj_order_id=%s (skip)",
            order.ebay_order_id, cj_order.cj_order_id,
        )
        return _result(cj_order, skipped=True, reason="already_placed")

    if cj_order is None:
        cj_order = CJOrder(
            order_id=order.id,
            ebay_order_id=order.ebay_order_id,
            sku=order.sku,
            quantity=order.quantity,
            shipping_country=order.buyer_country,
            status=CJOrderStatus.PENDING.value,
        )
        session.add(cj_order)
        await session.commit()
        await session.refresh(cj_order)

    if (
        not force
        and cj_order.status == CJOrderStatus.FAILED.value
        and cj_order.attempts >= settings.cj_order_max_attempts
    ):
        logger.warning(
            "cj order attempts exhausted ebay_order_id=%s attempts=%d (skip)",
            order.ebay_order_id, cj_order.attempts,
        )
        return _result(cj_order, skipped=True, reason="max_attempts_reached")

    # ── 発注明細（SKU → CJ vid）
    try:
        products, item_notes = await _build_products(session, order)
    except _UnmappedSkuError as e:
        return await _fail(
            session, cj_order, str(e), status=CJOrderStatus.UNMAPPED.value
        )

    # ── 配送先
    buyer_name, address = await _resolve_shipping(session, order)
    missing = _validate_address(address)
    if missing:
        return await _fail(
            session,
            cj_order,
            f"invalid shipping address: missing {','.join(missing)}",
        )
    if not buyer_name:
        return await _fail(session, cj_order, "invalid shipping address: missing name")

    payload = _build_order_payload(
        order=order,
        buyer_name=buyer_name,
        address=address,
        products=products,
    )

    cj_order.request_payload = payload
    cj_order.quantity = sum(p["quantity"] for p in products)
    cj_order.shipping_country = address.get("country")
    cj_order.logistic_name = settings.cj_order_logistic_name or None
    cj_order.dry_run = effective_dry_run

    if effective_dry_run:
        cj_order.status = CJOrderStatus.DRY_RUN.value
        cj_order.error_message = None
        await session.commit()
        await session.refresh(cj_order)
        logger.info(
            "cj order DRY_RUN ebay_order_id=%s products=%d notes=%s",
            order.ebay_order_id, len(products), item_notes,
        )
        return _result(cj_order, dry_run=True)

    cj_order.attempts += 1
    await session.commit()

    try:
        created = await cj_create_order(payload)
    except CJError as e:
        return await _fail(session, cj_order, f"CJ API error: {e}")
    except Exception as e:
        return await _fail(session, cj_order, f"unexpected error: {e}")

    cj_order_id = extract_cj_order_id(created)
    if not cj_order_id:
        return await _fail(
            session, cj_order, f"CJ response without orderId: {str(created)[:300]}"
        )

    cj_order.cj_order_id = cj_order_id
    cj_order.cj_order_num = extract_cj_order_num(created)
    cj_order.status = CJOrderStatus.CREATED.value
    cj_order.cj_status = str(created.get("orderStatus") or "") or None
    cj_order.response_payload = created
    cj_order.error_message = None
    cj_order.placed_at = datetime.now(timezone.utc)

    if order.order_status in (
        OrderStatus.DETECTED.value,
        OrderStatus.PROCESSING.value,
    ):
        order.order_status = OrderStatus.ORDERED.value

    await session.commit()
    await session.refresh(cj_order)

    logger.info(
        "cj order created ebay_order_id=%s cj_order_id=%s products=%d country=%s",
        order.ebay_order_id, cj_order_id, len(products), cj_order.shipping_country,
    )
    return _result(cj_order)


async def place_cj_order_for_ebay_order(
    session: AsyncSession,
    order_ref: str,
    *,
    dry_run: bool | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """DB id / eBay order id のどちらでも発注できる入口（API・webhook 共通）。"""
    order = await _find_order(session, order_ref)
    if order is None:
        raise CJOrderError(f"order not found: {order_ref}")
    return await place_cj_order(session, order, dry_run=dry_run, force=force)


async def place_cj_order_if_enabled(
    session: AsyncSession,
    order: Order,
    *,
    trigger: str,
) -> dict[str, Any] | None:
    """自動発注 hook。無効時や失敗時も呼び出し元の処理は止めない。"""
    if not settings.cj_order_enabled:
        return None

    try:
        result = await place_cj_order(session, order)
    except Exception as e:
        logger.warning(
            "cj auto order failed (graceful) trigger=%s ebay_order_id=%s err=%s",
            trigger, order.ebay_order_id, e,
        )
        return None

    logger.info(
        "cj auto order trigger=%s ebay_order_id=%s status=%s",
        trigger, order.ebay_order_id, result.get("status"),
    )
    return result


# ─────────────────────────────────────
# 状態同期（CJ → eBay tracking）
# ─────────────────────────────────────
async def sync_cj_order(
    session: AsyncSession, cj_order: CJOrder
) -> dict[str, Any]:
    """CJ 注文の状態をポーリングし、出荷済みなら tracking を eBay へ反映。"""
    if not cj_order.cj_order_id:
        return _result(cj_order, skipped=True, reason="not_placed")

    try:
        detail = await cj_order_detail(cj_order.cj_order_id)
    except CJError as e:
        logger.warning(
            "cj order detail failed cj_order_id=%s err=%s",
            cj_order.cj_order_id, e,
        )
        cj_order.last_synced_at = datetime.now(timezone.utc)
        cj_order.error_message = f"detail fetch failed: {e}"[:1000]
        await session.commit()
        return _result(cj_order, skipped=True, reason="detail_failed")

    raw_status = str(detail.get("orderStatus") or detail.get("status") or "")
    tracking = extract_tracking_number(detail)
    local_status = _map_cj_status(raw_status, tracking)

    cj_order.cj_status = raw_status or None
    cj_order.status = local_status
    cj_order.last_synced_at = datetime.now(timezone.utc)
    cj_order.response_payload = detail
    if tracking:
        cj_order.tracking_number = tracking
    amount = detail.get("orderAmount") or detail.get("totalAmount")
    if amount is not None:
        cj_order.total_amount = str(amount)[:32]
    await session.commit()

    tracking_pushed = False
    if (
        tracking
        and local_status in (CJOrderStatus.SHIPPED.value, CJOrderStatus.DELIVERED.value)
        and settings.cj_order_push_tracking_to_ebay
    ):
        tracking_pushed = await _push_tracking(session, cj_order, tracking)

    await session.refresh(cj_order)
    logger.info(
        "cj order synced cj_order_id=%s cj_status=%s status=%s tracking=%s pushed=%s",
        cj_order.cj_order_id, raw_status or "-", local_status,
        tracking or "-", tracking_pushed,
    )
    return _result(cj_order, tracking_pushed=tracking_pushed)


async def _push_tracking(
    session: AsyncSession, cj_order: CJOrder, tracking: str
) -> bool:
    order = await session.get(Order, cj_order.order_id)
    if order is None:
        return False
    if order.tracking_number == tracking:
        return False

    try:
        await apply_tracking_to_order(
            session,
            order_id_or_ebay_id=order.ebay_order_id,
            tracking_number=tracking,
            carrier_code=settings.cj_default_carrier_code,
            upload_to_ebay=True,
        )
    except Exception as e:
        logger.warning(
            "tracking push to ebay failed (graceful) ebay_order_id=%s err=%s",
            order.ebay_order_id, e,
        )
        return False

    if order.order_status != OrderStatus.SHIPPED.value:
        order.order_status = OrderStatus.SHIPPED.value
        await session.commit()

    logger.info(
        "cj tracking reflected to ebay ebay_order_id=%s tracking=%s",
        order.ebay_order_id, tracking,
    )
    return True


# ─────────────────────────────────────
# 定期ワーカー
# ─────────────────────────────────────
async def run_cj_order_worker() -> CJOrderSyncMetrics:
    """未発注の CJ 注文を発注し、発注済みの状態を同期する。"""
    global _last_metrics

    metrics = CJOrderSyncMetrics()

    if not settings.cj_order_enabled:
        logger.info("cj order worker disabled (CJ_ORDER_ENABLED=false)")
        metrics.finished_at = metrics.started_at
        _last_metrics = metrics
        return metrics

    limit = settings.cj_order_sync_batch_limit

    try:
        async with AsyncSessionLocal() as session:
            for cj_order in await _fetch_placeable(session, limit):
                order = await session.get(Order, cj_order.order_id)
                if order is None:
                    continue
                try:
                    result = await place_cj_order(session, order)
                except Exception as e:
                    logger.warning(
                        "cj order place crashed (graceful) ebay_order_id=%s err=%s",
                        cj_order.ebay_order_id, e,
                    )
                    metrics.place_failed += 1
                    continue
                status = result.get("status")
                if status == CJOrderStatus.CREATED.value:
                    metrics.placed += 1
                elif status == CJOrderStatus.UNMAPPED.value:
                    metrics.unmapped += 1
                elif status == CJOrderStatus.FAILED.value:
                    metrics.place_failed += 1

            for cj_order in await _fetch_syncable(session, limit):
                try:
                    result = await sync_cj_order(session, cj_order)
                except Exception as e:
                    logger.warning(
                        "cj order sync crashed (graceful) cj_order_id=%s err=%s",
                        cj_order.cj_order_id, e,
                    )
                    metrics.sync_failed += 1
                    continue
                metrics.synced += 1
                if result.get("status") in (
                    CJOrderStatus.SHIPPED.value,
                    CJOrderStatus.DELIVERED.value,
                ):
                    metrics.shipped += 1
                if result.get("tracking_pushed"):
                    metrics.tracking_pushed += 1
    except Exception:
        logger.exception("cj order worker aborted")

    metrics.finished_at = datetime.now(timezone.utc)
    logger.info(
        "cj order worker complete duration=%.1fs placed=%d unmapped=%d "
        "place_failed=%d synced=%d shipped=%d tracking_pushed=%d sync_failed=%d",
        metrics.duration_seconds or 0,
        metrics.placed, metrics.unmapped, metrics.place_failed,
        metrics.synced, metrics.shipped, metrics.tracking_pushed,
        metrics.sync_failed,
    )
    _last_metrics = metrics
    return metrics


# ─────────────────────────────────────
# 取得・一覧
# ─────────────────────────────────────
async def list_cj_orders(
    session: AsyncSession,
    *,
    status: str | None,
    limit: int,
    offset: int,
) -> list[CJOrder]:
    stmt = select(CJOrder).order_by(CJOrder.id.desc())
    if status:
        stmt = stmt.where(CJOrder.status == status)
    result = await session.execute(stmt.offset(offset).limit(limit))
    return list(result.scalars().all())


async def get_cj_order_for_ebay_order(
    session: AsyncSession, order_ref: str
) -> CJOrder:
    order = await _find_order(session, order_ref)
    if order is None:
        raise CJOrderError(f"order not found: {order_ref}")
    cj_order = await _get_cj_order_by_order_id(session, order.id)
    if cj_order is None:
        raise CJOrderError(f"cj order not found for: {order_ref}")
    return cj_order


# ─────────────────────────────────────
# 内部ロジック
# ─────────────────────────────────────
class _UnmappedSkuError(Exception):
    pass


async def _fetch_placeable(session: AsyncSession, limit: int) -> list[CJOrder]:
    result = await session.execute(
        select(CJOrder)
        .where(CJOrder.cj_order_id.is_(None))
        .where(CJOrder.status.in_(_RETRYABLE_STATUSES))
        .where(CJOrder.attempts < settings.cj_order_max_attempts)
        .order_by(CJOrder.id)
        .limit(limit)
    )
    return list(result.scalars().all())


async def _fetch_syncable(session: AsyncSession, limit: int) -> list[CJOrder]:
    result = await session.execute(
        select(CJOrder)
        .where(CJOrder.cj_order_id.is_not(None))
        .where(CJOrder.status.in_(_SYNCABLE_STATUSES))
        .order_by(CJOrder.last_synced_at.asc().nulls_first())
        .limit(limit)
    )
    return list(result.scalars().all())


async def _get_cj_order_by_order_id(
    session: AsyncSession, order_id: int
) -> CJOrder | None:
    result = await session.execute(
        select(CJOrder).where(CJOrder.order_id == order_id)
    )
    return result.scalar_one_or_none()


async def _find_order(session: AsyncSession, order_ref: str) -> Order | None:
    if order_ref.isdigit():
        order = await session.get(Order, int(order_ref))
        if order is not None:
            return order
    result = await session.execute(
        select(Order).where(Order.ebay_order_id == order_ref)
    )
    return result.scalar_one_or_none()


async def _build_products(
    session: AsyncSession, order: Order
) -> tuple[list[dict[str, Any]], list[str]]:
    """注文明細を CJ の products（vid + quantity）に変換する。

    OrderItem があれば全明細、無ければ Order の primary SKU 1件を使う。
    1つでも解決できない SKU があれば発注しない（部分発注は事故のもと）。
    """
    items_result = await session.execute(
        select(OrderItem).where(OrderItem.order_id == order.id)
    )
    items = [(i.sku, i.quantity) for i in items_result.scalars().all() if i.sku]
    if not items:
        items = [(order.sku, order.quantity)]

    products: list[dict[str, Any]] = []
    notes: list[str] = []
    for sku, quantity in items:
        mapping = await get_mapping(session, sku)
        if mapping is None:
            raise _UnmappedSkuError(f"no CJ mapping for sku={sku}")
        vid = await resolve_variant_id(session, mapping)
        if not vid:
            raise _UnmappedSkuError(
                f"no CJ variant for sku={sku} cj_pid={mapping.cj_product_id}"
            )
        products.append({"vid": vid, "quantity": max(1, int(quantity or 1))})
        notes.append(f"{sku}->{mapping.cj_product_id}/{vid}")

    return products, notes


async def _resolve_shipping(
    session: AsyncSession, order: Order
) -> tuple[str | None, dict[str, Any]]:
    """配送先は fulfillment task の正規化済み住所を優先、無ければ raw_payload。"""
    task_result = await session.execute(
        select(FulfillmentTask).where(FulfillmentTask.order_id == order.id)
    )
    task = task_result.scalar_one_or_none()
    if task is not None and task.buyer_address_json:
        return task.buyer_name, dict(task.buyer_address_json)

    raw = order.raw_payload if isinstance(order.raw_payload, dict) else {}
    return _extract_shipping_address(raw)


def _extract_shipping_address(raw: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
    fs = raw.get("fulfillmentStartInstructions")
    if not isinstance(fs, list) or not fs:
        return None, {}

    ship_to = ((fs[0] or {}).get("shippingStep") or {}).get("shipTo") or {}
    contact = ship_to.get("contactAddress") or {}
    phone = (ship_to.get("primaryPhone") or {}).get("phoneNumber")

    return ship_to.get("fullName"), {
        "country": contact.get("countryCode"),
        "state": contact.get("stateOrProvince"),
        "city": contact.get("city"),
        "postal_code": contact.get("postalCode"),
        "address_line1": contact.get("addressLine1"),
        "address_line2": contact.get("addressLine2"),
        "phone": phone,
    }


def _validate_address(address: dict[str, Any]) -> list[str]:
    required = ("country", "city", "address_line1")
    return [key for key in required if not (address.get(key) or "").strip()]


def _build_order_payload(
    *,
    order: Order,
    buyer_name: str,
    address: dict[str, Any],
    products: list[dict[str, Any]],
) -> dict[str, Any]:
    """CJ createOrderV2 の payload。空の任意項目は送らない。"""
    payload: dict[str, Any] = {
        "orderNumber": order.ebay_order_id,
        "shippingCountryCode": (address.get("country") or "").upper(),
        "shippingCity": address.get("city"),
        "shippingAddress": address.get("address_line1"),
        "shippingCustomerName": buyer_name,
        "fromCountryCode": settings.cj_order_from_country_code,
        "payType": settings.cj_order_pay_type,
        "products": products,
    }

    optional = {
        "shippingZip": address.get("postal_code"),
        "shippingProvince": address.get("state"),
        "shippingAddress2": address.get("address_line2"),
        "shippingPhone": address.get("phone"),
        "logisticName": settings.cj_order_logistic_name,
        "remark": settings.cj_order_remark,
    }
    for key, value in optional.items():
        if value:
            payload[key] = value

    return payload


def _map_cj_status(raw_status: str, tracking: str | None) -> str:
    normalized = (raw_status or "").strip().upper().replace("-", "_")

    if "CANCEL" in normalized:
        return CJOrderStatus.CANCELED.value
    if "DELIVERED" in normalized:
        return CJOrderStatus.DELIVERED.value
    if normalized in ("SHIPPED", "SHIPPING", "IN_TRANSIT", "FULFILLED"):
        return CJOrderStatus.SHIPPED.value
    if tracking:
        return CJOrderStatus.SHIPPED.value
    return CJOrderStatus.CREATED.value


async def _fail(
    session: AsyncSession,
    cj_order: CJOrder,
    message: str,
    *,
    status: str = CJOrderStatus.FAILED.value,
) -> dict[str, Any]:
    cj_order.status = status
    cj_order.error_message = message[:1000]
    await session.commit()
    await session.refresh(cj_order)
    logger.warning(
        "cj order %s ebay_order_id=%s attempts=%d reason=%s",
        status, cj_order.ebay_order_id, cj_order.attempts, message,
    )
    return _result(cj_order, error=message)


def _result(
    cj_order: CJOrder,
    *,
    skipped: bool = False,
    reason: str | None = None,
    dry_run: bool = False,
    error: str | None = None,
    tracking_pushed: bool = False,
) -> dict[str, Any]:
    return {
        "success": cj_order.status
        in (
            CJOrderStatus.CREATED.value,
            CJOrderStatus.SHIPPED.value,
            CJOrderStatus.DELIVERED.value,
            CJOrderStatus.DRY_RUN.value,
        ),
        "skipped": skipped,
        "reason": reason,
        "dry_run": dry_run or cj_order.dry_run,
        "ebay_order_id": cj_order.ebay_order_id,
        "cj_order_id": cj_order.cj_order_id,
        "cj_order_num": cj_order.cj_order_num,
        "status": cj_order.status,
        "cj_status": cj_order.cj_status,
        "tracking_number": cj_order.tracking_number,
        "tracking_pushed": tracking_pushed,
        "attempts": cj_order.attempts,
        "request_payload": cj_order.request_payload,
        "error": error or cj_order.error_message,
    }
