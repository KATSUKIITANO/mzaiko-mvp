"""production monitoring / alert system。

- health: DB / Redis / scheduler の生存確認
- metrics: 過去24hの失敗件数（repricing / sync / publish）
- alert: Slack / Discord webhook（optional）
- structured logging: extra={...} 形式で抽出しやすい

新規依存追加禁止のため、httpx / sqlalchemy / redis は既存。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import (
    BuyerMessage,
    MessageStatus,
    Order,
    Product,
    RepricingLog,
)
from app.services.cache_service import get_redis_client

logger = logging.getLogger(__name__)


# ── health ─────────────────────────
async def check_db(session: AsyncSession) -> dict[str, Any]:
    try:
        await session.execute(select(1))
        return {"status": "ok"}
    except Exception as e:
        logger.warning("db health failed: %s", e, extra={"component": "db"})
        return {"status": "fail", "error": str(e)[:200]}


async def check_redis() -> dict[str, Any]:
    client = get_redis_client()
    if client is None:
        return {"status": "disabled", "note": "no client (cache fallback)"}
    try:
        pong = await client.ping()
        return {"status": "ok" if pong else "fail"}
    except Exception as e:
        logger.warning("redis health failed: %s", e, extra={"component": "redis"})
        return {"status": "fail", "error": str(e)[:200]}


def check_scheduler() -> dict[str, Any]:
    from app.services import scheduler_service

    sched = getattr(scheduler_service, "_scheduler", None)
    if sched is None:
        return {"status": "not_started", "jobs": []}
    if not sched.running:
        return {"status": "stopped", "jobs": []}
    jobs = []
    for job in sched.get_jobs():
        jobs.append({
            "id": job.id,
            "next_run": job.next_run_time.isoformat() if job.next_run_time else None,
        })
    return {"status": "running", "jobs": jobs}


async def get_health(session: AsyncSession) -> dict[str, Any]:
    db = await check_db(session)
    redis = await check_redis()
    sched = check_scheduler()
    overall = "ok"
    if db["status"] != "ok" or sched["status"] not in ("running",):
        overall = "degraded"
    return {
        "overall": overall,
        "db": db,
        "redis": redis,
        "scheduler": sched,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


# ── metrics ────────────────────────
async def get_metrics(session: AsyncSession) -> dict[str, Any]:
    since = datetime.now(timezone.utc) - timedelta(hours=24)

    # repricing
    reprice_total_q = select(func.count(RepricingLog.id)).where(
        RepricingLog.created_at >= since
    )
    reprice_fail_q = select(func.count(RepricingLog.id)).where(
        RepricingLog.created_at >= since, RepricingLog.success.is_(False)
    )
    reprice_total = (await session.execute(reprice_total_q)).scalar_one() or 0
    reprice_fail = (await session.execute(reprice_fail_q)).scalar_one() or 0

    # publish (Product.ebay_listing_status 由来、24h は last_sync で代用)
    publish_published = (await session.execute(
        select(func.count(Product.id)).where(
            Product.ebay_listing_status == "PUBLISHED"
        )
    )).scalar_one() or 0
    publish_failed = (await session.execute(
        select(func.count(Product.id)).where(
            Product.ebay_listing_status.in_(
                ["INVENTORY_FAILED", "OFFER_FAILED", "PUBLISH_FAILED"]
            )
        )
    )).scalar_one() or 0

    # sync (Product.ebay_sync_status='sync_failed')
    sync_failed = (await session.execute(
        select(func.count(Product.id)).where(
            Product.ebay_sync_status == "sync_failed"
        )
    )).scalar_one() or 0

    # orders today / pending
    today_orders = (await session.execute(
        select(func.count(Order.id)).where(Order.created_at >= since)
    )).scalar_one() or 0

    # buyer messages awaiting human
    msg_awaiting = (await session.execute(
        select(func.count(BuyerMessage.id)).where(
            BuyerMessage.status == MessageStatus.AWAITING_HUMAN.value
        )
    )).scalar_one() or 0

    def _rate(success: int, total: int) -> float:
        if total <= 0:
            return 0.0
        return round(100.0 * (1 - success / total), 2)

    return {
        "window_hours": 24,
        "publish": {
            "published_total": int(publish_published),
            "failed_total": int(publish_failed),
            "success_rate_percent": (
                round(100.0 * publish_published /
                      (publish_published + publish_failed), 2)
                if (publish_published + publish_failed) else 100.0
            ),
        },
        "sync": {
            "failed_count": int(sync_failed),
        },
        "repricing": {
            "total_24h": int(reprice_total),
            "failed_24h": int(reprice_fail),
            "failure_rate_percent": _rate(reprice_total - reprice_fail, reprice_total),
        },
        "orders": {
            "imported_24h": int(today_orders),
        },
        "messages": {
            "awaiting_human": int(msg_awaiting),
        },
    }


# ── alert ──────────────────────────
async def send_alert(
    *,
    message: str,
    severity: str = "info",
) -> dict[str, Any]:
    """Slack / Discord webhook へ送信。両方未設定なら no-op。"""
    targets: list[str] = []
    delivery: list[dict[str, Any]] = []

    slack_url = settings.slack_webhook_url
    discord_url = settings.discord_webhook_url

    async with httpx.AsyncClient(timeout=10.0) as client:
        if slack_url:
            targets.append("slack")
            try:
                res = await client.post(
                    slack_url,
                    json={"text": f"[{severity.upper()}] {message}"},
                )
                delivery.append({"target": "slack", "status": res.status_code})
            except Exception as e:
                logger.warning("slack alert failed: %s", e)
                delivery.append({"target": "slack", "error": str(e)[:200]})
        if discord_url:
            targets.append("discord")
            try:
                res = await client.post(
                    discord_url,
                    json={"content": f"**[{severity.upper()}]** {message}"},
                )
                delivery.append({"target": "discord", "status": res.status_code})
            except Exception as e:
                logger.warning("discord alert failed: %s", e)
                delivery.append({"target": "discord", "error": str(e)[:200]})

    if not targets:
        logger.info("alert dropped (no webhook configured): %s", message)
        return {"sent": False, "reason": "no webhook configured"}

    logger.info(
        "alert sent severity=%s targets=%s",
        severity, ",".join(targets),
        extra={"alert": True, "severity": severity},
    )
    return {"sent": True, "delivery": delivery}
