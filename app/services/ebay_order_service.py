"""eBay Fulfillment API から注文を取得して DB に同期する。

incremental sync:
  - 前回 sync 時点の last_modified_date を Redis に保存
  - 次回は filter=lastmodifieddate:[<since>..] で差分のみ取得

dedupe:
  - ebay_order_id UNIQUE 制約 + SELECT-based upsert

atomic:
  - 1 注文単位で transaction（Order + OrderItem を一括 commit）
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Order, OrderItem
from app.services.cache_service import get_cached, set_cached
from app.services.ebay_auth_service import get_access_token
from app.services.ebay_listing_service import ListingError, _ebay_request

logger = logging.getLogger(__name__)


SYNC_STATE_KEY = "ebay_orders:last_modified_at"
SYNC_STATE_TTL = 60 * 60 * 24 * 365  # 1年（実質永続）
DEFAULT_PAGE_LIMIT = 50
MAX_PAGES = 20  # 1回の sync で最大 1000 件


# ── state（Redis）─────────────────
async def _load_last_modified() -> datetime | None:
    cached = await get_cached(SYNC_STATE_KEY)
    if not isinstance(cached, dict):
        return None
    iso = cached.get("iso")
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


async def _save_last_modified(dt: datetime) -> None:
    await set_cached(
        SYNC_STATE_KEY,
        {"iso": dt.astimezone(timezone.utc).isoformat()},
        SYNC_STATE_TTL,
    )


# ── eBay API ────────────────────
def _format_iso_for_ebay(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


async def fetch_orders_page(
    token: str,
    *,
    last_modified_after: datetime | None = None,
    limit: int = DEFAULT_PAGE_LIMIT,
    offset: int = 0,
) -> dict[str, Any]:
    """eBay Fulfillment API: GET /sell/fulfillment/v1/order"""
    params: dict[str, str] = {"limit": str(limit), "offset": str(offset)}
    if last_modified_after:
        iso = _format_iso_for_ebay(last_modified_after)
        params["filter"] = f"lastmodifieddate:[{iso}..]"
    qs = urlencode(params)
    return await _ebay_request(
        "GET",
        f"/sell/fulfillment/v1/order?{qs}",
        token=token,
    )


async def fetch_order_detail(token: str, ebay_order_id: str) -> dict[str, Any]:
    return await _ebay_request(
        "GET",
        f"/sell/fulfillment/v1/order/{ebay_order_id}",
        token=token,
    )


# ── parser ──────────────────────
def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _extract_total_price(order: dict[str, Any]) -> tuple[str, str]:
    total = (order.get("pricingSummary") or {}).get("total") or {}
    return (
        str(total.get("value") or "0"),
        str(total.get("currency") or "USD"),
    )


def _extract_country(order: dict[str, Any]) -> str | None:
    instructions = order.get("fulfillmentStartInstructions") or []
    for inst in instructions:
        addr = ((inst.get("shippingStep") or {}).get("shipTo") or {}).get(
            "contactAddress"
        ) or {}
        cc = addr.get("countryCode")
        if cc:
            return cc
    return None


def _extract_tracking(order: dict[str, Any]) -> str | None:
    fulfillments = order.get("shippingFulfillments") or []
    for f in fulfillments:
        tn = f.get("trackingNumber")
        if tn:
            return tn
    return None


def _extract_listing_id(order: dict[str, Any]) -> str | None:
    items = order.get("lineItems") or []
    for it in items:
        lid = it.get("legacyItemId") or it.get("listingMarketplaceId")
        if lid:
            return str(lid)
    return None


# ── DB upsert ───────────────────
async def _upsert_order(
    session: AsyncSession, raw: dict[str, Any]
) -> tuple[str, str]:
    """1注文をupsert。返り値: (action, ebay_order_id)
    action: imported / updated / skipped"""
    ebay_order_id = str(raw.get("orderId") or raw.get("legacyOrderId") or "")
    if not ebay_order_id:
        return ("skipped", "")

    total, currency = _extract_total_price(raw)
    line_items = raw.get("lineItems") or []
    first_sku = ""
    first_qty = 1
    if line_items:
        first_sku = str(line_items[0].get("sku") or "")
        first_qty = int(line_items[0].get("quantity") or 1)

    fields: dict[str, Any] = {
        "ebay_order_id": ebay_order_id,
        "sku": first_sku,
        "quantity": first_qty,
        "sold_price": total,
        "sold_currency": currency,
        "buyer_country": _extract_country(raw),
        "buyer_username": (raw.get("buyer") or {}).get("username"),
        "ebay_listing_id": _extract_listing_id(raw),
        "payment_status": raw.get("orderPaymentStatus"),
        "fulfillment_status": raw.get("orderFulfillmentStatus"),
        "tracking_number": _extract_tracking(raw),
        "ebay_created_at": _parse_dt(raw.get("creationDate")),
        "last_modified_date": _parse_dt(raw.get("lastModifiedDate")),
        "raw_payload": raw,
    }

    result = await session.execute(
        select(Order).where(Order.ebay_order_id == ebay_order_id)
    )
    existing = result.scalar_one_or_none()

    if existing is None:
        order = Order(**fields)
        session.add(order)
        await session.flush()
        action = "imported"
    else:
        prev_modified = existing.last_modified_date
        new_modified = fields["last_modified_date"]
        if prev_modified and new_modified and new_modified <= prev_modified:
            return ("skipped", ebay_order_id)
        for k, v in fields.items():
            if k == "ebay_order_id":
                continue
            setattr(existing, k, v)
        order = existing
        action = "updated"

    await session.execute(
        delete(OrderItem).where(OrderItem.order_id == order.id)
    )
    for item in line_items:
        cost = (item.get("lineItemCost") or {})
        session.add(OrderItem(
            order_id=order.id,
            sku=str(item.get("sku") or ""),
            title=item.get("title"),
            quantity=int(item.get("quantity") or 1),
            price=str(cost.get("value")) if cost.get("value") is not None else None,
            currency=str(cost.get("currency")) if cost.get("currency") else None,
        ))

    return (action, ebay_order_id)


# ── high-level: sync ─────────────
async def sync_orders(session: AsyncSession) -> dict[str, int]:
    """全注文の incremental sync。response: {imported, updated, skipped}"""
    counters = {"imported": 0, "updated": 0, "skipped": 0}

    try:
        token = await get_access_token()
    except Exception as e:
        logger.warning("sync_orders auth failed (graceful): %s", e)
        return counters

    since = await _load_last_modified()
    max_modified = since

    for page in range(MAX_PAGES):
        offset = page * DEFAULT_PAGE_LIMIT
        try:
            data = await fetch_orders_page(
                token,
                last_modified_after=since,
                limit=DEFAULT_PAGE_LIMIT,
                offset=offset,
            )
        except ListingError as e:
            logger.warning(
                "sync_orders partial failure page=%d err=%s", page, e
            )
            break
        except Exception as e:
            logger.warning(
                "sync_orders unexpected error page=%d err=%s", page, e
            )
            break

        orders = data.get("orders") or []
        if not orders:
            break

        for raw in orders:
            try:
                action, oid = await _upsert_order(session, raw)
                counters[action] = counters.get(action, 0) + 1
                if action == "imported":
                    logger.info("new order imported ebay_order_id=%s", oid)
                modified = _parse_dt(raw.get("lastModifiedDate"))
                if modified and (max_modified is None or modified > max_modified):
                    max_modified = modified
                await session.commit()
            except Exception as e:
                logger.warning(
                    "order upsert failed (continue) raw=%s err=%s",
                    raw.get("orderId"), e,
                )
                await session.rollback()

        if len(orders) < DEFAULT_PAGE_LIMIT:
            break

    if max_modified is not None and max_modified != since:
        await _save_last_modified(max_modified)

    logger.info(
        "orders synced imported=%d updated=%d skipped=%d",
        counters["imported"], counters["updated"], counters["skipped"],
    )
    return counters
