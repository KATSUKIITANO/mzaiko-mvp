"""Fulfillment Queue Service（配送見積 + country risk 統合版）。

profit 計算式:
  expected_profit       = ebay_sold − amazon_price − fee − shipping
                          − fx_buffer − refund_buffer
  risk_buffer_jpy       = ebay_sold × buffer_adjust(risk_level) / 100
  risk_adjusted_profit  = expected_profit − risk_buffer_jpy

risk gate:
  blocked   → task_status = 'failed'
  high      → task_status = 'reviewing' + manual review note
  medium    → task_status = 'pending' (通常)
  low       → task_status = 'pending' (通常)

profit gate:
  expected_profit       < MIN_PROFIT_YEN → 'failed' (auto)
  risk_adjusted_profit  < MIN_PROFIT_YEN → WARNING のみ（status 維持）
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import (
    CountryRiskLevel,
    FulfillmentTask,
    Listing,
    Order,
    Product,
    TaskStatus,
)
from app.schemas.fulfillment import BuyerAddress, ProfitBreakdown
from app.services.country_risk_service import (
    calc_risk_buffer_jpy,
    compute_country_risk_score,
    get_country_risk_level,
)
from app.services.shipping_service import (
    ShippingEstimate,
    estimate_with_defaults,
)

logger = logging.getLogger(__name__)


class FulfillmentError(Exception):
    pass


class TaskNotFoundError(FulfillmentError):
    pass


class TaskTransitionError(FulfillmentError):
    pass


# ─────────────────────────────────────
# task 生成
# ─────────────────────────────────────
async def create_task_from_order(
    order: Order,
    listing: Listing | None,
    session: AsyncSession,
) -> FulfillmentTask:
    existing_q = await session.execute(
        select(FulfillmentTask).where(FulfillmentTask.order_id == order.id)
    )
    existing = existing_q.scalar_one_or_none()
    if existing is not None:
        logger.debug(
            "fulfillment task already exists order_id=%s task_id=%d",
            order.ebay_order_id, existing.id,
        )
        return existing

    product: Product | None = None
    if listing and listing.product_id:
        product = await session.get(Product, listing.product_id)

    raw = order.raw_payload or {}
    buyer_name, buyer_address = _normalize_buyer(raw if isinstance(raw, dict) else {})

    amazon_price = product.price if product else 0
    ebay_sold_jpy = _convert_sold_to_jpy(order.sold_price, order.sold_currency)

    country = order.buyer_country or (buyer_address.get("country") if buyer_address else None)

    # ── Country Risk
    risk_level = get_country_risk_level(country)
    risk_score = compute_country_risk_score(country)
    risk_buffer_jpy = calc_risk_buffer_jpy(ebay_sold_jpy, risk_level)

    # ── 配送見積
    estimate = estimate_with_defaults(
        country=country,
        weight_gram=product.package_weight_gram if product else None,
        length_cm=product.package_length_cm if product else None,
        width_cm=product.package_width_cm if product else None,
        height_cm=product.package_height_cm if product else None,
    )

    # ── 利益計算
    expected_profit, breakdown = _compute_profit(
        ebay_sold_jpy=ebay_sold_jpy,
        amazon_price=amazon_price,
        estimated_shipping=estimate.estimated_cost_jpy,
    )
    risk_adjusted_profit = expected_profit - risk_buffer_jpy

    task = FulfillmentTask(
        order_id=order.id,
        listing_id=listing.id if listing else None,
        sku=order.sku,
        amazon_url=product.url if product else None,
        amazon_price=amazon_price,
        ebay_sold_price=ebay_sold_jpy,
        expected_profit=expected_profit,
        risk_adjusted_profit=risk_adjusted_profit,
        estimated_shipping_cost=estimate.estimated_cost_jpy,
        shipping_zone=estimate.zone,
        shipping_method=estimate.method,
        country_risk_level=risk_level,
        country_risk_score=risk_score,
        buyer_name=buyer_name,
        buyer_address_json=buyer_address,
        buyer_country=country,
        task_status=TaskStatus.PENDING.value,
    )

    initial_notes: list[str] = []
    for n in estimate.notes:
        initial_notes.append(_format_note(f"[shipping] {n}"))

    # ── Country Risk Gate
    if risk_level == CountryRiskLevel.BLOCKED.value:
        task.task_status = TaskStatus.FAILED.value
        msg = f"blocked country={country}"
        initial_notes.append(_format_note(f"[auto-failed] {msg}"))
        logger.warning(
            "blocked country task auto-failed order_id=%s country=%s",
            order.ebay_order_id, country,
        )
    elif risk_level == CountryRiskLevel.HIGH.value:
        task.task_status = TaskStatus.REVIEWING.value
        msg = f"high-risk country={country} (manual review required)"
        initial_notes.append(_format_note(f"[manual-review] {msg}"))
        logger.warning(
            "high risk shipment country=%s order_id=%s status=reviewing",
            country, order.ebay_order_id,
        )

    # ── Profit Gate（expected_profit、blocked と重複時は最終 failed のまま）
    if (
        task.task_status not in (TaskStatus.FAILED.value,)
        and expected_profit < settings.min_profit_yen
    ):
        task.task_status = TaskStatus.FAILED.value
        initial_notes.append(_format_note(
            f"[auto-failed] expected_profit={expected_profit} JPY < "
            f"MIN_PROFIT_YEN={settings.min_profit_yen}"
        ))
        logger.warning(
            "low profit task auto-failed order_id=%s sku=%s expected=%d threshold=%d",
            order.ebay_order_id, order.sku, expected_profit, settings.min_profit_yen,
        )

    # ── risk_adjusted_profit warning（status 変更なし）
    if risk_adjusted_profit < settings.min_profit_yen:
        initial_notes.append(_format_note(
            f"[risk-warning] risk_adjusted_profit={risk_adjusted_profit} JPY < "
            f"MIN_PROFIT_YEN={settings.min_profit_yen}"
        ))
        logger.warning(
            "adjusted profit below threshold order_id=%s country=%s "
            "expected=%d adjusted=%d threshold=%d",
            order.ebay_order_id, country,
            expected_profit, risk_adjusted_profit, settings.min_profit_yen,
        )

    # ── address 不正
    if not buyer_address or not buyer_address.get("address_line1"):
        initial_notes.append(_format_note("[warning] invalid or missing buyer address"))
        logger.warning(
            "invalid address for task order_id=%s sku=%s",
            order.ebay_order_id, order.sku,
        )

    if listing is None:
        logger.warning(
            "missing listing for sku=%s order_id=%s",
            order.sku, order.ebay_order_id,
        )

    if not product:
        initial_notes.append(_format_note("[warning] product not linked, using default dimensions"))

    if initial_notes:
        task.notes = "\n".join(initial_notes)

    session.add(task)
    await session.commit()
    await session.refresh(task)

    logger.info(
        "task created id=%d order_id=%s sku=%s status=%s country=%s "
        "risk_level=%s risk_score=%d amazon=%d ebay_sold=%d "
        "shipping=%d fee=%d fx_buf=%d refund_buf=%d risk_buf=%d "
        "expected=%d risk_adjusted=%d",
        task.id, order.ebay_order_id, order.sku, task.task_status, country,
        risk_level, risk_score,
        amazon_price, ebay_sold_jpy,
        estimate.estimated_cost_jpy, breakdown["fee"],
        breakdown["fx_buffer"], breakdown["refund_buffer"], risk_buffer_jpy,
        expected_profit, risk_adjusted_profit,
    )
    return task


# ─────────────────────────────────────
# 配送再見積（risk_adjusted_profit も再計算）
# ─────────────────────────────────────
async def recalculate_shipping_for_task(
    task_id: int,
    session: AsyncSession,
) -> tuple[FulfillmentTask, ShippingEstimate]:
    task = await _get_task(task_id, session)

    listing: Listing | None = None
    product: Product | None = None
    if task.listing_id:
        listing = await session.get(Listing, task.listing_id)
    if listing and listing.product_id:
        product = await session.get(Product, listing.product_id)

    estimate = estimate_with_defaults(
        country=task.buyer_country,
        weight_gram=product.package_weight_gram if product else None,
        length_cm=product.package_length_cm if product else None,
        width_cm=product.package_width_cm if product else None,
        height_cm=product.package_height_cm if product else None,
    )

    old_cost = task.estimated_shipping_cost or 0
    old_profit = task.expected_profit
    old_adjusted = task.risk_adjusted_profit or 0

    expected_profit, _ = _compute_profit(
        ebay_sold_jpy=task.ebay_sold_price,
        amazon_price=task.amazon_price,
        estimated_shipping=estimate.estimated_cost_jpy,
    )

    # risk は変わらない前提だが、country が事後変更されている可能性も考慮して再計算
    risk_level = get_country_risk_level(task.buyer_country)
    risk_score = compute_country_risk_score(task.buyer_country)
    risk_buffer_jpy = calc_risk_buffer_jpy(task.ebay_sold_price, risk_level)
    risk_adjusted_profit = expected_profit - risk_buffer_jpy

    task.estimated_shipping_cost = estimate.estimated_cost_jpy
    task.shipping_zone = estimate.zone
    task.shipping_method = estimate.method
    task.expected_profit = expected_profit
    task.country_risk_level = risk_level
    task.country_risk_score = risk_score
    task.risk_adjusted_profit = risk_adjusted_profit

    note_parts = [
        f"old_cost={old_cost}→{estimate.estimated_cost_jpy}",
        f"old_profit={old_profit}→{expected_profit}",
        f"old_adjusted={old_adjusted}→{risk_adjusted_profit}",
    ]
    for n in estimate.notes:
        note_parts.append(f"[shipping] {n}")
    _append_note(task, " | ".join(note_parts), label="recalc")

    if expected_profit < settings.min_profit_yen:
        if task.task_status in (TaskStatus.PENDING.value, TaskStatus.REVIEWING.value):
            task.task_status = TaskStatus.FAILED.value
            _append_note(
                task,
                f"recalc auto-failed: profit={expected_profit} < {settings.min_profit_yen}",
                label="recalc-fail",
            )
            logger.warning(
                "recalc auto-failed task_id=%d profit=%d threshold=%d",
                task.id, expected_profit, settings.min_profit_yen,
            )
        else:
            _append_note(
                task,
                f"recalc warning: profit={expected_profit} below threshold "
                f"(status={task.task_status} unchanged)",
                label="recalc-warn",
            )

    if risk_adjusted_profit < settings.min_profit_yen:
        _append_note(
            task,
            f"risk_adjusted_profit={risk_adjusted_profit} below MIN_PROFIT_YEN",
            label="recalc-risk-warn",
        )
        logger.warning(
            "adjusted profit below threshold task_id=%d adjusted=%d",
            task.id, risk_adjusted_profit,
        )

    await session.commit()
    await session.refresh(task)

    logger.info(
        "shipping recalculated task_id=%d zone=%s method=%s old_cost=%d "
        "new_cost=%d old_profit=%d new_profit=%d new_adjusted=%d",
        task.id, estimate.zone, estimate.method or "NONE",
        old_cost, estimate.estimated_cost_jpy,
        old_profit, expected_profit, risk_adjusted_profit,
    )

    return task, estimate


# ─────────────────────────────────────
# 状態遷移
# ─────────────────────────────────────
_APPROVABLE_FROM = (TaskStatus.PENDING.value, TaskStatus.REVIEWING.value)
_FINAL_STATES = (TaskStatus.COMPLETED.value, TaskStatus.CANCELED.value)


async def approve_task(
    task_id: int,
    *,
    assigned_to: str | None,
    notes: str | None,
    session: AsyncSession,
) -> FulfillmentTask:
    task = await _get_task(task_id, session)
    if task.task_status not in _APPROVABLE_FROM:
        raise TaskTransitionError(
            f"approve は pending/reviewing のみ。現在={task.task_status}"
        )
    task.task_status = TaskStatus.APPROVED.value
    if assigned_to:
        task.assigned_to = assigned_to
    _append_note(task, notes, label="approved")
    await session.commit()
    await session.refresh(task)
    logger.info("task approved id=%d order_id=%d assigned_to=%s",
                task.id, task.order_id, assigned_to)

    await _place_cj_order_on_approve(task, session)
    return task


async def _place_cj_order_on_approve(
    task: FulfillmentTask,
    session: AsyncSession,
) -> None:
    """approve された注文を CJ に発注する（CJ_ORDER_ON_APPROVE=true のとき）。

    循環 import を避けるため関数内 import。発注失敗は approve を巻き戻さない。
    """
    if not settings.cj_order_on_approve:
        return

    from app.services.cj_order_service import place_cj_order_if_enabled

    order = await session.get(Order, task.order_id)
    if order is None:
        logger.warning("cj order skipped: order not found task_id=%d", task.id)
        return

    result = await place_cj_order_if_enabled(session, order, trigger="approve")
    if result is None:
        return

    _append_note(
        task,
        f"cj_order status={result.get('status')} "
        f"cj_order_id={result.get('cj_order_id') or '-'} "
        f"dry_run={result.get('dry_run')}"
        + (f" error={result.get('error')}" if result.get("error") else ""),
        label="cj-order",
    )
    await session.commit()


async def mark_purchased(
    task_id: int,
    *,
    notes: str | None,
    tracking_number: str | None,
    session: AsyncSession,
) -> FulfillmentTask:
    task = await _get_task(task_id, session)
    if task.task_status != TaskStatus.APPROVED.value:
        raise TaskTransitionError(
            f"mark-purchased は approved のみ。現在={task.task_status}"
        )
    task.task_status = TaskStatus.PURCHASED.value
    note_parts = []
    if notes:
        note_parts.append(notes)
    if tracking_number:
        note_parts.append(f"tracking={tracking_number}")
    _append_note(task, " | ".join(note_parts) or None, label="purchased")
    await session.commit()
    await session.refresh(task)
    logger.info("purchased id=%d order_id=%d", task.id, task.order_id)
    return task


async def mark_shipped(
    task_id: int,
    *,
    notes: str | None,
    tracking_number: str | None,
    session: AsyncSession,
) -> FulfillmentTask:
    task = await _get_task(task_id, session)
    if task.task_status != TaskStatus.PURCHASED.value:
        raise TaskTransitionError(
            f"mark-shipped は purchased のみ。現在={task.task_status}"
        )
    task.task_status = TaskStatus.SHIPPED.value
    note_parts = []
    if notes:
        note_parts.append(notes)
    if tracking_number:
        note_parts.append(f"tracking={tracking_number}")
    _append_note(task, " | ".join(note_parts) or None, label="shipped")
    await session.commit()
    await session.refresh(task)
    logger.info("shipped id=%d order_id=%d", task.id, task.order_id)
    return task


async def cancel_task(
    task_id: int,
    *,
    reason: str | None,
    session: AsyncSession,
) -> FulfillmentTask:
    task = await _get_task(task_id, session)
    if task.task_status in _FINAL_STATES:
        raise TaskTransitionError(
            f"cancel は最終状態(completed/canceled)からは不可。現在={task.task_status}"
        )
    task.task_status = TaskStatus.CANCELED.value
    _append_note(task, reason, label="canceled")
    await session.commit()
    await session.refresh(task)
    logger.info("task canceled id=%d order_id=%d reason=%s",
                task.id, task.order_id, reason)
    return task


# ─────────────────────────────────────
# 取得・一覧
# ─────────────────────────────────────
async def list_tasks(
    *,
    task_status: str | None,
    limit: int,
    offset: int,
    session: AsyncSession,
) -> list[FulfillmentTask]:
    stmt = select(FulfillmentTask).order_by(FulfillmentTask.id.desc())
    if task_status:
        stmt = stmt.where(FulfillmentTask.task_status == task_status)
    stmt = stmt.offset(offset).limit(limit)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def list_tasks_by_risk(
    *,
    risk_level: str | None,
    limit: int,
    offset: int,
    session: AsyncSession,
) -> list[FulfillmentTask]:
    stmt = select(FulfillmentTask).order_by(FulfillmentTask.id.desc())
    if risk_level:
        stmt = stmt.where(FulfillmentTask.country_risk_level == risk_level)
    stmt = stmt.offset(offset).limit(limit)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_task(task_id: int, session: AsyncSession) -> FulfillmentTask:
    return await _get_task(task_id, session)


async def _get_task(task_id: int, session: AsyncSession) -> FulfillmentTask:
    task = await session.get(FulfillmentTask, task_id)
    if task is None:
        raise TaskNotFoundError(f"task not found: id={task_id}")
    return task


# ─────────────────────────────────────
# UI helper
# ─────────────────────────────────────
def build_profit_breakdown(task: FulfillmentTask) -> ProfitBreakdown:
    fee = int(task.ebay_sold_price * settings.ebay_fee_percent)
    fx_buffer = int(task.ebay_sold_price * settings.fx_buffer_percent / 100)
    refund_buffer = int(task.ebay_sold_price * settings.refund_buffer_percent / 100)
    risk_buffer = calc_risk_buffer_jpy(
        task.ebay_sold_price, task.country_risk_level or "low"
    )
    return ProfitBreakdown(
        ebay_sold_jpy=task.ebay_sold_price,
        amazon_price_jpy=task.amazon_price,
        estimated_fee_jpy=fee,
        estimated_shipping_jpy=task.estimated_shipping_cost or 0,
        fx_buffer_jpy=fx_buffer,
        refund_buffer_jpy=refund_buffer,
        risk_buffer_jpy=risk_buffer,
        expected_profit_jpy=task.expected_profit,
        risk_adjusted_profit_jpy=task.risk_adjusted_profit or task.expected_profit,
    )


# ─────────────────────────────────────
# 内部ロジック
# ─────────────────────────────────────
def _compute_profit(
    *,
    ebay_sold_jpy: int,
    amazon_price: int,
    estimated_shipping: int,
) -> tuple[int, dict[str, int]]:
    fee = int(ebay_sold_jpy * settings.ebay_fee_percent)
    fx_buffer = int(ebay_sold_jpy * settings.fx_buffer_percent / 100)
    refund_buffer = int(ebay_sold_jpy * settings.refund_buffer_percent / 100)
    profit = (
        ebay_sold_jpy
        - amazon_price
        - fee
        - estimated_shipping
        - fx_buffer
        - refund_buffer
    )
    return profit, {
        "fee": fee,
        "shipping": estimated_shipping,
        "fx_buffer": fx_buffer,
        "refund_buffer": refund_buffer,
    }


def _normalize_buyer(raw: dict[str, Any]) -> tuple[str | None, dict | None]:
    fs = raw.get("fulfillmentStartInstructions")
    if not isinstance(fs, list) or not fs:
        return None, None

    first = fs[0] or {}
    ship_to = (first.get("shippingStep") or {}).get("shipTo") or {}
    contact = ship_to.get("contactAddress") or {}
    phone_obj = ship_to.get("primaryPhone") or {}

    name = ship_to.get("fullName")

    if not contact:
        return name, None

    normalized = {
        "country": contact.get("countryCode"),
        "state": contact.get("stateOrProvince"),
        "city": contact.get("city"),
        "postal_code": contact.get("postalCode"),
        "address_line1": contact.get("addressLine1"),
        "address_line2": contact.get("addressLine2"),
        "phone": phone_obj.get("phoneNumber"),
    }
    if not any(v for v in normalized.values()):
        return name, None
    return name, BuyerAddress(**normalized).model_dump()


def _convert_sold_to_jpy(price: str, currency: str) -> int:
    try:
        value = float(price)
    except (TypeError, ValueError):
        return 0
    if currency.upper() == "JPY":
        return int(value)
    return int(value * settings.fx_rate_usd_jpy)


def _format_note(note: str) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"[{ts}] {note}"


def _append_note(
    task: FulfillmentTask,
    note: str | None,
    *,
    label: str,
) -> None:
    if not note:
        new_line = _format_note(f"[{label}]")
    else:
        new_line = _format_note(f"[{label}] {note}")

    if task.notes:
        task.notes = task.notes + "\n" + new_line
    else:
        task.notes = new_line
