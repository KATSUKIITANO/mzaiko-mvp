import asyncio
import logging
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.database import AsyncSessionLocal
from app.db.models import MonitoringStatus, Product
from app.schemas.ebay import EbaySearchRequest
from app.scraper.amazon import scrape_amazon
from app.services.ebay import search_ebay
from app.services.cj_order_service import run_cj_order_worker
from app.services.ebay_message_service import sync_messages
from app.services.ebay_order_service import sync_orders
from app.services.repricing_service import bulk_reprice
from app.services.ebay_revise_service import sync_ebay_listing
from app.services.notification_service import notify_status_change
from app.services.revise_worker_service import run_revise_worker
from app.services.score_service import (
    ProductScore,
    compute_scores,
    is_profitable_by_score,
)
from app.services.sold_sync_service import run_sold_sync_worker

logger = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None


async def start_scheduler() -> None:
    global _scheduler

    if _scheduler is not None and _scheduler.running:
        logger.warning("scheduler already running")
        return

    scheduler = AsyncIOScheduler(timezone="UTC")

    # ── 監視バッチ
    if settings.monitor_enabled:
        scheduler.add_job(
            run_monitoring_batch,
            trigger=IntervalTrigger(minutes=settings.monitor_interval_minutes),
            id="monitoring_batch",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )
        logger.info(
            "scheduler: monitoring_batch registered interval=%dmin batch_size=%d",
            settings.monitor_interval_minutes,
            settings.monitor_batch_size,
        )
    else:
        logger.info("scheduler: monitoring_batch disabled")

    # ── eBay revise worker
    if settings.revise_worker_enabled:
        scheduler.add_job(
            run_revise_worker,
            trigger=IntervalTrigger(minutes=settings.revise_worker_interval_minutes),
            id="revise_worker",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )
        logger.info(
            "scheduler: revise_worker registered interval=%dmin "
            "batch_limit=%d concurrency=%d dry_run=%s",
            settings.revise_worker_interval_minutes,
            settings.revise_worker_batch_limit,
            settings.revise_worker_concurrency,
            settings.revise_worker_dry_run,
        )
    else:
        logger.info("scheduler: revise_worker disabled")

    # ── eBay sold sync worker
    if settings.sold_sync_enabled:
        scheduler.add_job(
            run_sold_sync_worker,
            trigger=IntervalTrigger(minutes=settings.sold_sync_interval_minutes),
            id="sold_sync_worker",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )
        logger.info(
            "scheduler: sold_sync_worker registered interval=%dmin "
            "concurrency=%d lookback=%dh dry_run=%s",
            settings.sold_sync_interval_minutes,
            settings.sold_sync_concurrency,
            settings.sold_sync_lookback_hours,
            settings.sold_sync_dry_run,
        )
    else:
        logger.info("scheduler: sold_sync_worker disabled")

    # ── CJ 発注ワーカー（未発注の CJ 発注 + 状態同期 + tracking 反映）
    if settings.cj_order_enabled and settings.cj_order_sync_enabled:
        scheduler.add_job(
            run_cj_order_worker,
            trigger=IntervalTrigger(
                minutes=settings.cj_order_sync_interval_minutes
            ),
            id="cj_order_worker",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )
        logger.info(
            "scheduler: cj_order_worker registered interval=%dmin "
            "batch_limit=%d dry_run=%s auto_on_sold=%s on_approve=%s",
            settings.cj_order_sync_interval_minutes,
            settings.cj_order_sync_batch_limit,
            settings.cj_order_dry_run,
            settings.cj_auto_order_on_sold,
            settings.cj_order_on_approve,
        )
    else:
        logger.info("scheduler: cj_order_worker disabled")

    # ── eBay buyer message sync
    if settings.ebay_message_sync_enabled:
        scheduler.add_job(
            run_ebay_message_sync,
            trigger=IntervalTrigger(
                minutes=settings.ebay_message_sync_interval_minutes
            ),
            id="ebay_message_sync",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )
        logger.info(
            "scheduler: ebay_message_sync registered interval=%dmin auto_reply_send=%s",
            settings.ebay_message_sync_interval_minutes,
            settings.auto_reply_send_enabled,
        )

    # ── eBay order sync
    if settings.ebay_order_sync_enabled:
        scheduler.add_job(
            run_ebay_order_sync,
            trigger=IntervalTrigger(
                minutes=settings.ebay_order_sync_interval_minutes
            ),
            id="ebay_order_sync",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )
        logger.info(
            "scheduler: ebay_order_sync registered interval=%dmin",
            settings.ebay_order_sync_interval_minutes,
        )
    else:
        logger.info("scheduler: ebay_order_sync disabled")

    # ── eBay auto repricing
    if settings.auto_repricing_enabled:
        scheduler.add_job(
            run_auto_repricing,
            trigger=IntervalTrigger(
                minutes=settings.auto_repricing_interval_minutes
            ),
            id="auto_repricing",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )
        logger.info(
            "scheduler: auto_repricing registered interval=%dmin dry_run=%s batch=%d",
            settings.auto_repricing_interval_minutes,
            settings.auto_repricing_dry_run,
            settings.auto_repricing_batch_limit,
        )

    if not scheduler.get_jobs():
        logger.info("scheduler: no jobs registered, skip start")
        return

    scheduler.start()
    _scheduler = scheduler
    logger.info("scheduler started")


