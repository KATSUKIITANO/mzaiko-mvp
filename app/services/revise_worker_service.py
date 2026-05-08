"""eBay 自動 revise worker。

scheduler から定期呼び出され、Amazon の価格・在庫・タイトル・画像変動を検知して
eBay listing を自動 revise する。

設計:
  - target 抽出: listing_status='published' AND sync_status != 'running'
                 ORDER BY last_synced_at ASC NULLS FIRST LIMIT N
  - 並列処理: Semaphore(REVISE_WORKER_CONCURRENCY)
  - 各 listing:
      1. running 状態に遷移 (DB)
      2. risk / monitoring_status の skip ガード
      3. Amazon refetch（失敗時は DB の前回値を使う）
      4. 変化検知（title/price/in_stock/image_url）
      5. 変化なし → ok で終了
      6. 利益閾値チェック → 未達なら skipped
      7. revise_listing() を呼び出し
      8. sync_finished_at / next_sync_at を更新
  - メトリクス集計（target_count / synced / revised / skipped / failed / no_change）

future:
  - bulk revise を実装したらここの Semaphore ループを bulk_revise() に置換
  - PAAPI 連携時は `_refetch_amazon_into_product` の冒頭に追加
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.database import AsyncSessionLocal
from app.db.models import (
    Listing,
    ListingStatus,
    MonitoringStatus,
    Product,
    SyncStatus,
)
from app.schemas.listing import ReviseItemRequest
from app.scraper.amazon import scrape_amazon
from app.services.ebay_listing_service import (
    ListingError,
    RiskBlockedError,
    revise_listing,
)

logger = logging.getLogger(__name__)


# ── Module-level state（単一プロセス前提）──
@dataclass
class WorkerMetrics:
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None
    target_count: int = 0
    synced_count: int = 0
    revised_count: int = 0
    skipped_count: int = 0
    failed_count: int = 0
    no_change_count: int = 0

    @property
    def duration_seconds(self) -> float | None:
        if self.finished_at is None:
            return None
        return (self.finished_at - self.started_at).total_seconds()


_last_metrics: WorkerMetrics | None = None
_running_flag: bool = False
_run_lock: asyncio.Lock = asyncio.Lock()


# ── public ────────────────────────────
def get_last_metrics() -> WorkerMetrics | None:
    return _last_metrics


def is_worker_running() -> bool:
    return _running_flag


async def run_revise_worker() -> WorkerMetrics:
    """1回のスケジュール実行 entry point。"""
    global _last_metrics, _running_flag

    if not settings.revise_worker_enabled:
        logger.info("revise worker disabled (REVISE_WORKER_ENABLED=false)")
        m = WorkerMetrics()
        m.finished_at = m.started_at
        _last_metrics = m
        return m

    async with _run_lock:
        if _running_flag:
            logger.warning("revise worker already running, skip this tick")
            return _last_metrics or WorkerMetrics()
        _running_flag = True

    metrics = WorkerMetrics()
    try:
        target_ids = await _fetch_target_ids()
        metrics.target_count = len(target_ids)

        logger.info(
            "revise worker start targets=%d concurrency=%d "
            "interval=%dmin dry_run=%s",
            len(target_ids),
            settings.revise_worker_concurrency,
            settings.revise_worker_interval_minutes,
            settings.revise_worker_dry_run,
        )

        if not target_ids:
            metrics.finished_at = datetime.now(timezone.utc)
            _last_metrics = metrics
            return metrics

        sem = asyncio.Semaphore(settings.revise_worker_concurrency)
        tasks = [_process_with_metrics(lid, sem, metrics) for lid in target_ids]
        await asyncio.gather(*tasks, return_exceptions=True)

        metrics.finished_at = datetime.now(timezone.utc)
        logger.info(
            "revise worker complete duration=%.1fs target=%d synced=%d "
            "revised=%d no_change=%d skipped=%d failed=%d",
            metrics.duration_seconds or 0,
            metrics.target_count,
            metrics.synced_count,
            metrics.revised_count,
            metrics.no_change_count,
            metrics.skipped_count,
            metrics.failed_count,
        )
        _last_metrics = metrics
    except Exception:
        logger.exception("revise worker run aborted")
        if metrics.finished_at is None:
            metrics.finished_at = datetime.now(timezone.utc)
        _last_metrics = metrics
    finally:
        _running_flag = False

    return metrics


# ── target 抽出 ────────────────────────
async def _fetch_target_ids() -> list[int]:
    async with AsyncSessionLocal() as session:
        stmt = (
            select(Listing.id)
            .where(Listing.listing_status == ListingStatus.PUBLISHED.value)
            .where(Listing.sync_status != SyncStatus.RUNNING.value)
            .order_by(Listing.last_synced_at.asc().nullsfirst(), Listing.id.asc())
            .limit(settings.revise_worker_batch_limit)
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())


# ── per-listing 処理 ───────────────────
async def _process_with_metrics(
    listing_id: int,
    sem: asyncio.Semaphore,
    metrics: WorkerMetrics,
) -> None:
    async with sem:
        try:
            outcome = await _process_listing(listing_id)
        except Exception as e:
            logger.exception(
                "revise worker process unexpected error listing_id=%d err=%s",
                listing_id, e,
            )
            outcome = "failed"

        if outcome == "revised":
            metrics.revised_count += 1
            metrics.synced_count += 1
        elif outcome == "no_change":
            metrics.no_change_count += 1
            metrics.synced_count += 1
        elif outcome == "skipped":
            metrics.skipped_count += 1
            metrics.synced_count += 1
        elif outcome == "failed":
            metrics.failed_count += 1


async def _process_listing(listing_id: int) -> str:
    """1 listing を処理。 returns: revised | no_change | skipped | failed"""
    async with AsyncSessionLocal() as session:
        listing = await session.get(Listing, listing_id)
        if listing is None:
            logger.warning("revise worker: listing not found id=%d", listing_id)
            return "failed"

        # ── running に遷移
        listing.sync_status = SyncStatus.RUNNING.value
        listing.sync_started_at = datetime.now(timezone.utc)
        listing.sync_attempts = (listing.sync_attempts or 0) + 1
        listing.sync_error_message = None
        await session.commit()
        await session.refresh(listing)

        # ── product
        if listing.product_id is None:
            return await _finalize_skipped(
                session, listing, "product 紐付けが切れています"
            )
        product = await session.get(Product, listing.product_id)
        if product is None:
            return await _finalize_skipped(
                session, listing, f"product not found id={listing.product_id}"
            )

        # ── skip ガード（risk / monitoring）
        if product.risk_level != "low":
            return await _finalize_skipped(
                session, listing, f"risk_level={product.risk_level}"
            )
        if product.monitoring_status == MonitoringStatus.DANGER.value:
            return await _finalize_skipped(
                session, listing, "monitoring_status=danger"
            )

        # ── Amazon refetch（snapshot → refetch → compare）
        before = _snapshot_product(product)
        await _refetch_amazon_into_product(product, session)
        after = _snapshot_product(product)

        if before == after:
            return await _finalize_no_change(session, listing)

        logger.info(
            "revise worker change detected listing_id=%d sku=%s "
            "price=%s→%s in_stock=%s→%s",
            listing.id, listing.sku,
            before["price"], after["price"],
            before["in_stock"], after["in_stock"],
        )

        # ── 利益閾値チェック
        if not _check_profit_thresholds(product):
            return await _finalize_skipped(
                session, listing, "profit threshold 未達"
            )

        # ── revise 実行
        return await _trigger_revise(session, listing)


# ── revise 起動 ────────────────────────
async def _trigger_revise(
    session: AsyncSession,
    listing: Listing,
) -> str:
    req = ReviseItemRequest(
        dry_run=settings.revise_worker_dry_run,
        refetch_amazon=False,  # worker 側で既に refetch 済み
        markup_percent=None,   # env のデフォルト
        out_of_stock_action=settings.out_of_stock_action,
    )

    logger.info(
        "revise triggered listing_id=%d sku=%s dry_run=%s",
        listing.id, listing.sku, req.dry_run,
    )

    try:
        await revise_listing(listing.id, req, session)
    except RiskBlockedError as e:
        # ガードは事前に通したはずだが念のため
        return await _finalize_skipped(session, listing, str(e))
    except (ListingError, Exception) as e:
        logger.warning(
            "revise worker: revise_listing raised listing_id=%d err=%s",
            listing.id, e,
        )
        return await _finalize_error(session, listing, str(e))

    # revise_listing は内部で sync_status を OK / PARTIAL / DRY_RUN / ERROR に設定済
    await session.refresh(listing)
    now = datetime.now(timezone.utc)
    listing.sync_finished_at = now
    listing.next_sync_at = _calc_next_sync()
    await session.commit()

    logger.info(
        "listing synced listing_id=%d sku=%s sync_status=%s",
        listing.id, listing.sku, listing.sync_status,
    )

    if listing.sync_status == SyncStatus.ERROR.value:
        return "failed"
    return "revised"


# ── ファイナライズ helpers ──────────────
async def _finalize_skipped(
    session: AsyncSession,
    listing: Listing,
    reason: str,
) -> str:
    now = datetime.now(timezone.utc)
    listing.sync_status = SyncStatus.SKIPPED.value
    listing.sync_error_message = reason
    listing.sync_finished_at = now
    listing.last_synced_at = now
    listing.next_sync_at = _calc_next_sync()
    await session.commit()
    logger.info("revise skipped listing_id=%d reason=%s", listing.id, reason)
    return "skipped"


async def _finalize_no_change(
    session: AsyncSession,
    listing: Listing,
) -> str:
    now = datetime.now(timezone.utc)
    listing.sync_status = SyncStatus.OK.value
    listing.sync_error_message = None
    listing.sync_finished_at = now
    listing.last_synced_at = now
    listing.next_sync_at = _calc_next_sync()
    await session.commit()
    logger.info(
        "listing synced listing_id=%d sku=%s no_change",
        listing.id, listing.sku,
    )
    return "no_change"


async def _finalize_error(
    session: AsyncSession,
    listing: Listing,
    error_msg: str,
) -> str:
    now = datetime.now(timezone.utc)
    listing.sync_status = SyncStatus.ERROR.value
    listing.sync_error_message = error_msg
    listing.sync_finished_at = now
    listing.last_synced_at = now
    listing.next_sync_at = _calc_next_sync()
    await session.commit()
    logger.warning(
        "revise worker error listing_id=%d sku=%s err=%s",
        listing.id, listing.sku, error_msg,
    )
    return "failed"


# ── ユーティリティ ──────────────────────
def _calc_next_sync() -> datetime:
    return datetime.now(timezone.utc) + timedelta(
        minutes=settings.revise_worker_interval_minutes
    )


def _snapshot_product(product: Product) -> dict:
    return {
        "title": product.title,
        "price": product.price,
        "in_stock": product.in_stock,
        "image_url": product.image_url,
    }


def _check_profit_thresholds(product: Product) -> bool:
    selling = product.selling_price or 0
    purchase = product.purchase_price or product.price or 0
    fee = int(selling * settings.ebay_fee_percent)
    ship = settings.default_shipping_cost
    profit = selling - purchase - fee - ship

    if profit < settings.min_profit_yen:
        logger.debug(
            "profit threshold: profit=%d < %d",
            profit, settings.min_profit_yen,
        )
        return False

    if selling > 0:
        margin_pct = profit / selling * 100.0
        if margin_pct < settings.min_profit_margin_percent:
            logger.debug(
                "profit margin: %.2f%% < %.2f%%",
                margin_pct, settings.min_profit_margin_percent,
            )
            return False

    return True


async def _refetch_amazon_into_product(
    product: Product,
    session: AsyncSession,
) -> None:
    """Amazon を再取得して Product を更新。失敗時は前回値で続行。

    将来 PAAPI を追加する場合はこの関数の冒頭に挿入し、失敗時に
    scrape_amazon にフォールバックする構造にする。
    """
    try:
        data = await scrape_amazon(product.url)
        product.title = data["title"] or product.title
        product.price = data["price"]
        product.in_stock = data["in_stock"]
        product.image_url = data["image_url"]
        await session.commit()
        await session.refresh(product)
    except Exception as e:
        await session.rollback()
        logger.warning(
            "revise worker amazon refetch failed product_id=%d "
            "(continuing with cached) err=%s",
            product.id, e,
        )
