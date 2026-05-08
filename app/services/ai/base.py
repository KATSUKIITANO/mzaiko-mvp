"""AI Copy Provider 抽象基底クラス。

将来 OpenAI / Claude / ローカルLLM を差し替えできるように
generate() を async 統一インターフェースで定義する。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from app.db.models import Product


class CopyProvider(ABC):
    """eBay販売文章生成プロバイダの基底。"""

    name: str = "base"

    @abstractmethod
    async def generate(self, product: Product) -> dict[str, Any]:
        """商品情報から販売文章一式を生成する。

        必須キー:
          optimized_title, subtitle, html_description, bullet_points,
          seo_keywords, condition_note, shipping_note, return_note
        """
        raise NotImplementedError


class CopyProviderError(Exception):
    pass
