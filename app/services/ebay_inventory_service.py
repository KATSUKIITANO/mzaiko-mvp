"""eBay Inventory Item サービス（CRUD + idempotent upsert）。

3段階構造の publish フェーズで使う薄いラッパ。
HTTP レイヤーは既存 ebay_listing_service._ebay_request を再利用し、
新規依存追加なしで retry/timeout を踏襲する。
"""
from __future__ import annotations

import logging
from typing import Any

from app.services.ebay_listing_service import ListingError, _ebay_request

logger = logging.getLogger(__name__)


def _is_not_found(err: Exception) -> bool:
    msg = str(err)
    return "status=404" in msg or "404" in msg


async def get_inventory_item(token: str, sku: str) -> dict[str, Any] | None:
    try:
        return await _ebay_request(
            "GET",
            f"/sell/inventory/v1/inventory_item/{sku}",
            token=token,
        )
    except ListingError as e:
        if _is_not_found(e):
            return None
        raise


async def upsert_inventory_item(
    token: str, sku: str, payload: dict[str, Any]
) -> dict[str, Any]:
    """idempotent: 既存 sku があれば update、なければ create。
    eBay の PUT /inventory_item/{sku} が createOrReplace なので、
    実装上は単に PUT を叩けばよい。
    """
    result = await _ebay_request(
        "PUT",
        f"/sell/inventory/v1/inventory_item/{sku}",
        token=token,
        json_body=payload,
    )
    logger.info("inventory created sku=%s", sku)
    return result


async def safe_delete_inventory_item(token: str, sku: str) -> None:
    """rollback 用。例外は WARNING に落として握り潰す。"""
    try:
        await _ebay_request(
            "DELETE",
            f"/sell/inventory/v1/inventory_item/{sku}",
            token=token,
        )
        logger.info("inventory deleted sku=%s", sku)
    except Exception as e:
        logger.warning("inventory delete failed sku=%s err=%s", sku, e)
