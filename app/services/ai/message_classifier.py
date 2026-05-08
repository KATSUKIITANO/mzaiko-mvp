"""Buyer message のカテゴリ分類 + 返信下書き（template fallback）。

OpenAI/Claude 実装は将来の拡張ポイント。
新規依存追加禁止のため、当面は keyword-based の template provider が動く。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from app.db.models import MessageCategory
from app.services.ai.base import CopyProviderError

logger = logging.getLogger(__name__)


@dataclass
class ClassifiedReply:
    category: str
    confidence: float
    requires_human: bool
    draft_reply: str


# ── 分類用キーワード ─────────────────
_KEYWORDS: dict[str, tuple[str, ...]] = {
    MessageCategory.UNSAFE.value: (
        "scam", "fraud", "lawyer", "attorney", "police", "report you",
    ),
    MessageCategory.DISPUTE.value: (
        "dispute", "case", "claim", "paypal claim", "open a case",
    ),
    MessageCategory.REFUND.value: (
        "refund", "money back", "return my money",
    ),
    MessageCategory.COMPLAINT.value: (
        "broken", "damaged", "wrong item", "not as described",
        "disappointed", "terrible", "awful", "complaint",
    ),
    MessageCategory.TRACKING.value: (
        "tracking", "track", "where is my order", "shipment status",
        "haven't received", "didn't arrive",
    ),
    MessageCategory.SHIPPING.value: (
        "ship", "shipping", "delivery", "deliver", "when will i receive",
        "arrival",
    ),
    MessageCategory.DISCOUNT.value: (
        "discount", "coupon", "cheaper", "lower price", "offer",
    ),
    MessageCategory.GREETING.value: (
        "hello", "hi ", "thank", "thanks", "great seller", "good seller",
    ),
}

_HUMAN_REQUIRED_CATEGORIES = {
    MessageCategory.UNSAFE.value,
    MessageCategory.REFUND.value,
    MessageCategory.DISPUTE.value,
    MessageCategory.COMPLAINT.value,
}


# ── 返信テンプレート ──────────────────
_REPLY_TEMPLATES: dict[str, str] = {
    MessageCategory.SHIPPING.value: (
        "Hello, thank you for your message. Your item ships from Japan via "
        "tracked international shipping. Estimated delivery is 3-7 business "
        "days after dispatch. Please let us know if you have any other "
        "questions."
    ),
    MessageCategory.TRACKING.value: (
        "Hello, thank you for reaching out. Once your order is dispatched, "
        "the tracking number will be uploaded to eBay automatically. "
        "Please check the order details page on eBay for the latest status."
    ),
    MessageCategory.DISCOUNT.value: (
        "Hello, thank you for your interest. Our listing prices already "
        "reflect our best offer. We hope you understand."
    ),
    MessageCategory.GREETING.value: (
        "Hello, thank you very much for your kind message and for shopping "
        "with us. Please let us know if there is anything we can help with."
    ),
    MessageCategory.COMPLAINT.value: "",
    MessageCategory.REFUND.value: "",
    MessageCategory.DISPUTE.value: "",
    MessageCategory.UNSAFE.value: "",
    MessageCategory.OTHER.value: (
        "Hello, thank you for your message. We will get back to you as soon "
        "as possible."
    ),
}


def _normalize(text: str | None) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", text.lower()).strip()


def classify_message(subject: str | None, body: str | None) -> tuple[str, float]:
    """カテゴリと confidence を返す。最初にマッチした優先カテゴリを採用。"""
    text = f"{_normalize(subject)} {_normalize(body)}"
    if not text.strip():
        return (MessageCategory.OTHER.value, 0.1)

    priority_order = [
        MessageCategory.UNSAFE.value,
        MessageCategory.DISPUTE.value,
        MessageCategory.REFUND.value,
        MessageCategory.COMPLAINT.value,
        MessageCategory.TRACKING.value,
        MessageCategory.SHIPPING.value,
        MessageCategory.DISCOUNT.value,
        MessageCategory.GREETING.value,
    ]
    for cat in priority_order:
        for kw in _KEYWORDS.get(cat, ()):
            if kw in text:
                return (cat, 0.6)
    return (MessageCategory.OTHER.value, 0.2)


def build_template_reply(category: str) -> str:
    return _REPLY_TEMPLATES.get(category, _REPLY_TEMPLATES[MessageCategory.OTHER.value])


def classify_and_draft(
    *, subject: str | None, body: str | None
) -> ClassifiedReply:
    """template-based: 常に成功・依存ゼロ。"""
    category, conf = classify_message(subject, body)
    requires_human = category in _HUMAN_REQUIRED_CATEGORIES
    draft = "" if requires_human else build_template_reply(category)
    if requires_human and not draft:
        # human_required でも初稿は薄く出す（人間が編集しやすいよう）
        draft = (
            "Thank you for your message. We are reviewing the details and "
            "will follow up shortly."
        )
    logger.info(
        "message classified category=%s conf=%.2f human=%s",
        category, conf, requires_human,
    )
    return ClassifiedReply(
        category=category,
        confidence=conf,
        requires_human=requires_human,
        draft_reply=draft,
    )


# ── AI provider 拡張ポイント ──────────
async def classify_and_draft_async(
    *,
    subject: str | None,
    body: str | None,
    provider_name: str | None = None,
) -> ClassifiedReply:
    """provider 切替対応。OpenAI/Claude 実装は将来差し替え。
    現状は template fallback で常に成功。
    """
    if provider_name and provider_name not in ("template", None):
        try:
            raise CopyProviderError(
                f"{provider_name} provider not implemented yet"
            )
        except CopyProviderError as e:
            logger.warning("ai fallback used err=%s", e)
    return classify_and_draft(subject=subject, body=body)
