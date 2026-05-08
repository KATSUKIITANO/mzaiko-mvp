"""Anthropic Claude Copy Provider（スケルトン）。

将来 Anthropic API を呼ぶ実装を入れるための placeholder。
新規依存追加禁止のため、今は generate() で CopyProviderError を投げ、
listing_copy_service が template provider に fallback する。
"""
from __future__ import annotations

import logging
from typing import Any

from app.db.models import Product
from app.services.ai.base import CopyProvider, CopyProviderError

logger = logging.getLogger(__name__)


class ClaudeProvider(CopyProvider):
    name = "claude"

    async def generate(self, product: Product) -> dict[str, Any]:
        logger.warning(
            "claude provider not implemented yet; raising for fallback"
        )
        raise CopyProviderError("Claude provider not implemented")
