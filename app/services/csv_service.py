"""CSV エクスポート（streaming）。

設計:
  Layer 1 (Query)        : build_export_query()
  Layer 2 (Schema変換)   : product_to_row()
  Layer 3 (Render)       : render_csv_row()
  Streaming統合          : stream_products_csv()

xlsx 化する際は Layer 3 を openpyxl に差し替えるだけで済む構造。
"""
import csv
import io
import logging
from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy import Select, func, select

from app.db.database import AsyncSessionLocal
from app.db.models import Product
from app.schemas.export import ExportFilters

logger = logging.getLogger(__name__)


# ── 設定値 ──────────────────────────────
EXPORT_CHUNK_SIZE: int = 200
BOM: str = "﻿"
CSV_LINE_TERMINATOR: str = "\r\n"

# CSV injection 対策：これらで始まる値は先頭に "'" を付与
DANGEROUS_PREFIXES: tuple[str, ...] = ("=", "+", "-", "@", "\t", "\r")


# ── ヘッダ定義（出力順を保証）─────────────────
HEADERS: list[str] = [
    "id",
    "title",
    "amazon_price",
    "ebay_average_price",
    "profit",
    "profit_rate",
    "overall_score",
    "demand_score",
    "competition_score",
    "risk_level",
    "risk_reason",
    "monitoring_status",
    "is_profitable",
    "is_allowed",
    "created_at",
]


# ───────────────────────────────────────
# Layer 1: Query
# ───────────────────────────────────────
def build_export_query(filters: ExportFilters) -> Select[tuple[Product]]:
    stmt = select(Product).order_by(Product.id.desc())

    if filters.profitable_only:
        stmt = stmt.where(Product.is_profitable.is_(True))
    if filters.allowed_only:
        stmt = stmt.where(Product.is_allowed.is_(True))
    if filters.monitoring_status is not None:
        stmt = stmt.where(Product.monitoring_status == filters.monitoring_status)
    if filters.min_profit_rate is not None:
        stmt = stmt.where(
            func.coalesce(Product.current_profit_rate, Product.profit_rate)
            >= filters.min_profit_rate
        )
    if filters.min_overall_score is not None:
        stmt = stmt.where(Product.overall_score >= filters.min_overall_score)

    return stmt


# ───────────────────────────────────────
# Layer 2: Schema変換 (Product → list[str])
# ───────────────────────────────────────
def product_to_row(p: Product) -> list[str]:
    profit = p.current_profit if p.current_profit is not None else p.profit
    profit_rate = (
        p.current_profit_rate
        if p.current_profit_rate is not None
        else p.profit_rate
    )
    risk_reason_str = ", ".join(p.risk_reason) if p.risk_reason else ""
    created_at = p.created_at.isoformat() if p.created_at else ""

    return [
        _sanitize(p.id),
        _sanitize(p.title),
        _sanitize(p.price),
        _sanitize(p.selling_price),
        _sanitize(profit),
        _sanitize(profit_rate),
        _sanitize(p.overall_score),
        _sanitize(p.demand_score),
        _sanitize(p.competition_score),
        _sanitize(p.risk_level),
        _sanitize(risk_reason_str),
        _sanitize(p.monitoring_status),
        _sanitize(p.is_profitable),
        _sanitize(p.is_allowed),
        _sanitize(created_at),
    ]


def _sanitize(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    s = str(value)
    if s and s[0] in DANGEROUS_PREFIXES:
        return "'" + s
    return s


# ───────────────────────────────────────
# Layer 3: Render (list[str] → CSV string)
# ───────────────────────────────────────
def render_csv_row(values: list[str]) -> str:
    buf = io.StringIO()
    writer = csv.writer(
        buf,
        quoting=csv.QUOTE_MINIMAL,
        lineterminator=CSV_LINE_TERMINATOR,
    )
    writer.writerow(values)
    return buf.getvalue()


def render_csv_chunk(rows: list[list[str]]) -> str:
    buf = io.StringIO()
    writer = csv.writer(
        buf,
        quoting=csv.QUOTE_MINIMAL,
        lineterminator=CSV_LINE_TERMINATOR,
    )
    writer.writerows(rows)
    return buf.getvalue()


# ───────────────────────────────────────
# Streaming 統合
# ───────────────────────────────────────
async def stream_products_csv(
    filters: ExportFilters,
) -> AsyncIterator[bytes]:
    logger.info(
        "csv export start filters=profitable=%s allowed=%s status=%s "
        "min_rate=%s min_score=%s chunk=%d",
        filters.profitable_only,
        filters.allowed_only,
        filters.monitoring_status,
        filters.min_profit_rate,
        filters.min_overall_score,
        EXPORT_CHUNK_SIZE,
    )

    yield (BOM + render_csv_row(HEADERS)).encode("utf-8")

    base_stmt = build_export_query(filters)
    offset = 0
    total = 0

    async with AsyncSessionLocal() as session:
        while True:
            stmt = base_stmt.offset(offset).limit(EXPORT_CHUNK_SIZE)
            result = await session.execute(stmt)
            products = list(result.scalars().all())

            if not products:
                break

            rows = [product_to_row(p) for p in products]
            yield render_csv_chunk(rows).encode("utf-8")

            total += len(products)
            logger.debug(
                "csv export chunk offset=%d size=%d total=%d",
                offset, len(products), total,
            )

            if len(products) < EXPORT_CHUNK_SIZE:
                break
            offset += EXPORT_CHUNK_SIZE

    logger.info("csv export complete rows=%d", total)
