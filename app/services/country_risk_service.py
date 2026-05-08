"""Country Risk Scoring Service。

I/O なしの純粋関数群。INR (Item Not Received) / fraud / 配送遅延が多い国を
事前にレベル付けし、profit 計算 / 自動 fail の判断材料にする。

設計:
  - country → risk_level (low/medium/high/blocked)
  - risk_level → risk_score (0-100、静的 mapping)
  - risk_level → refund_buffer 調整 (% 加算)
  - blocked: task auto failed
  - high:    task auto reviewing（manual review required）
  - medium / low: 通常フロー（pending）

将来:
  - delivery_success_rate 統計から動的スコアリング
  - fraud detection score 連携
  - country + buyer reputation の複合判定

env で上書き可能（CSV 空ならコード内 DEFAULTS 使用）:
  - RISK_BLOCKED_COUNTRIES
  - RISK_HIGH_COUNTRIES
  - RISK_MEDIUM_COUNTRIES
"""
from __future__ import annotations

import logging

from app.config import settings
from app.db.models import CountryRiskLevel

logger = logging.getLogger(__name__)


# ── デフォルト国マッピング
# 運用方針: BR / MX / IT / ZA は INR / 配送遅延・紛失リスクが高いので blocked へ
DEFAULT_BLOCKED_COUNTRIES: list[str] = [
    "RU", "IR", "KP",
    "BR", "MX", "IT", "ZA",
]
DEFAULT_HIGH_RISK_COUNTRIES: list[str] = []
DEFAULT_MEDIUM_RISK_COUNTRIES: list[str] = ["AR", "CL", "TR"]

# ── レベル → スコア（静的、将来動的化）
LEVEL_TO_SCORE: dict[str, int] = {
    CountryRiskLevel.LOW.value: 10,
    CountryRiskLevel.MEDIUM.value: 40,
    CountryRiskLevel.HIGH.value: 75,
    CountryRiskLevel.BLOCKED.value: 100,
}


def _resolve_csv(env_value: str, defaults: list[str]) -> set[str]:
    cleaned = (env_value or "").strip()
    if cleaned:
        return {c.strip().upper() for c in cleaned.split(",") if c.strip()}
    return {c.upper() for c in defaults}


def get_blocked_countries() -> set[str]:
    return _resolve_csv(settings.risk_blocked_countries, DEFAULT_BLOCKED_COUNTRIES)


def get_high_risk_countries() -> set[str]:
    return _resolve_csv(settings.risk_high_countries, DEFAULT_HIGH_RISK_COUNTRIES)


def get_medium_risk_countries() -> set[str]:
    return _resolve_csv(settings.risk_medium_countries, DEFAULT_MEDIUM_RISK_COUNTRIES)


def get_buffer_adjustment_percent_table() -> dict[str, float]:
    return {
        CountryRiskLevel.LOW.value: settings.risk_buffer_low_percent,
        CountryRiskLevel.MEDIUM.value: settings.risk_buffer_medium_percent,
        CountryRiskLevel.HIGH.value: settings.risk_buffer_high_percent,
        CountryRiskLevel.BLOCKED.value: 0.0,  # blocked は auto-fail のため意味なし
    }


# ── 公開 API ────────────────────────
def get_country_risk_level(country: str | None) -> str:
    """ISO 国コードから risk_level を返す。未収載は low。"""
    if not country:
        return CountryRiskLevel.LOW.value
    code = country.upper()

    if code in get_blocked_countries():
        return CountryRiskLevel.BLOCKED.value
    if code in get_high_risk_countries():
        return CountryRiskLevel.HIGH.value
    if code in get_medium_risk_countries():
        return CountryRiskLevel.MEDIUM.value
    return CountryRiskLevel.LOW.value


def compute_country_risk_score(country: str | None) -> int:
    """risk_score を返す。

    現状: level → static score。
    将来: delivery_success_rate / fraud signal / buyer reputation を加味した
          動的スコアリングに差し替え可能。
    """
    level = get_country_risk_level(country)
    return LEVEL_TO_SCORE.get(level, LEVEL_TO_SCORE[CountryRiskLevel.LOW.value])


def get_risk_buffer_percent(risk_level: str) -> float:
    """risk_level → refund_buffer 加算 %。"""
    table = get_buffer_adjustment_percent_table()
    return table.get(risk_level, 0.0)


def calc_risk_buffer_jpy(ebay_sold_jpy: int, risk_level: str) -> int:
    """risk_buffer 額（JPY 整数）を計算。"""
    pct = get_risk_buffer_percent(risk_level)
    return int(ebay_sold_jpy * pct / 100.0)


def is_blocked(country: str | None) -> bool:
    return get_country_risk_level(country) == CountryRiskLevel.BLOCKED.value


def is_high_risk(country: str | None) -> bool:
    return get_country_risk_level(country) == CountryRiskLevel.HIGH.value
