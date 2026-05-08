"""eBay buyer message の取得・分類・下書き生成・DB同期。

fail-safe:
  - AI 失敗時は keyword-based template fallback（classify_and_draft が常に成功）
  - 自動送信は既定 OFF（AUTO_REPLY_SEND_ENABLED=false）
  - 自動送信が許可されていても、requires_human=true や complaint/refund/dispute は
    必ず human review 待ちにする
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import BuyerMessage, MessageStatus
from app.services.ai.message_classifier import classify_and_draft_async
from app.services.cache_service import get_cached, set_cached
from app.services.ebay_auth_service import get_access_token
from app.services.ebay_listing_service import ListingError, _ebay_request

logger = logging.getLogger(__name__)


SYNC_STATE_KEY = "ebay_messages:last_received_at"
SYNC_STATE_TTL = 60 * 60 * 24 * 365


# ── eBay API（参考: GET /sell/messaging/v1/messages がGAでなければ legacy Trading API 経由）──
async def fetch_messages_page(
    token: str, *, limit: int = 50, offset: int = 0
) -> dict[str, Any]:
    """eBay 側のメッセージ取得 API。エンドポイントは将来差し替え可能。
    現状は scaffold として GET を叩いて返す。404/501 でも graceful fallback。
    """
    try:
        return await _ebay_request(
            "GET",
            f"/sell/messaging/v1/messages?limit={limit}&offset={offset}",
            token=token,
        )
    except ListingError as e:
        logger.warning("messaging API fetch failed (graceful): %s", e)
        return {"messages": []}


# ── parser ──────────────────────────
def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _extract_message_id(raw: dict) -> str:
    return str(
        raw.get("messageId") or raw.get("id") or raw.get("ebay_message_id") or ""
    )


# ── upsert ──────────────────────────
async def _upsert_message(
    session: AsyncSession, raw: dict[str, Any]
) -> tuple[str, str]:
    msg_id = _extract_message_id(raw)
    if not msg_id:
        return ("skipped", "")

    classified = await classify_and_draft_async(
        subject=raw.get("subject"),
        body=raw.get("body") or raw.get("content") or raw.get("text"),
        provider_name=settings.listing_copy_provider,
    )

    fields: dict[str, Any] = {
        "ebay_message_id": msg_id,
        "buyer_username": raw.get("senderUsername") or raw.get("buyer"),
        "ebay_order_id": str(raw.get("orderId")) if raw.get("orderId") else None,
        "subject": raw.get("subject"),
        "body": raw.get("body") or raw.get("content") or raw.get("text"),
        "received_at": _parse_dt(raw.get("receivedDate") or raw.get("creationDate")),
        "category": classified.category,
        "draft_reply": classified.draft_reply,
        "confidence": classified.confidence,
        "requires_human": classified.requires_human,
        "status": (
            MessageStatus.AWAITING_HUMAN.value
            if classified.requires_human
            else MessageStatus.DRAFTED.value
        ),
        "raw_payload": raw,
    }

    result = await session.execute(
        select(BuyerMessage).where(BuyerMessage.ebay_message_id == msg_id)
    )
    existing = result.scalar_one_or_none()
    if existing is None:
        session.add(BuyerMessage(**fields))
        await session.flush()
        logger.info(
            "buyer message imported id=%s category=%s human=%s",
            msg_id, classified.category, classified.requires_human,
        )
        return ("imported", msg_id)

    if existing.status in (MessageStatus.SENT.value, MessageStatus.APPROVED.value):
        # 既に人間が承認/送信した内容を上書きしない
        return ("skipped", msg_id)

    for k, v in fields.items():
        if k == "ebay_message_id":
            continue
        setattr(existing, k, v)
    return ("updated", msg_id)


async def sync_messages(session: AsyncSession) -> dict[str, int]:
    counters = {"imported": 0, "updated": 0, "skipped": 0}
    try:
        token = await get_access_token()
    except Exception as e:
        logger.warning("message sync auth failed (graceful): %s", e)
        return counters

    try:
        data = await fetch_messages_page(token, limit=50, offset=0)
    except Exception as e:
        logger.warning("message sync fetch failed (graceful): %s", e)
        return counters

    raw_messages = data.get("messages") or []
    for raw in raw_messages:
        try:
            action, _ = await _upsert_message(session, raw)
            counters[action] = counters.get(action, 0) + 1
            await session.commit()
        except Exception as e:
            logger.warning("message upsert failed (continue) err=%s", e)
            await session.rollback()

    logger.info(
        "messages synced imported=%d updated=%d skipped=%d",
        counters["imported"], counters["updated"], counters["skipped"],
    )
    return counters


# ── reply send（既定 OFF） ──────────
async def send_reply(
    *, message_id: int, session: AsyncSession, force: bool = False
) -> dict[str, Any]:
    """承認済みメッセージを eBay に送信。
    AUTO_REPLY_SEND_ENABLED=false（既定）の場合は force=True でないと送らない。
    """
    msg = await session.get(BuyerMessage, message_id)
    if msg is None:
        raise ListingError(f"message not found id={message_id}")

    if msg.requires_human and msg.status != MessageStatus.APPROVED.value:
        return {
            "success": False,
            "skipped": True,
            "reason": "requires_human and not approved",
        }
    if not settings.auto_reply_send_enabled and not force:
        return {
            "success": False,
            "skipped": True,
            "reason": "auto_reply_send disabled (use force=true to override)",
        }

    # eBay messaging API への送信は現状エンドポイントが不安定なため scaffold のみ。
    # 実装は将来差し替え（POST /sell/messaging/v1/messages 等）。
    logger.info(
        "send_reply scaffold msg_id=%d category=%s",
        message_id, msg.category,
    )
    msg.status = MessageStatus.SENT.value
    msg.replied_at = datetime.now(timezone.utc)
    await session.commit()
    return {"success": True, "message_id": message_id, "status": msg.status}
