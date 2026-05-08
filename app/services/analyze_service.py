import logging
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import Product
from app.schemas.analyze import AnalyzeRequest, AnalyzeResponse
from app.schemas.ebay import EbaySearchRequest, EbaySearchResponse
from app.schemas.risk import RiskAssessment
from app.scraper.amazon import ScrapeError, scrape_amazon
from app.services.ebay import EbayApiError, search_ebay
from app.services.risk_filter_service import assess_risk
from app.services.score_service import (
    ProductScore,
    compute_scores,
    is_profitable_by_score,
)

logger = logging.getLogger(__name__)


class AnalyzeError(Exception):
    pass


async def analyze_product(
    payload: AnalyzeRequest,
    session: AsyncSession,
) -> AnalyzeResponse:
    url = str(payload.amazon_url)
    logger.info("analyze_product start url=%s", url)

    amazon_data = await _fetch_amazon(url)
    amazon_title = amazon_data["title"]
    amazon_price = amazon_data["price"]
    logger.info(
        "amazon ok title=%s price=%d",
        amazon_title, amazon_price,
    )

    # ── 早期リスク判定（ブロックなら eBay コールをスキップして API 負荷削減）
    risk = assess_risk(
        amazon_title=amazon_title,
        ebay_title=amazon_title,
        brand="",
    )
    if not risk.is_allowed:
        logger.info(
            "blocked by risk filter url=%s reasons=%s",
            url, risk.risk_reason,
        )
        return _build_blocked_response(amazon_title, amazon_price, risk)

    ebay_result = await _fetch_ebay(amazon_title)
    ebay_avg = ebay_result.average_price
    item_count = ebay_result.total_item_count
    logger.info("ebay ok average_price=%d items=%d", ebay_avg, item_count)

    platform_fee = int(ebay_avg * settings.ebay_fee_percent)
    shipping_cost = settings.default_shipping_cost
    profit = ebay_avg - amazon_price - platform_fee - shipping_cost
    profit_rate = _calc_profit_rate(profit, ebay_avg)

    scores = compute_scores(profit_rate=profit_rate, item_count=item_count)
    is_profitable = is_profitable_by_score(scores.overall_score)

    logger.info(
        "calc profit=%d rate=%.2f overall=%d profitable=%s allowed=%s",
        profit, profit_rate, scores.overall_score, is_profitable, risk.is_allowed,
    )

    if is_profitable and risk.is_allowed:
        await _save_product(
            session=session,
            url=url,
            amazon_data=amazon_data,
            amazon_price=amazon_price,
            ebay_avg=ebay_avg,
            platform_fee=platform_fee,
            shipping_cost=shipping_cost,
            profit=profit,
            profit_rate=profit_rate,
            scores=scores,
            risk=risk,
        )
        logger.info("db saved url=%s", url)
    else:
        logger.info(
            "skip db save url=%s reason=%s",
            url,
            "not profitable" if not is_profitable else "not allowed",
        )

    return AnalyzeResponse(
        amazon_title=amazon_title,
        amazon_price=amazon_price,
        ebay_average_price=ebay_avg,
        platform_fee=platform_fee,
        shipping_cost=shipping_cost,
        profit=profit,
        profit_rate=profit_rate,
        profit_score=scores.profit_score,
        demand_score=scores.demand_score,
        competition_score=scores.competition_score,
        overall_score=scores.overall_score,
        is_profitable=is_profitable,
        risk_level=risk.risk_level,
        risk_reason=risk.risk_reason,
        is_allowed=risk.is_allowed,
    )


def _build_blocked_response(
    amazon_title: str,
    amazon_price: int,
    risk: RiskAssessment,
) -> AnalyzeResponse:
    return AnalyzeResponse(
        amazon_title=amazon_title,
        amazon_price=amazon_price,
        ebay_average_price=0,
        platform_fee=0,
        shipping_cost=settings.default_shipping_cost,
        profit=0,
        profit_rate=0.0,
        profit_score=0,
        demand_score=0,
        competition_score=0,
        overall_score=0,
        is_profitable=False,
        risk_level=risk.risk_level,
        risk_reason=risk.risk_reason,
        is_allowed=risk.is_allowed,
    )


async def _fetch_amazon(url: str) -> dict[str, Any]:
    try:
        return await scrape_amazon(url)
    except ScrapeError as e:
        logger.error("amazon scrape failed: %s", e)
        raise AnalyzeError(f"Amazonスクレイピング失敗: {e}")
    except Exception as e:
        logger.exception("amazon scrape unexpected error")
        raise AnalyzeError(f"Amazonスクレイピング予期しないエラー: {e}")


async def _fetch_ebay(title: str) -> EbaySearchResponse:
    try:
        return await search_ebay(EbaySearchRequest(keyword=title))
    except EbayApiError as e:
        logger.error("ebay search failed: %s", e)
        raise AnalyzeError(f"eBay検索失敗: {e}")
    except Exception as e:
        logger.exception("ebay search unexpected error")
        raise AnalyzeError(f"eBay検索予期しないエラー: {e}")


def _calc_profit_rate(profit: int, ebay_avg: int) -> float:
    if ebay_avg <= 0:
        return 0.0
    rate = (Decimal(profit) / Decimal(ebay_avg)) * Decimal(100)
    return float(rate.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


async def _save_product(
    session: AsyncSession,
    url: str,
    amazon_data: dict[str, Any],
    amazon_price: int,
    ebay_avg: int,
    platform_fee: int,
    shipping_cost: int,
    profit: int,
    profit_rate: float,
    scores: ProductScore,
    risk: RiskAssessment,
) -> None:
    try:
        result = await session.execute(select(Product).where(Product.url == url))
        existing = result.scalar_one_or_none()

        if existing:
            existing.title = amazon_data["title"]
            existing.price = amazon_price
            existing.in_stock = amazon_data["in_stock"]
            existing.image_url = amazon_data["image_url"]
            existing.purchase_price = amazon_price
            existing.selling_price = ebay_avg
            existing.platform_fee = platform_fee
            existing.shipping_cost = shipping_cost
            existing.profit = profit
            existing.profit_rate = profit_rate
            existing.profit_score = scores.profit_score
            existing.demand_score = scores.demand_score
            existing.competition_score = scores.competition_score
            existing.overall_score = scores.overall_score
            existing.risk_level = risk.risk_level
            existing.risk_reason = list(risk.risk_reason)
            existing.is_allowed = risk.is_allowed
        else:
            session.add(
                Product(
                    url=url,
                    title=amazon_data["title"],
                    price=amazon_price,
                    in_stock=amazon_data["in_stock"],
                    image_url=amazon_data["image_url"],
                    purchase_price=amazon_price,
                    selling_price=ebay_avg,
                    platform_fee=platform_fee,
                    shipping_cost=shipping_cost,
                    profit=profit,
                    profit_rate=profit_rate,
                    profit_score=scores.profit_score,
                    demand_score=scores.demand_score,
                    competition_score=scores.competition_score,
                    overall_score=scores.overall_score,
                    risk_level=risk.risk_level,
                    risk_reason=list(risk.risk_reason),
                    is_allowed=risk.is_allowed,
                )
            )

        await session.commit()
    except Exception as e:
        await session.rollback()
        logger.exception("db save failed url=%s", url)
        raise AnalyzeError(f"DB保存失敗: {e}")
