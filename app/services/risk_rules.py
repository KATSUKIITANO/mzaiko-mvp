"""リスクフィルタの参照ルール定義。

将来 DB 化する際は `get_vero_brands` / `get_hazmat_keywords` の中身だけ
DB lookup に差し替えれば呼び出し側は無修正で済む構造にしている。
"""
from app.config import settings


# ── デフォルトの VeRO / 知財ハイリスク・ブランド ──────────────
DEFAULT_VERO_BRANDS: list[str] = [
    "Nintendo",
    "Sony",
    "Apple",
    "LEGO",
    "Rolex",
    "Louis Vuitton",
    "Nike",
    "Adidas",
    "Pokemon",
    "Bandai",
]

# ── 危険物 / 発送不可キーワード ──────────────────────────
DEFAULT_HAZMAT_KEYWORDS: list[str] = [
    "lithium",
    "battery",
    "perfume",
    "alcohol",
    "spray",
    "gas",
    "lighter",
    "medicine",
    "liquid",
]


# ── reason 識別子 ──────────────────────────────────
RISK_REASON_VERO_BRAND: str = "vero_brand"
RISK_REASON_HAZMAT: str = "hazmat"


# ── risk_level 値 ──────────────────────────────────
RISK_LEVEL_HIGH: str = "high"
RISK_LEVEL_LOW: str = "low"


def get_vero_brands() -> list[str]:
    """env が空ならデフォルト、設定ありなら env を採用。すべて lowercase で返す。"""
    return _resolve_keywords(settings.risk_vero_brands, DEFAULT_VERO_BRANDS)


def get_hazmat_keywords() -> list[str]:
    return _resolve_keywords(settings.risk_hazmat_keywords, DEFAULT_HAZMAT_KEYWORDS)


def _resolve_keywords(env_value: str, defaults: list[str]) -> list[str]:
    cleaned_env = env_value.strip()
    if cleaned_env:
        items = [k.strip() for k in cleaned_env.split(",") if k.strip()]
    else:
        items = defaults
    return [k.lower() for k in items]
