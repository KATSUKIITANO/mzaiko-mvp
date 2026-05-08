import asyncio
import logging

from app.config import settings
from app.db.database import AsyncSessionLocal
from app.schemas.analyze import AnalyzeRequest, AnalyzeResponse
from app.schemas.bulk import (
    BulkAnalyzeItemResult,
    BulkAnalyzeRequest,
    BulkAnalyzeResponse,
)
from app.services.analyze_service import analyze_product

logger = logging.getLogger(__name__)


async def bulk_analyze_products(payload: BulkAnalyzeRequest) -> BulkAnalyzeResponse:
    urls: list[str] = [str(u) for u in payload.amazon_urls]
    total_count = len(urls)

    logger.info(
        "bulk_analyze start total=%d concurrency=%d timeout=%ds retries=%d",
        total_count,
        settings.max_concurrent_analysis,
        settings.bulk_analyze_timeout_seconds,
        settings.bulk_analyze_max_retries,
    )

    semaphore = asyncio.Semaphore(settings.max_concurrent_analysis)
    tasks = [_analyze_one_with_retry(url, semaphore) for url in urls]
    raw_results = await asyncio.gather(*tasks, return_exceptions=True)

    success_results: list[AnalyzeResponse] = []
    failed_count = 0

    for url, item in zip(urls, raw_results, strict=True):
        if isinstance(item, BaseException):
            failed_count += 1
            logger.warning("bulk item failed url=%s err=%s", url, item)
            continue
        success_results.append(item)

    profitable_count = sum(1 for r in success_results if r.is_profitable)
    blocked_count = sum(1 for r in success_results if not r.is_allowed)

    results = [
        BulkAnalyzeItemResult(
            title=r.amazon_title,
            profit=r.profit,
            profit_rate=r.profit_rate,
            profit_score=r.profit_score,
            demand_score=r.demand_score,
            competition_score=r.competition_score,
            overall_score=r.overall_score,
            is_profitable=r.is_profitable,
            risk_level=r.risk_level,
            risk_reason=r.risk_reason,
            is_allowed=r.is_allowed,
        )
        for r in success_results
    ]

    logger.info(
        "bulk_analyze done total=%d success=%d profitable=%d blocked=%d failed=%d",
        total_count,
        len(success_results),
        profitable_count,
        blocked_count,
        failed_count,
    )

    return BulkAnalyzeResponse(
        total_count=total_count,
        success_count=len(success_results),
        profitable_count=profitable_count,
        blocked_count=blocked_count,
        failed_count=failed_count,
        results=results,
    )


async def _analyze_one_with_retry(
    url: str,
    semaphore: asyncio.Semaphore,
) -> AnalyzeResponse:
    async with semaphore:
        last_exc: BaseException | None = None
        max_attempts = max(1, settings.bulk_analyze_max_retries + 1)

        for attempt in range(1, max_attempts + 1):
            try:
                return await _analyze_one(url)
            except asyncio.TimeoutError as e:
                last_exc = e
                logger.warning(
                    "bulk timeout attempt=%d/%d url=%s",
                    attempt, max_attempts, url,
                )
            except Exception as e:
                last_exc = e
                logger.warning(
                    "bulk attempt failed attempt=%d/%d url=%s err=%s",
                    attempt, max_attempts, url, e,
                )

            if attempt < max_attempts:
                backoff = min(2 ** attempt, 30)
                await asyncio.sleep(backoff)

        assert last_exc is not None
        raise last_exc


async def _analyze_one(url: str) -> AnalyzeResponse:
    async with asyncio.timeout(settings.bulk_analyze_timeout_seconds):
        async with AsyncSessionLocal() as session:
            return await analyze_product(
                AnalyzeRequest(amazon_url=url),
                session,
            )
