"""eBay 販売文章 AI 生成のオーケストレーター。

- provider 切替（settings.listing_copy_provider）
- AI 失敗時に template fallback
- dry_run=true は強制 template
"""
from __future__ import annotations

import logging
from typing import Any

from app.config import settings
from app.db.models import Product
from app.services.ai.base import CopyProvider, CopyProviderError
from app.services.ai.claude_provider import ClaudeProvider
from app.services.ai.openai_provider import OpenAIProvider
from app.services.ai.template_provider import TemplateProvider

logger = logging.getLogger(__name__)


_PROVIDERS: dict[str, type[CopyProvider]] = {
    "template": TemplateProvider,
    "openai": OpenAIProvider,
    "claude": ClaudeProvider,
}


def _get_provider(name: str | None) -> CopyProvider:
    key = (name or settings.listing_copy_provider or "template").lower()
    cls = _PROVIDERS.get(key, TemplateProvider)
    return cls()


async def generate_copy(
    product: Product,
    *,
    dry_run: bool = False,
    provider_name: str | None = None,
) -> dict[str, Any]:
    """販売文章を生成する。失敗時は template fallback。

    dry_run=true → AI を呼ばず常に template
    """
    if dry_run:
        return await TemplateProvider().generate(product)

    provider = _get_provider(provider_name)

    if isinstance(provider, TemplateProvider):
        return await provider.generate(product)

    try:
        result = await provider.generate(product)
        logger.info(
            "copy generated provider=%s product_id=%s",
            provider.name, product.id,
        )
        return result
    except CopyProviderError as e:
        logger.warning(
            "ai fallback used provider=%s product_id=%s err=%s",
            provider.name, product.id, e,
        )
    except Exception as e:
        logger.warning(
            "ai unexpected error fallback used provider=%s product_id=%s err=%s",
            provider.name, product.id, e,
        )

    return await TemplateProvider().generate(product)
