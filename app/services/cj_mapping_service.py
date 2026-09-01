"""eBay SKU ↔ CJ 商品（pid / vid）マッピング。

売れた SKU から「どの CJ 商品を発注するか」を解決するための土台。

登録経路:
  - 出品時（ebay_listing_cj_service / ebay_publish_service）に自動 upsert
  - POST /cj/mappings で手動 upsert（既存出品の後追い登録）

vid（variant id）は CJ の発注単位。未登録なら発注時に
resolve_variant_id() が CJ API から取得してマッピングへ書き戻す。
"""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import CJProductMapping
from app.scraper.cj import CJError, cj_variant_list, extract_variant_id

logger = logging.getLogger(__name__)


async def get_mapping(session: AsyncSession, sku: str) -> CJProductMapping | None:
    if not sku:
        return None
    result = await session.execute(
        select(CJProductMapping).where(CJProductMapping.sku == sku)
    )
    return result.scalar_one_or_none()


async def list_mappings(
    session: AsyncSession, *, limit: int, offset: int
) -> list[CJProductMapping]:
    result = await session.execute(
        select(CJProductMapping)
        .order_by(CJProductMapping.id.desc())
        .offset(offset)
        .limit(limit)
    )
    return list(result.scalars().all())


async def upsert_mapping(
    session: AsyncSession,
    *,
    sku: str,
    cj_product_id: str,
    product_id: int | None = None,
    cj_variant_id: str | None = None,
    cj_sku: str | None = None,
    source: str = "manual",
    note: str | None = None,
) -> CJProductMapping:
    """SKU 単位で upsert。既存行は与えられた値だけ上書きする。"""
    mapping = await get_mapping(session, sku)
    if mapping is None:
        mapping = CJProductMapping(sku=sku, cj_product_id=cj_product_id)
        session.add(mapping)

    mapping.cj_product_id = cj_product_id
    if product_id is not None:
        mapping.product_id = product_id
    if cj_variant_id:
        mapping.cj_variant_id = cj_variant_id
    if cj_sku:
        mapping.cj_sku = cj_sku
    if note:
        mapping.note = note
    mapping.source = source

    await session.commit()
    await session.refresh(mapping)

    logger.info(
        "cj mapping upserted sku=%s cj_pid=%s vid=%s source=%s",
        sku, cj_product_id, mapping.cj_variant_id, source,
    )
    return mapping


async def record_listing_mapping(
    session: AsyncSession,
    *,
    sku: str,
    cj_product_id: str,
    product_id: int | None,
) -> None:
    """出品フローからの best-effort 登録（失敗しても出品は止めない）。"""
    if not sku or not cj_product_id:
        return
    try:
        await upsert_mapping(
            session,
            sku=sku,
            cj_product_id=cj_product_id,
            product_id=product_id,
            source="listing",
        )
    except Exception as e:
        logger.warning(
            "cj mapping record failed (graceful) sku=%s cj_pid=%s err=%s",
            sku, cj_product_id, e,
        )


async def resolve_variant_id(
    session: AsyncSession, mapping: CJProductMapping
) -> str | None:
    """vid 未登録なら CJ API から取得して保存する。"""
    if mapping.cj_variant_id:
        return mapping.cj_variant_id

    try:
        variants = await cj_variant_list(mapping.cj_product_id)
    except CJError as e:
        logger.warning(
            "cj variant lookup failed sku=%s cj_pid=%s err=%s",
            mapping.sku, mapping.cj_product_id, e,
        )
        return None
    except Exception as e:
        logger.warning(
            "cj variant lookup unexpected error sku=%s cj_pid=%s err=%s",
            mapping.sku, mapping.cj_product_id, e,
        )
        return None

    vid = extract_variant_id(variants)
    if not vid:
        logger.warning(
            "cj variant not found sku=%s cj_pid=%s variants=%d",
            mapping.sku, mapping.cj_product_id, len(variants),
        )
        return None

    mapping.cj_variant_id = vid
    await session.commit()
    logger.info(
        "cj variant resolved sku=%s cj_pid=%s vid=%s",
        mapping.sku, mapping.cj_product_id, vid,
    )
    return vid
