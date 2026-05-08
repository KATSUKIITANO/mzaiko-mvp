"""需要スコアリング（純粋関数のみ）。

I/O は持たず、入力 (profit_rate, item_count) → 出力 (4スコア) の関数に閉じる。
ユニットテストが容易な設計。
"""
import logging
from dataclasses import dataclass

from app.config import settings

logger = logging.getLogger(__name__)


# ── 利益スコアのカーブ
PROFIT_RATE_MAX_PERCENT: float = 30.0  # この利益率で profit_score=100

# ── 需要スコアのカーブ
DEMAND_RAMP_END: int = 10           # 0 〜 ここまで線形上昇
DEMAND_OPTIMAL_LOW: int = 10        # 高スコア帯の下限（Inclusive）
DEMAND_OPTIMAL_HIGH: int = 200      # 高スコア帯の上限（Inclusive）
DEMAND_OVERSATURATED: int = 5000    # ここで 0、以降も 0

# ── 競合スコアのカーブ（出品数に対する減点幅）
COMPETITION_MAX_COUNT: int = 1000   # この出品数で competition_score=100（最大ペナルティ）

# ── 共通
SCORE_MIN: int = 0
SCORE_MAX: int = 100


@dataclass(frozen=True)
class ProductScore:
    profit_score: int
    demand_score: int
    competition_score: int
    overall_score: int


def compute_profit_score(profit_rate: float) -> int:
    """利益率を 0-100 に正規化。

    profit_rate <= 0       → 0
    profit_rate >= MAX     → 100
    その間                 → 線形
    """
    if profit_rate <= 0:
        return SCORE_MIN
    raw = profit_rate / PROFIT_RATE_MAX_PERCENT * SCORE_MAX
    return _clamp(raw)


def compute_demand_score(item_count: int) -> int:
    """検索ヒット件数から需要スコアを算出。

    0件                        → 0
    1〜OPTIMAL_LOW未満         → 線形上昇 (1→10ポイント, OPTIMAL_LOW→100)
    OPTIMAL_LOW〜OPTIMAL_HIGH  → 100（最適帯）
    OPTIMAL_HIGH〜OVERSATURATED → 線形下降（100→0）
    OVERSATURATED以上          → 0
    """
    if item_count <= 0:
        return SCORE_MIN
    if item_count < DEMAND_RAMP_END:
        return _clamp(item_count * SCORE_MAX / DEMAND_RAMP_END)
    if item_count <= DEMAND_OPTIMAL_HIGH:
        return SCORE_MAX
    if item_count >= DEMAND_OVERSATURATED:
        return SCORE_MIN
    distance = item_count - DEMAND_OPTIMAL_HIGH
    range_width = DEMAND_OVERSATURATED - DEMAND_OPTIMAL_HIGH
    declined = SCORE_MAX * (1 - distance / range_width)
    return _clamp(declined)


def compute_competition_score(item_count: int) -> int:
    """出品数を競合度合いとしてスコア化。多いほど高スコア（=減点が大きい）。"""
    if item_count <= 0:
        return SCORE_MIN
    if item_count >= COMPETITION_MAX_COUNT:
        return SCORE_MAX
    return _clamp(item_count * SCORE_MAX / COMPETITION_MAX_COUNT)


def compute_overall_score(
    profit_score: int,
    demand_score: int,
    competition_score: int,
) -> int:
    """重み付け合算。competition は減点。最終的に 0-100 にクランプ。"""
    raw = (
        profit_score * settings.score_profit_weight
        + demand_score * settings.score_demand_weight
        - competition_score * settings.score_competition_weight
    )
    return _clamp(raw)


def compute_scores(profit_rate: float, item_count: int) -> ProductScore:
    profit_s = compute_profit_score(profit_rate)
    demand_s = compute_demand_score(item_count)
    competition_s = compute_competition_score(item_count)
    overall_s = compute_overall_score(profit_s, demand_s, competition_s)

    logger.info(
        "scores profit=%d demand=%d competition=%d overall=%d "
        "(rate=%.2f%% items=%d weights=p%.2f/d%.2f/c%.2f)",
        profit_s, demand_s, competition_s, overall_s,
        profit_rate, item_count,
        settings.score_profit_weight,
        settings.score_demand_weight,
        settings.score_competition_weight,
    )

    return ProductScore(
        profit_score=profit_s,
        demand_score=demand_s,
        competition_score=competition_s,
        overall_score=overall_s,
    )


def is_profitable_by_score(overall_score: int) -> bool:
    return overall_score >= settings.score_threshold


def _clamp(value: float) -> int:
    return max(SCORE_MIN, min(SCORE_MAX, int(value)))