async def run_ebay_order_sync() -> None:
    """scheduler 用ラッパ。失敗してもジョブを止めない。"""
    try:
        async with AsyncSessionLocal() as session:
            await sync_orders(session)
    except Exception as e:
        logger.warning("ebay_order_sync crashed (graceful): %s", e)


async def run_ebay_message_sync() -> None:
    try:
        async with AsyncSessionLocal() as session:
            await sync_messages(session)
    except Exception as e:
        logger.warning("ebay_message_sync crashed (graceful): %s", e)


async def run_auto_repricing() -> None:
    try:
        async with AsyncSessionLocal() as session:
            await bulk_reprice(
                session,
                dry_run=settings.auto_repricing_dry_run,
                limit=settings.auto_repricing_batch_limit,
            )
    except Exception as e:
        logger.warning("auto_repricing crashed (graceful): %s", e)


async def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is None:
        return
    try:
        if _scheduler.running:
            _scheduler.shutdown(wait=False)
            logger.info("scheduler stopped (graceful)")
    except Exception as e:
        logger.warning("scheduler shutdown error: %s", e)
    finally:
        _scheduler = None


# ─────────────────────────────────────
# 監視バッチ（既存）
# ─────────────────────────────────────
async def run_monitoring_batch() -> None:
    started = datetime.now(timezone.utc)
    logger.info("monitoring batch start at=%s", started.isoformat())
    try:
        total = await _process_all_target_products()
        logger.info("monitoring batch complete processed=%d", total)
    except Exception:
        logger.exception("monitoring batch failed")


async def _process_all_target_products() -> int:
    semaphore = asyncio.Semaphore(settings.max_concurrent_analysis)
    batch_size = settings.monitor_batch_size
    offset = 0
    total_processed = 0

    while True:
        ids = await _fetch_target_ids(offset=offset, limit=batch_size)
        if not ids:
            break

        logger.info(
            "monitoring page offset=%d size=%d ids=%s",
            offset, len(ids), ids[:5],
        )

        tasks = [_recheck_with_timeout(pid, semaphore) for pid in ids]
        await asyncio.gather(*tasks, return_exceptions=True)

        total_processed += len(ids)
        if len(ids) < batch_size:
            break
        offset += batch_size

    return total_processed


async def _fetch_target_ids(offset: int, limit: int) -> list[int]:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Product.id)
            .where(Product.is_profitable.is_(True))
            .where(Product.is_allowed.is_(True))
            .order_by(Product.id)
            .offset(offset)
            .limit(limit)
        )
        return list(result.scalars().all())


async def _recheck_with_timeout(
    product_id: int,
    semaphore: asyncio.Semaphore,
) -> None:
    async with semaphore:
        try:
            async with asyncio.timeout(settings.bulk_analyze_timeout_seconds):
                await _recheck_one(product_id)
        except asyncio.TimeoutError:
            logger.warning("recheck timeout product_id=%d", product_id)
        except Exception as e:
            logger.warning("recheck failed product_id=%d err=%s", product_id, e)


