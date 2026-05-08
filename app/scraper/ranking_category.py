"""Amazonランキングのカテゴリ → URL マッピング。

URL ハードコードを最小化し、追加・変更を1か所に集約する。
将来 DB 化する場合は `resolve_category_url` の中身だけ差し替えれば
呼び出し側は無修正で動作する。
"""
# ── ベース URL（変更があればここだけ修正）
AMAZON_RANKING_BASE_URL: str = "https://www.amazon.co.jp/gp/bestsellers"
AMAZON_RANKING_PAGE_PARAM: str = "pg"  # ?pg=2 で 51-100 位


# ── カテゴリスラッグ表 ──────────────
# key: 公開API用カテゴリ名 / value: Amazonサイト上のスラッグ
CATEGORIES: dict[str, str] = {
    "videogames": "videogames",
    "camera": "camera",
    "electronics": "electronics",
    "hobby": "hobby",
    "toys": "toys",
    "kitchen": "kitchen",
    "books": "books",
    "computers": "computers",
    "beauty": "beauty",
    "office": "office-products",
}


class UnknownCategoryError(ValueError):
    pass


def resolve_category_url(category: str, page: int = 1) -> str:
    if category not in CATEGORIES:
        raise UnknownCategoryError(
            f"Unknown category '{category}'. Available: {sorted(CATEGORIES.keys())}"
        )
    slug = CATEGORIES[category]
    base = f"{AMAZON_RANKING_BASE_URL}/{slug}"
    if page <= 1:
        return base
    return f"{base}?{AMAZON_RANKING_PAGE_PARAM}={page}"


def list_categories() -> list[str]:
    return sorted(CATEGORIES.keys())
