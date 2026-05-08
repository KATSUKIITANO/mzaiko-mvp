"""eBay sold listing sync worker。

scheduler から定期呼び出され、Fulfillment API の getOrders で
新規注文を検知 → DB に Order 行を作成 → 関連 listing を sold に更新 →
fulfillment_task を自動生成する。

設計:
  - since 計算: DB 最大の ebay_created_at - 1分（オーバーラップで取りこぼし防止）
                履歴ゼロなら now - SOLD_SYNC_LOOKBACK_HOURS
  - eBay API: GET /sell/fulfillment/v1/order with filter=creationdate:[since..]
  - ページング: limit=SOLD_SYNC_PAGE_LIMIT で next が無くなるまで
  - 並列度: Semaphore(SOLD_SYNC_CONCURRENCY)
  - 重複: ebay_order_id UNIQUE + 事前チェック
  - listing 紐付け: SKU で listings を検索、見つかれば listing_id を保存・SOLD 化
  - sold hook: fulfillment_service.create_task_from_order() で task 自動生成

future:
  - Amazon 自動発注 → fulfillment task の approve hook に挿入
  - 追跡番号同期 → task.shipped 時に eBay へ POST shipping_fulfillment
  - auto fulfill → fulfillment task の状態を全自動で進める
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.config import settings
from app.db.database import AsyncSessionLocal
from app.db.models import Listing, ListingStatus, Order, OrderStatus
from app.services.ebay_auth_service import get_access_token
from app.services.fulfillment_service import create_task_from_order

logger = logging.getLogger(__name__)


@dataclass
class SoldSyncMetrics:
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None
    orders_fetched: int = 0
    sold_detected: int = 0
    order_created: int = 0
    duplicate_skipped: int = 0
    unknown_sku: int = 0
    failed: int = 0

    @property
    def duration_seconds(self) -> float | None:
        if self.finished_at is None:
            return None
        return (self.finished_at - self.started_at).total_seconds()


_last_metrics: SoldSyncMetrics | None = None
_running_flag: bool = False
_run_lock: asyncio.Lock = asyncio.Lock()


class SoldSyncError(Exception):
    pass


def get_last_metrics() -> SoldSyncMetrics | None:
    return _last_metrics


def is_worker_running() -> bool:
    return _running_flag


async def run_sold_sync_worker() -> SoldSyncMetrics:
    global _last_metrics, _running_flag

    if not settings.sold_sync_enabled:
        logger.info("sold sync worker disabled (SOLD_SYNC_ENABLED=false)")
        m = SoldSyncMetrics()
        m.finished_at = m.started_at
        _last_metrics = m
        return m

    async with _run_lock:
        if _running_flag:
            logger.warning("sold sync worker already running, skip this tick")
            return _last_metrics or SoldSyncMetrics()
        _running_flag = True

    metrics = SoldSyncMetrics()
    try:
        since = await _determine_since()
        logger.info(
            "sold sync start since=%s interval=%dmin concurrency=%d dry_run=%s",
            since.isoformat(),
            settings.sold_sync_interval_minutes,
            settings.sold_sync_concurrency,
            settings.sold_sync_dry_run,
        )

        orders = await _fetch_all_orders(since)
        metrics.orders_fetched = len(orders)

        if not orders:
            metrics.finished_at = datetime.now(timezone.utc)
            logger.info(
                "sold sync complete duration=%.1fs no new orders",
                metrics.duration_seconds or 0,
            )
            _last_metrics = metrics
            return metrics

        sem = asyncio.Semaphore(settings.sold_sync_concurrency)
        tasks = [_process_order_with_metrics(o, sem, metrics) for o in orders]
        await asyncio.gather(*tasks, return_exceptions=True)

        metrics.finished_at = datetime.now(timezone.utc)
        logger.info(
            "sold sync complete duration=%.1fs fetched=%d detected=%d "
            "created=%d duplicate=%d unknown_sku=%d failed=%d",
            metrics.duration_seconds or 0,
            metrics.orders_fetched,
            metrics.sold_detected,
            metrics.order_created,
            metrics.duplicate_skipped,
            metrics.unknown_sku,
            metrics.failed,
        )
        _last_metrics = metrics
    except Exception:
        logger.exception("sold sync worker run aborted")
        if metrics.finished_at is None:
            metrics.finished_at = datetime.now(timezone.utc)
        _last_metrics = metrics
    finally:
        _running_flag = False

    return metrics


async def _determine_since() -> datetime:
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(func.max(Order.ebay_created_at)))
        last = result.scalar_one_or_none()

    if last is not None:
        return last - timedelta(minutes=1)
    return datetime.now(timezone.utc) - timedelta(
        hours=settings.sold_sync_lookback_hours
    )


@retry(
    stop=stop_after_attempt(max(1, settings.sold_sync_max_retries + 1)),
    wait=wait_exponential(multiplier=1, min=1, max=5),
    retry=retry_if_exception_type((httpx.HTTPError,)),
    reraise=True,
)
async def _ebay_get_orders(
    token: str,
    *,
    since: datetime,
    offset: int,
    limit: int,
) -> dict:
    url = f"{settings.ebay_api_base}/sell/fulfillment/v1/order"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    params = {
        "filter": f"creationdate:[{_format_ebay_date(since)}..]",
        "limit": limit,
        "offset": offset,
    }

    async with httpx.AsyncClient(
        timeout=settings.sold_sync_request_timeout
    ) as client:
        res = await client.get(url, headers=headers, params=params)

    if res.status_code != 200:
        raise SoldSyncError(
            f"eBay getOrders 失敗: status={res.status_code} body={res.text[:500]}"
        )

    return res.json()


async def _fetch_all_orders(since: datetime) -> list[dict]:
    token = await get_access_token()
    limit = settings.sold_sync_page_limit
    offset = 0
    out: list[dict] = []

    while True:
        body = await _ebay_get_orders(token, since=since, offset=offset, limit=limit)
        page = body.get("orders") or []

        logger.debug(
            "sold sync fetched offset=%d size=%d total=%s",
            offset, len(page), body.get("total"),
        )

        if not page:
            break
        out.extend(page)

        if len(page) < limit:
            break
        offset += limit

        if offset > 10000:
            logger.warning("sold sync pagination limit reached, stopping")
            break

    return out


async def _process_order_with_metrics(
    order_data: dict,
    sem: asyncio.Semaphore,
    metrics: SoldSyncMetrics,
) -> None:
    async with sem:
        try:
            outcome = await _process_order(order_data)
        except Exception as e:
            logger.exception(
                "sold sync process order error order_id=%s err=%s",
                order_data.get("orderId"), e,
            )
            metrics.failed += 1
            return

        if outcome == "created":
            metrics.sold_detected += 1
            metrics.order_created += 1
        elif outcome == "duplicate":
            metrics.duplicate_skipped += 1
        elif outcome == "unknown_sku_only":
            metrics.sold_detected += 1
            metrics.order_created += 1
            metrics.unknown_sku += 1
        elif outcome == "no_line_items":
            metrics.failed += 1


async def _process_order(order_data: dict) -> str:
    order_id = order_data.get("orderId")
    if not order_id:
        logger.warning("sold sync: order without orderId, skip")
        return "no_line_items"

    line_items = order_data.get("lineItems") or []
    if not line_items:
        logger.warning("sold sync: order has no lineItems order_id=%s", order_id)
        return "no_line_items"

    if settings.sold_sync_dry_run:
        logger.info(
            "sold sync DRY_RUN: would record order_id=%s line_items=%d",
            order_id, len(line_items),
        )
        return "created"

    async with AsyncSessionLocal() as session:
        exists = await session.execute(
            select(Order.id).where(Order.ebay_order_id == order_id)
        )
        if exists.scalar_one_or_none() is not None:
            logger.warning("duplicate order skipped order_id=%s", order_id)
            return "duplicate"

        primary = line_items[0]
        primary_sku = (primary.get("sku") or "").strip() or "UNKNOWN"
        primary_qty = int(primary.get("quantity") or 1)
        price_obj = primary.get("total") or primary.get("lineItemCost") or {}
        sold_price = str(price_obj.get("value", "0"))
        sold_currency = price_obj.get("currency", "USD")

        buyer_country = _extract_buyer_country(order_data)
        ebay_created = _parse_ebay_date(order_data.get("creationDate"))

        primary_listing = await _find_listing_by_sku(session, primary_sku)
        unknown_only = primary_listing is None
        if unknown_only:
            logger.warning(
                "unknown sku order_id=%s sku=%s (recording without listing link)",
                order_id, primary_sku,
            )

        order = Order(
            ebay_order_id=order_id,
            listing_id=primary_listing.id if primary_listing else None,
            sku=primary_sku,
            quantity=primary_qty,
            sold_price=sold_price,
            sold_currency=sold_currency,
            buyer_country=buyer_country,
            order_status=OrderStatus.DETECTED.value,
            raw_payload=order_data,
            ebay_created_at=ebay_created,
        )
        session.add(order)

        marked_listings: list[int] = []
        for li in line_items:
            sku = (li.get("sku") or "").strip()
            if not sku:
                continue
            listing = await _find_listing_by_sku(session, sku)
            if listing and listing.listing_status != ListingStatus.SOLD.value:
                listing.listing_status = ListingStatus.SOLD.value
                marked_listings.append(listing.id)

        await session.commit()
        await session.refresh(order)

        logger.info(
            "sold detected order_id=%s primary_sku=%s qty=%d listing_id=%s "
            "marked_listings=%s line_items=%d sold_price=%s %s",
            order_id, primary_sku, primary_qty,
            primary_listing.id if primary_listing else None,
            marked_listings, len(line_items),
            sold_price, sold_currency,
        )

        try:
            await _trigger_post_sold_actions(order, primary_listing, session)
        except Exception as e:
            logger.warning(
                "post-sold action failed order_id=%s err=%s (non-fatal)",
                order_id, e,
            )

    if unknown_only:
        return "unknown_sku_only"
    return "created"


async def _trigger_post_sold_actions(
    order: Order,
    listing: Listing | None,
    session: AsyncSession,
) -> None:
    """売れた直後の自動化 hook。

    現在: fulfillment_task を作成して人間 review を待つキューに入れる。

    将来:
      - approve 後に Playwright 自動購入
      - 追跡番号取得・eBay 同期
      - auto fulfill（人間介在なし）
    """
    await create_task_from_order(order, listing, session)


async def _find_listing_by_sku(session: AsyncSession, sku: str) -> Listing | None:
    if not sku:
        return None
    result = await session.execute(select(Listing).where(Listing.sku == sku))
    return result.scalar_one_or_none()


def _extract_buyer_country(order_data: dict) -> str | None:
    try:
        fs = order_data.get("fulfillmentStartInstructions") or []
        if fs and isinstance(fs, list):
            ship = fs[0].get("shippingStep", {}).get("shipTo", {}).get("contactAddress", {})
            cc = ship.get("countryCode")
            if cc:
                return cc
    except Exception:
        pass
    try:
        buyer = order_data.get("buyer") or {}
        ta = buyer.get("taxAddress") or {}
        cc = ta.get("countryCode")
        if cc:
            return cc
    except Exception:
        pass
    return None


def _parse_ebay_date(s: Any) -> datetime | None:
    if not s or not isinstance(s, str):
        return None
    try:
        normalized = s.replace("Z", "+00:00")
        return datetime.fromisoformat(normalized)
    except Exception:
        return None


def _format_ebay_date(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
