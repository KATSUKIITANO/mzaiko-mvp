"""通関リスク判定（純粋関数）。

目的:
  日焼け止め5個を1口にまとめて発送 → 米国税関で「商用輸入」と判断され返送、
  という実際の事故を発注段階で防ぐ。

判定内容:
  - 商材カテゴリの通関リスク（化粧品・液体・サプリ・電池など）
  - 1小包あたりの最大個数（＝これを超える注文は分割発注する）

考え方:
  税関は「個人使用の範囲」を超える数量を商用輸入とみなす。
  同じ商品でも 1個なら通り、5個まとめると止まる。
  そこでリスクの高い商材ほど 1小包あたりの個数を絞る。

  high   → 1個/小包（化粧品・日焼け止め・医薬品的なもの）
  medium → 2個/小包（液体・粉末・食品など）
  low    → CUSTOMS_DEFAULT_MAX_UNITS_PER_PARCEL（既定5個）

既存 risk_filter_service（VeRO/Hazmat）とは別軸。
あちらは「出品していいか」、こちらは「どう発送を割るか」を決める。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.config import settings

logger = logging.getLogger(__name__)

RISK_HIGH = "high"
RISK_MEDIUM = "medium"
RISK_LOW = "low"

# ── 1個/小包に絞る商材（規制が厳しく、複数個で商用輸入とみなされやすい）
DEFAULT_HIGH_RISK_KEYWORDS: list[str] = [
    "sunscreen", "sun screen", "spf", "日焼け止め",
    "cosmetic", "化粧品",
    "serum", "美容液",
    "lotion", "ローション",
    "cream", "クリーム",
    "essence", "toner", "化粧水",
    "foundation", "ファンデーション",
    "lipstick", "口紅",
    "mask pack", "シートマスク",
    "supplement", "サプリ",
    "vitamin", "ビタミン",
    "medicine", "医薬",
    "ointment", "軟膏",
    "contact lens", "コンタクト",
]

# ── 2個/小包に絞る商材（液体・粉末・食品など）
DEFAULT_MEDIUM_RISK_KEYWORDS: list[str] = [
    "liquid", "液体",
    "powder", "パウダー", "粉末",
    "gel", "ジェル",
    "oil", "オイル",
    "shampoo", "シャンプー",
    "food", "食品",
    "tea", "coffee",
    "battery", "電池",
    "aerosol", "spray", "スプレー",
]


@dataclass
class CustomsRisk:
    level: str = RISK_LOW
    reasons: list[str] = field(default_factory=list)
    max_units_per_parcel: int = 1
    matched_keyword: str | None = None

    @property
    def is_risky(self) -> bool:
        return self.level != RISK_LOW


def assess_customs_risk(*, title: str = "", sku: str = "") -> CustomsRisk:
    """商品タイトルから通関リスクと1小包あたりの上限個数を決める。"""
    default_max = max(1, settings.customs_default_max_units_per_parcel)

    if not settings.customs_split_enabled:
        return CustomsRisk(max_units_per_parcel=default_max)

    haystack = " ".join(p for p in (title, sku) if p).lower()
    if not haystack.strip():
        return CustomsRisk(max_units_per_parcel=default_max)

    matched = _find_match(haystack, _high_risk_keywords())
    if matched:
        return CustomsRisk(
            level=RISK_HIGH,
            reasons=[f"customs_high:{matched}"],
            max_units_per_parcel=max(1, settings.customs_high_max_units_per_parcel),
            matched_keyword=matched,
        )

    matched = _find_match(haystack, _medium_risk_keywords())
    if matched:
        return CustomsRisk(
            level=RISK_MEDIUM,
            reasons=[f"customs_medium:{matched}"],
            max_units_per_parcel=max(1, settings.customs_medium_max_units_per_parcel),
            matched_keyword=matched,
        )

    return CustomsRisk(max_units_per_parcel=default_max)


def split_quantity(quantity: int, max_units: int) -> list[int]:
    """数量を1小包あたりの上限で割る。[5] & max=1 → [1,1,1,1,1]"""
    qty = max(1, int(quantity or 1))
    cap = max(1, int(max_units or 1))
    if qty <= cap:
        return [qty]

    chunks = [cap] * (qty // cap)
    remainder = qty % cap
    if remainder:
        chunks.append(remainder)
    return chunks


def _find_match(haystack: str, needles: list[str]) -> str | None:
    for needle in needles:
        if needle and needle in haystack:
            return needle
    return None


def _high_risk_keywords() -> list[str]:
    return _resolve(settings.customs_high_risk_keywords, DEFAULT_HIGH_RISK_KEYWORDS)


def _medium_risk_keywords() -> list[str]:
    return _resolve(settings.customs_medium_risk_keywords, DEFAULT_MEDIUM_RISK_KEYWORDS)


def _resolve(env_value: str, defaults: list[str]) -> list[str]:
    cleaned = (env_value or "").strip()
    items = (
        [k.strip() for k in cleaned.split(",") if k.strip()] if cleaned else defaults
    )
    return [k.lower() for k in items]