async def _recheck_one(product_id: int) -> None:
    snapshot: dict | None = None

    async with AsyncSessionLocal() as session:
        product = await session.get(Product, product_id)
        if product is None:
            logger.warning("product not found id=%d", product_id)
            return

        old_status = product.monitoring_status
        url = product.url

        try:
            amazon_data = await scrape_amazon(url)
            new_title = amazon_data["title"] or product.title
            amazon_price = amazon_data["price"]

            ebay_result = await search_ebay(EbaySearchRequest(keyword=new_title))
            ebay_avg = ebay_result.average_price
            item_count = ebay_result.total_item_count

            platform_fee = int(ebay_avg * settings.ebay_fee_percent)
            shipping_cost = settings.default_shipping_cost
            new_profit = ebay_avg - amazon_price - platform_fee - shipping_cost
            new_rate = _calc_rate(new_profit, ebay_avg)

            scores = compute_scores(profit_rate=new_rate, item_count=item_count)
            new_status = _determine_status(new_profit, new_rate)
            is_profitable = (
                new_status == MonitoringStatus.OK.value
                and is_profitable_by_score(scores.overall_score)
            )

            await _update_product(
                session=session,
                product=product,
                amazon_data=amazon_data,
                amazon_price=amazon_price,
                ebay_avg=ebay_avg,
                platform_fee=platform_fee,
                shipping_cost=shipping_cost,
                new_profit=new_profit,
                new_rate=new_rate,
                status=new_status,
                is_profitable=is_profitable,
                scores=scores,
            )

            logger.info(
                "rechecked id=%d title=%s profit=%d rate=%.2f overall=%d status=%s",
                product_id, new_title, new_profit, new_rate,
                scores.overall_score, new_status,
            )

            if product.ebay_listing_status == "PUBLISHED":
                try:
                    sync_result = await sync_ebay_listing(product, session)
                    if not sync_result.get("success"):
                        logger.warning(
                            "sync partial failure id=%d action=%s err=%s",
                            product_id, sync_result.get("action"),
                            sync_result.get("error"),
                        )
                except Exception as e:
                    logger.warning(
                        "sync_ebay_listing crashed (graceful) id=%d err=%s",
                        product_id, e,
                    )

            snapshot = {
                "product_id": product_id,
                "title": new_title,
                "amazon_price": amazon_price,
                "ebay_price": ebay_avg,
                "current_profit": new_profit,
                "current_profit_rate": new_rate,
                "old_status": old_status,
                "new_status": new_status,
                "amazon_url": url,
            }
        except Exception:
            await session.rollback()
            raise

    if snapshot is not None:
        try:
            await notify_status_change(**snapshot)
        except Exception as e:
            logger.warning(
                "notification dispatch failed (graceful) id=%d err=%s",
                product_id, e,
            )


async def _update_product(
    session: AsyncSession,
    product: Product,
    amazon_data: dict,
    amazon_price: int,
    ebay_avg: int,
    platform_fee: int,
    shipping_cost: int,
    new_profit: int,
    new_rate: float,
    status: str,
    is_profitable: bool,
    scores: ProductScore,
) -> None:
    product.title = amazon_data["title"] or product.title
    product.price = amazon_price
    product.in_stock = amazon_data["in_stock"]
    product.image_url = amazon_data["image_url"]
    product.purchase_price = amazon_price
    product.selling_price = ebay_avg
    product.platform_fee = platform_fee
    product.shipping_cost = shipping_cost
    product.current_profit = new_profit
    product.current_profit_rate = new_rate
    product.monitoring_status = status
    product.is_profitable = is_profitable
    product.profit_score = scores.profit_score
    product.demand_score = scores.demand_score
    product.competition_score = scores.competition_score
    product.overall_score = scores.overall_score
    product.last_checked_at = datetime.now(timezone.utc)
    await session.commit()


def _calc_rate(profit: int, ebay_avg: int) -> float:
    if ebay_avg <= 0:
        return 0.0
    rate = (Decimal(profit) / Decimal(ebay_avg)) * Decimal(100)
    return float(rate.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _determine_status(profit: int, rate: float) -> str:
    if profit <= 0:
        return MonitoringStatus.DANGER.value
    if rate < settings.profit_threshold:
        return MonitoringStatus.WARNING.value
    return MonitoringStatus.OK.value
