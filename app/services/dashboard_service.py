"""ダッシュボード集計サービス。

設計:
  Layer 1 (Query Builder)  : build_*_query() — SQLAlchemy Select を返す純粋関数
  Layer 2 (Fetch)          : fetch_*() — 実行 + 結果変換
  Layer 3 (Service Entry)  : get_dashboard_summary() — 組み立て + ログ

将来 chart/timeseries API を追加する際は Layer 1 の query builder に
GROUP BY を加えるだけで再利用できる構造。
キャッシュも Layer 3 を `@cached` でラップするだけで導入可能。
"""
import logging

from sqlalchemy import Float, Select, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import MonitoringStatus, Product
from app.schemas.dashboard import DashboardSummary, TopProduct

logger = logging.getLogger(__name__)


TOP_PRODUCTS_LIMIT: int = 5


# ───────────────────────────────────────
# Layer 1: Query Builder（純粋）
# ───────────────────────────────────────
def build_aggregate_query() -> Select:
    """件数・平均をすべて 1クエリ で取得する集約 SELECT。"""
    rate_expr = func.coalesce(Product.current_profit_rate, Product.profit_rate)

    return select(
        func.count(Product.id).label("total"),
        func.coalesce(
            func.sum(case((Product.is_profitable.is_(True), 1), else_=0)), 0
        ).label("profitable"),
        func.coalesce(
            func.sum(case((Product.is_allowed.is_(True), 1), else_=0)), 0
        ).label("allowed"),
        func.coalesce(
            func.sum(case((Product.is_allowed.is_(False), 1), else_=0)), 0
        ).label("blocked"),
        func.coalesce(
            func.sum(
                case(
                    (
                        Product.monitoring_status == MonitoringStatus.WARNING.value,
                        1,
                    ),
                    else_=0,
                )
            ),
            0,
        ).label("warning"),
        func.coalesce(
            func.sum(
                case(
                    (
                        Product.monitoring_status == MonitoringStatus.DANGER.value,
                        1,
                    ),
                    else_=0,
                )
            ),
            0,
        ).label("danger"),
        func.avg(rate_expr).label("avg_profit_rate"),
        func.avg(Product.overall_score).label("avg_overall_score"),
    )


def build_top_profitable_query(limit: int = TOP_PRODUCTS_LIMIT) -> Select:
    """利益率の高い順 TOP N（is_profitable=True のみ）。"""
    rate_expr = func.coalesce(
        Product.current_profit_rate, Product.profit_rate
    ).label("rate")

    return (
        select(
            Product.id,
            Product.title,
            Product.overall_score,
            rate_expr,
        )
        .where(Product.is_profitable.is_(True))
        .order_by(
            func.coalesce(Product.current_profit_rate, Product.profit_rate)
            .desc()
            .nullslast()
        )
        .limit(limit)
    )


def build_top_scored_query(limit: int = TOP_PRODUCTS_LIMIT) -> Select:
    """overall_score の高い順 TOP N（INDEX 活用）。"""
    rate_expr = func.coalesce(
        Product.current_profit_rate, Product.profit_rate
    ).label("rate")

    return (
        select(
            Product.id,
            Product.title,
            Product.overall_score,
            rate_expr,
        )
        .where(Product.overall_score.is_not(None))
        .order_by(Product.overall_score.desc().nullslast())
        .limit(limit)
    )


# ───────────────────────────────────────
# Layer 2: Fetch（実行 + 変換）
# ───────────────────────────────────────
async def fetch_aggregates(session: AsyncSession) -> dict:
    result = await session.execute(build_aggregate_query())
    row = result.one()
    return {
        "total": int(row.total or 0),
        "profitable": int(row.profitable or 0),
        "allowed": int(row.allowed or 0),
        "blocked": int(row.blocked or 0),
        "warning": int(row.warning or 0),
        "danger": int(row.danger or 0),
        "avg_profit_rate": _round_or_zero(row.avg_profit_rate),
        "avg_overall_score": _round_or_zero(row.avg_overall_score),
    }


async def fetch_top_profitable(session: AsyncSession) -> list[TopProduct]:
    result = await session.execute(build_top_profitable_query())
    return [
        TopProduct(
            id=row.id,
            title=row.title,
            overall_score=row.overall_score,
            profit_rate=_round_or_none(row.rate),
        )
        for row in result
    ]


async def fetch_top_scored(session: AsyncSession) -> list[TopProduct]:
    result = await session.execute(build_top_scored_query())
    return [
        TopProduct(
            id=row.id,
            title=row.title,
            overall_score=row.overall_score,
            profit_rate=_round_or_none(row.rate),
        )
        for row in result
    ]


# ───────────────────────────────────────
# Layer 3: Service Entry
# ───────────────────────────────────────
async def fetch_order_aggregates(session: AsyncSession) -> dict:
    """orders テーブルから today_orders / today_sales / pending_shipments を集計。"""
    from datetime import datetime, time, timezone

    from app.db.models import Order

    today_start = datetime.combine(
        datetime.now(timezone.utc).date(), time.min, tzinfo=timezone.utc,
    )

    today_count_stmt = select(func.count(Order.id)).where(
        Order.created_at >= today_start
    )
    today_orders = (await session.execute(today_count_stmt)).scalar_one() or 0

    today_sales_stmt = select(
        func.coalesce(func.sum(func.cast(Order.sold_price, Float)), 0.0)
    ).where(Order.created_at >= today_start)
    today_sales = (await session.execute(today_sales_stmt)).scalar_one() or 0.0

    pending_stmt = select(func.count(Order.id)).where(
        Order.fulfillment_status.notin_(["shipped", "delivered", "cancelled",
                                          "SHIPPED", "DELIVERED", "CANCELLED",
                                          "FULFILLED"])
        | Order.fulfillment_status.is_(None)
    )
    pending = (await session.execute(pending_stmt)).scalar_one() or 0

    return {
        "today_orders": int(today_orders),
        "today_sales": float(today_sales),
        "pending_shipments": int(pending),
    }


async def get_dashboard_summary(session: AsyncSession) -> DashboardSummary:
    aggregates = await fetch_aggregates(session)
    top_profitable = await fetch_top_profitable(session)
    top_scored = await fetch_top_scored(session)
    orders = await fetch_order_aggregates(session)

    summary = DashboardSummary(
        total_products=aggregates["total"],
        profitable_products=aggregates["profitable"],
        allowed_products=aggregates["allowed"],
        blocked_products=aggregates["blocked"],
        warning_products=aggregates["warning"],
        danger_products=aggregates["danger"],
        average_profit_rate=aggregates["avg_profit_rate"],
        average_overall_score=aggregates["avg_overall_score"],
        top_profitable_products=top_profitable,
        top_scored_products=top_scored,
        today_orders=orders["today_orders"],
        today_sales=orders["today_sales"],
        pending_shipments=orders["pending_shipments"],
    )

    logger.info(
        "dashboard summary total=%d profitable=%d allowed=%d blocked=%d "
        "warning=%d danger=%d avg_rate=%.2f avg_score=%.2f top_profit=%d top_score=%d",
        summary.total_products,
        summary.profitable_products,
        summary.allowed_products,
        summary.blocked_products,
        summary.warning_products,
        summary.danger_products,
        summary.average_profit_rate,
        summary.average_overall_score,
        len(top_profitable),
        len(top_scored),
    )

    return summary


# ───────────────────────────────────────
# helpers
# ───────────────────────────────────────
def _round_or_zero(value) -> float:
    if value is None:
        return 0.0
    return round(float(value), 2)


def _round_or_none(value) -> float | None:
    if value is None:
        return None
    return round(float(value), 2)
