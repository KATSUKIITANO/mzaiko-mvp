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
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.database import AsyncSessionLocal
from app.db.models import (
    CJOrder,
    CJOrderStatus,
    FulfillmentTask,
    Listing,
    Order,
    OrderItem,
    OrderStatus,
    Product,
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
from app.services.customs_risk_service import CustomsRisk, assess_customs_risk
from app.services.ebay_fulfillment_tracking_service import apply_tracking_to_order
from app.services.slack_service import SlackError, post_to_webhook

logger = logging.getLogger(__name__)

# 発注を再試行できる（＝まだ CJ に注文が無い）状態
_RETRYABLE_STATUSES = (
    CJOrderStatus.PENDING.value,
    CJOrderStatus.DRY_RUN.value,
    CJOrderStatus.UNMAPPED.value,
    CJOrderStatus.FAILED.value,
)

# 状態同期の対象（CJ 側でまだ動く可能性がある）
# RETURNED は人間の対応待ちなので、以後ポーリングしない
_SYNCABLE_STATUSES = (
    CJOrderStatus.CREATED.value,
    CJOrderStatus.SHIPPED.value,
)

_SUCCESS_STATUSES = (
    CJOrderStatus.CREATED.value,
    CJOrderStatus.SHIPPED.value,
    CJOrderStatus.DELIVERED.value,
    CJOrderStatus.DRY_RUN.value,
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
    """eBay 注文 1件を CJ に発注する（冪等 / 通関対策で小包分割）。

    通関リスクの高い商材（化粧品・日焼け止め等）を数量まとめて1小包で送ると
    「商用輸入」とみなされて返送されるため、customs_risk_service が決めた
    1小包あたりの上限個数で分割し、小包ごとに CJ 注文を作る。

    既に発注済みの小包はスキップするので、途中失敗しても再実行できる。
    force=True で失敗済み行の再試行上限を無視する。
    """
    effective_dry_run = (
        settings.cj_order_dry_run if dry_run is None else dry_run
    )

    existing = await _get_cj_orders_by_order_id(session, order.id)

    if existing and all(row.cj_order_id for row in existing.values()):
        logger.info(
            "cj order already placed ebay_order_id=%s parcels=%d (skip)",
            order.ebay_order_id, len(existing),
        )
        return _aggregate(
            order, list(existing.values()), skipped=True, reason="already_placed"
        )

    # ── 発注明細（SKU → CJ vid）
    try:
        items = await _build_items(session, order)
    except _UnmappedSkuError as e:
        row = await _ensure_row(session, order, 0, 1, None)
        await _fail(session, row, str(e), status=CJOrderStatus.UNMAPPED.value)
        return _aggregate(order, [row])

    # ── 通関リスクで小包に分割
    parcels, risk = _split_into_parcels(items)
    if len(parcels) > 1:
        logger.info(
            "cj order split for customs ebay_order_id=%s parcels=%d "
            "risk=%s keyword=%s max_units=%d",
            order.ebay_order_id, len(parcels), risk.level,
            risk.matched_keyword, risk.max_units_per_parcel,
        )

    # ── 配送先（全小包で共通）
    buyer_name, address = await _resolve_shipping(session, order)
    missing = _validate_address(address)
    if missing or not buyer_name:
        reason = (
            f"invalid shipping address: missing {','.join(missing)}"
            if missing
            else "invalid shipping address: missing name"
        )
        row = await _ensure_row(session, order, 0, len(parcels), risk.level)
        await _fail(session, row, reason)
        return _aggregate(order, [row])

    rows: list[CJOrder] = []
    for index, products in enumerate(parcels):
        row = existing.get(index) or await _ensure_row(
            session, order, index, len(parcels), risk.level
        )
        row.split_total = len(parcels)
        row.customs_risk_level = risk.level
        rows.append(row)

        if row.cj_order_id:
            continue

        if (
            not force
            and row.status == CJOrderStatus.FAILED.value
            and row.attempts >= settings.cj_order_max_attempts
        ):
            logger.warning(
                "cj parcel attempts exhausted ebay_order_id=%s split=%d attempts=%d",
                order.ebay_order_id, index, row.attempts,
            )
            continue

        await _place_one_parcel(
            session,
            order=order,
            row=row,
            products=products,
            buyer_name=buyer_name,
            address=address,
            parcel_count=len(parcels),
            dry_run=effective_dry_run,
        )

    if any(r.cj_order_id for r in rows) and order.order_status in (
        OrderStatus.DETECTED.value,
        OrderStatus.PROCESSING.value,
    ):
        order.order_status = OrderStatus.ORDERED.value
        await session.commit()

    return _aggregate(order, rows)


async def _place_one_parcel(
    session: AsyncSession,
    *,
    order: Order,
    row: CJOrder,
    products: list[dict[str, Any]],
    buyer_name: str,
    address: dict[str, Any],
    parcel_count: int,
    dry_run: bool,
) -> None:
    """小包1つ分を CJ に発注して row を更新する（例外は _fail に集約）。"""
    payload = _build_order_payload(
        order=order,
        buyer_name=buyer_name,
        address=address,
        products=products,
        split_index=row.split_index,
        parcel_count=parcel_count,
    )

    row.request_payload = payload
    row.quantity = sum(p["quantity"] for p in products)
    row.shipping_country = address.get("country")
    row.logistic_name = settings.cj_order_logistic_name or None
    row.dry_run = dry_run

    if dry_run:
        row.status = CJOrderStatus.DRY_RUN.value
        row.error_message = None
        await session.commit()
        logger.info(
            "cj order DRY_RUN ebay_order_id=%s split=%d/%d qty=%d",
            order.ebay_order_id, row.split_index + 1, parcel_count, row.quantity,
        )
        return

    row.attempts += 1
    await session.commit()

    try:
        created = await cj_create_order(payload)
    except CJError as e:
        await _fail(session, row, f"CJ API error: {e}")
        return
    except Exception as e:
        await _fail(session, row, f"unexpected error: {e}")
        return

    cj_order_id = extract_cj_order_id(created)
    if not cj_order_id:
        await _fail(
            session, row, f"CJ response without orderId: {str(created)[:300]}"
        )
        return

    row.cj_order_id = cj_order_id
    row.cj_order_num = extract_cj_order_num(created)
    row.status = CJOrderStatus.CREATED.value
    row.cj_status = str(created.get("orderStatus") or "") or None
    row.response_payload = created
    row.error_message = None
    row.placed_at = datetime.now(timezone.utc)
    await session.commit()

    logger.info(
        "cj order created ebay_order_id=%s split=%d/%d cj_order_id=%s qty=%d",
        order.ebay_order_id, row.split_index + 1, parcel_count,
        cj_order_id, row.quantity,
    )


async def _ensure_row(
    session: AsyncSession,
    order: Order,
    split_index: int,
    split_total: int,
    risk_level: str | None,
) -> CJOrder:
    """(order_id, split_index) の行を取得、無ければ作る。

    unmapped / 住所不備で失敗した注文をワーカーが再試行したとき、
    既存行があるのに作り直して UNIQUE 制約で落ちるのを防ぐ。
    """
    result = await session.execute(
        select(CJOrder)
        .where(CJOrder.order_id == order.id)
        .where(CJOrder.split_index == split_index)
    )
    row = result.scalar_one_or_none()

    if row is None:
        row = CJOrder(
            order_id=order.id,
            ebay_order_id=order.ebay_order_id,
            sku=order.sku,
            quantity=order.quantity,
            shipping_country=order.buyer_country,
            status=CJOrderStatus.PENDING.value,
            split_index=split_index,
        )
        session.add(row)

    row.split_total = split_total
    if risk_level:
        row.customs_risk_level = risk_level

    await session.commit()
    await session.refresh(row)
    return row


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
        return _parcel_result(cj_order, skipped=True, reason="not_placed")

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
        return _parcel_result(cj_order, skipped=True, reason="detail_failed")

    raw_status = str(detail.get("orderStatus") or detail.get("status") or "")
    tracking = extract_tracking_number(detail)
    local_status = _map_cj_status(raw_status, tracking)
    previous_status = cj_order.status

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

    # 返送は「届かない」確定なので、tracking を eBay に上げず人間に知らせる
    if local_status == CJOrderStatus.RETURNED.value:
        if previous_status != CJOrderStatus.RETURNED.value:
            logger.warning(
                "cj order RETURNED ebay_order_id=%s cj_order_id=%s cj_status=%s "
                "sku=%s risk=%s",
                cj_order.ebay_order_id, cj_order.cj_order_id, raw_status,
                cj_order.sku, cj_order.customs_risk_level,
            )
            await _alert_returned(session, cj_order)
        await session.refresh(cj_order)
        return _parcel_result(cj_order)

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
    return _parcel_result(cj_order, tracking_pushed=tracking_pushed)


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
                ebay_order_id = cj_order.ebay_order_id
                try:
                    result = await place_cj_order(session, order)
                except Exception as e:
                    # flush 失敗でセッションが壊れていることがあるので必ず戻す
                    await session.rollback()
                    logger.warning(
                        "cj order place crashed (graceful) ebay_order_id=%s err=%s",
                        ebay_order_id, e,
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
                cj_order_id = cj_order.cj_order_id
                try:
                    result = await sync_cj_order(session, cj_order)
                except Exception as e:
                    await session.rollback()
                    logger.warning(
                        "cj order sync crashed (graceful) cj_order_id=%s err=%s",
                        cj_order_id, e,
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


async def get_cj_orders_for_ebay_order(
    session: AsyncSession, order_ref: str
) -> list[CJOrder]:
    """eBay 注文に紐づく CJ 発注（分割時は小包ごとに複数行）。"""
    order = await _find_order(session, order_ref)
    if order is None:
        raise CJOrderError(f"order not found: {order_ref}")
    rows = await _get_cj_orders_by_order_id(session, order.id)
    if not rows:
        raise CJOrderError(f"cj order not found for: {order_ref}")
    return [rows[i] for i in sorted(rows)]


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


async def _get_cj_orders_by_order_id(
    session: AsyncSession, order_id: int
) -> dict[int, CJOrder]:
    """split_index をキーにした CJ 発注行。"""
    result = await session.execute(
        select(CJOrder)
        .where(CJOrder.order_id == order_id)
        .order_by(CJOrder.split_index)
    )
    return {row.split_index: row for row in result.scalars().all()}


async def _find_order(session: AsyncSession, order_ref: str) -> Order | None:
    if order_ref.isdigit():
        order = await session.get(Order, int(order_ref))
        if order is not None:
            return order
    result = await session.execute(
        select(Order).where(Order.ebay_order_id == order_ref)
    )
    return result.scalar_one_or_none()


async def _build_items(
    session: AsyncSession, order: Order
) -> list[dict[str, Any]]:
    """注文明細を CJ 発注単位（vid + quantity + 通関判定用の title）に変換する。

    OrderItem があれば全明細、無ければ Order の primary SKU 1件を使う。
    1つでも解決できない SKU があれば発注しない（部分発注は事故のもと）。
    """
    items_result = await session.execute(
        select(OrderItem).where(OrderItem.order_id == order.id)
    )
    rows = [
        (i.sku, i.quantity, i.title)
        for i in items_result.scalars().all()
        if i.sku
    ]
    if not rows:
        rows = [(order.sku, order.quantity, None)]

    out: list[dict[str, Any]] = []
    for sku, quantity, title in rows:
        mapping = await get_mapping(session, sku)
        if mapping is None:
            raise _UnmappedSkuError(f"no CJ mapping for sku={sku}")
        vid = await resolve_variant_id(session, mapping)
        if not vid:
            raise _UnmappedSkuError(
                f"no CJ variant for sku={sku} cj_pid={mapping.cj_product_id}"
            )
        out.append({
            "vid": vid,
            "quantity": max(1, int(quantity or 1)),
            "sku": sku,
            "title": title or await _lookup_title(session, sku),
        })

    return out


async def _lookup_title(session: AsyncSession, sku: str) -> str:
    """通関リスク判定用のタイトルを listing → product の順に引く。"""
    listing_q = await session.execute(select(Listing).where(Listing.sku == sku))
    listing = listing_q.scalars().first()
    if listing is not None:
        if listing.title:
            return listing.title
        if listing.product_id:
            product = await session.get(Product, listing.product_id)
            if product is not None and product.title:
                return product.title

    product_q = await session.execute(
        select(Product).where(Product.ebay_sku == sku)
    )
    product = product_q.scalars().first()
    return product.title if product is not None and product.title else ""


def _split_into_parcels(
    items: list[dict[str, Any]],
) -> tuple[list[list[dict[str, Any]]], CustomsRisk]:
    """通関リスクに応じて明細を小包に割る。

    複数商材が混ざる場合は **最も厳しい上限**を小包全体に適用する
    （リスクの高い1点が同梱されていれば小包ごと止められるため）。
    """
    risk = _combined_risk(items)
    total_units = sum(i["quantity"] for i in items)
    max_units = risk.max_units_per_parcel

    # 分割しすぎの安全弁（配送料が嵩むため）
    cap = max(1, settings.customs_max_split_parcels)
    if total_units > max_units * cap:
        adjusted = -(-total_units // cap)  # ceil
        logger.warning(
            "customs split capped parcels=%d max_units=%d → %d (total=%d)",
            cap, max_units, adjusted, total_units,
        )
        max_units = adjusted
        risk = replace(risk, max_units_per_parcel=max_units)

    if total_units <= max_units:
        return [[{"vid": i["vid"], "quantity": i["quantity"]} for i in items]], risk

    # 1個ずつに展開してから上限で詰め直す（商材をまたいで均等に割る）
    units: list[str] = []
    for item in items:
        units.extend([item["vid"]] * item["quantity"])

    parcels: list[list[dict[str, Any]]] = []
    for start in range(0, len(units), max_units):
        chunk = units[start:start + max_units]
        grouped: dict[str, int] = {}
        for vid in chunk:
            grouped[vid] = grouped.get(vid, 0) + 1
        parcels.append(
            [{"vid": vid, "quantity": qty} for vid, qty in grouped.items()]
        )

    return parcels, risk


def _combined_risk(items: list[dict[str, Any]]) -> CustomsRisk:
    """同梱される全商材のうち最も厳しい判定を採用する。"""
    strictest: CustomsRisk | None = None
    for item in items:
        risk = assess_customs_risk(title=item.get("title") or "", sku=item["sku"])
        if (
            strictest is None
            or risk.max_units_per_parcel < strictest.max_units_per_parcel
        ):
            strictest = risk
    return strictest or CustomsRisk(
        max_units_per_parcel=max(1, settings.customs_default_max_units_per_parcel)
    )


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
    split_index: int = 0,
    parcel_count: int = 1,
) -> dict[str, Any]:
    """CJ createOrderV2 の payload。空の任意項目は送らない。

    分割時は orderNumber が衝突しないよう連番を付ける
    （CJ は同一 orderNumber の重複注文を受け付けない）。
    """
    order_number = order.ebay_order_id
    if parcel_count > 1:
        order_number = f"{order_number}-{split_index + 1}"

    payload: dict[str, Any] = {
        "orderNumber": order_number,
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


# 返送・通関留置を示す語（CJ / 各国郵便のステータス文字列に現れる）
_RETURN_MARKERS = (
    "RETURN",          # RETURNED / RETURN_TO_SENDER / Item Returned from Customs
    "REJECT",
    "REFUSE",
    "UNDELIVERABLE",
    "DETAIN",
    "SEIZED",
    "DISPOSE",
    "DISCARD",
)


def _map_cj_status(raw_status: str, tracking: str | None) -> str:
    normalized = (raw_status or "").strip().upper().replace("-", "_")

    # 返送は最優先で判定する（shipped のまま放置すると気づけない）
    if any(marker in normalized for marker in _RETURN_MARKERS):
        return CJOrderStatus.RETURNED.value
    if "CANCEL" in normalized:
        return CJOrderStatus.CANCELED.value
    if "DELIVERED" in normalized:
        return CJOrderStatus.DELIVERED.value
    if normalized in ("SHIPPED", "SHIPPING", "IN_TRANSIT", "FULFILLED"):
        return CJOrderStatus.SHIPPED.value
    if tracking:
        return CJOrderStatus.SHIPPED.value
    return CJOrderStatus.CREATED.value


async def _alert_returned(session: AsyncSession, row: CJOrder) -> None:
    """返送を検知したら Slack に飛ばす（通信失敗は握り潰す）。

    同じ SKU の他の発注も巻き添えで返送される可能性が高いので、
    同一 SKU で進行中の件数も添えて知らせる。
    """
    if not (settings.customs_alert_on_returned and settings.slack_notifications_enabled):
        return

    siblings = 0
    if row.sku:
        result = await session.execute(
            select(func.count())
            .select_from(CJOrder)
            .where(CJOrder.sku == row.sku)
            .where(CJOrder.id != row.id)
            .where(CJOrder.status.in_(_SYNCABLE_STATUSES))
        )
        siblings = int(result.scalar() or 0)

    text = (
        ":rotating_light: *CJ発注が返送されました*\n"
        f"• eBay order: `{row.ebay_order_id}`\n"
        f"• SKU: `{row.sku or '-'}`\n"
        f"• CJ order: `{row.cj_order_id or '-'}`"
        f"（小包 {row.split_index + 1}/{row.split_total}）\n"
        f"• CJ status: `{row.cj_status or '-'}`\n"
        f"• 通関リスク: `{row.customs_risk_level or 'unknown'}`\n"
        f"• 同一SKUで配送中の他の発注: *{siblings}件*\n"
        "→ バイヤーへの連絡と返金/再送の判断、CJへのdispute提出が必要です。"
    )

    try:
        await post_to_webhook(text)
        logger.info(
            "returned alert sent ebay_order_id=%s sku=%s siblings=%d",
            row.ebay_order_id, row.sku, siblings,
        )
    except SlackError as e:
        logger.warning("returned alert failed (graceful): %s", e)
    except Exception as e:
        logger.warning("returned alert unexpected error (graceful): %s", e)


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
    return _parcel_result(cj_order, error=message)


def _parcel_result(
    cj_order: CJOrder,
    *,
    skipped: bool = False,
    reason: str | None = None,
    error: str | None = None,
    tracking_pushed: bool = False,
) -> dict[str, Any]:
    """小包1つ分の結果。"""
    return {
        "success": cj_order.status in _SUCCESS_STATUSES,
        "skipped": skipped,
        "reason": reason,
        "split_index": cj_order.split_index,
        "split_total": cj_order.split_total,
        "dry_run": cj_order.dry_run,
        "ebay_order_id": cj_order.ebay_order_id,
        "cj_order_id": cj_order.cj_order_id,
        "cj_order_num": cj_order.cj_order_num,
        "status": cj_order.status,
        "cj_status": cj_order.cj_status,
        "quantity": cj_order.quantity,
        "customs_risk_level": cj_order.customs_risk_level,
        "tracking_number": cj_order.tracking_number,
        "tracking_pushed": tracking_pushed,
        "attempts": cj_order.attempts,
        "request_payload": cj_order.request_payload,
        "error": error or cj_order.error_message,
    }


def _aggregate(
    order: Order,
    rows: list[CJOrder],
    *,
    skipped: bool = False,
    reason: str | None = None,
) -> dict[str, Any]:
    """eBay 注文1件分（＝全小包）の結果。"""
    parcels = [_parcel_result(r) for r in rows]
    errors = [p["error"] for p in parcels if p["error"]]

    return {
        "success": bool(parcels) and all(p["success"] for p in parcels),
        "skipped": skipped,
        "reason": reason,
        "dry_run": any(p["dry_run"] for p in parcels),
        "ebay_order_id": order.ebay_order_id,
        "status": _representative_status(rows),
        "parcel_count": len(parcels),
        "split": len(parcels) > 1,
        "customs_risk_level": rows[0].customs_risk_level if rows else None,
        "total_quantity": sum(r.quantity for r in rows),
        "cj_order_ids": [r.cj_order_id for r in rows if r.cj_order_id],
        "parcels": parcels,
        "error": " | ".join(errors) if errors else None,
    }


def _representative_status(rows: list[CJOrder]) -> str:
    """全小包を1つのステータスに畳む（悪い方を優先して見落とさない）。"""
    if not rows:
        return CJOrderStatus.PENDING.value
    statuses = {r.status for r in rows}
    for worst in (
        CJOrderStatus.RETURNED.value,
        CJOrderStatus.FAILED.value,
        CJOrderStatus.UNMAPPED.value,
        CJOrderStatus.CANCELED.value,
        CJOrderStatus.PENDING.value,
        CJOrderStatus.DRY_RUN.value,
        CJOrderStatus.CREATED.value,
        CJOrderStatus.SHIPPED.value,
    ):
        if worst in statuses:
            return worst
    return rows[0].status
