"""eBay Offer サービス（create / update / publish / withdraw）。

3段階構造の publish フェーズで使う薄いラッパ。
HTTP レイヤーは既存 ebay_listing_service._ebay_request を再利用。
"""
from __future__ import annotations

import logging
from typing import Any

from app.services.ebay_listing_service import _ebay_request

logger = logging.getLogger(__name__)


async def create_offer(token: str, payload: dict[str, Any]) -> str:
    """POST /offer → offerId を返す。"""
    res = await _ebay_request(
        "POST",
        "/sell/inventory/v1/offer",
        token=token,
        json_body=payload,
    )
    offer_id = res.get("offerId", "")
    logger.info("offer created offer_id=%s sku=%s", offer_id, payload.get("sku"))
    return offer_id


async def update_offer(
    token: str, offer_id: str, payload: dict[str, Any]
) -> dict[str, Any]:
    return await _ebay_request(
        "PUT",
        f"/sell/inventory/v1/offer/{offer_id}",
        token=token,
        json_body=payload,
    )


async def publish_offer(token: str, offer_id: str) -> str:
    """publish → listingId を返す。"""
    res = await _ebay_request(
        "POST",
        f"/sell/inventory/v1/offer/{offer_id}/publish",
        token=token,
    )
    listing_id = res.get("listingId", "")
    logger.info(
        "listing published offer_id=%s listing_id=%s", offer_id, listing_id
    )
    return listing_id


async def safe_withdraw_offer(token: str, offer_id: str) -> None:
    try:
        await _ebay_request(
            "POST",
            f"/sell/inventory/v1/offer/{offer_id}/withdraw",
            token=token,
        )
        logger.info("offer withdrawn offer_id=%s", offer_id)
    except Exception as e:
        logger.warning("offer withdraw failed offer_id=%s err=%s", offer_id, e)


async def safe_delete_offer(token: str, offer_id: str) -> None:
    try:
        await _ebay_request(
            "DELETE",
            f"/sell/inventory/v1/offer/{offer_id}",
            token=token,
        )
        logger.info("offer deleted offer_id=%s", offer_id)
    except Exception as e:
        logger.warning("offer delete failed offer_id=%s err=%s", offer_id, e)
