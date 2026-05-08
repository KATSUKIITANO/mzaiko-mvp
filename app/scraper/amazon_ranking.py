import asyncio
import logging
import re
from typing import Any

from playwright.async_api import (
    Page,
    TimeoutError as PWTimeout,
    async_playwright,
)
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.config import settings
from app.scraper.ranking_category import (
    UnknownCategoryError,
    resolve_category_url,
)
from app.services.cache_service import (
    get_cached,
    make_cache_key,
    set_cached,
)

logger = logging.getLogger(__name__)


ITEMS_PER_PAGE: int = 50  # Amazon Best Sellers は 1ページ最大50件
ASIN_RE = re.compile(r"/(?:dp|gp/product)/([A-Z0-9]{10})")
RANK_RE = re.compile(r"\d+")


class RankingScrapeError(Exception):
    pass


async def fetch_amazon_ranking(category: str, limit: int) -> list[dict[str, Any]]:
    """Amazon ランキングを取得（Redis キャッシュ優先）。

    キャッシュは「カテゴリ全件」を保持し、limit でスライスして返す。
    （limit ごとにキャッシュを分けないため、キャッシュ効率が高い）
    """
    cache_key = make_cache_key("ranking", category)
    cached = await get_cached(cache_key)
    if cached is not None:
        logger.info(
            "ranking cache hit category=%s cached_count=%d limit=%d",
            category, len(cached), limit,
        )
        return list(cached)[:limit]

    logger.info("ranking cache miss category=%s", category)

    target_count = min(limit, ITEMS_PER_PAGE * settings.ranking_max_pages)
    products = await _scrape_with_pagination(category, max_items=target_count)
    await set_cached(cache_key, products, settings.ranking_cache_ttl)
    logger.info(
        "ranking cached category=%s count=%d ttl=%d",
        category, len(products), settings.ranking_cache_ttl,
    )
    return products[:limit]


async def _scrape_with_pagination(
    category: str,
    max_items: int,
) -> list[dict[str, Any]]:
    pages_needed = min(
        settings.ranking_max_pages,
        (max_items + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE,
    )
    seen_asins: set[str] = set()
    results: list[dict[str, Any]] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            context = await browser.new_context(
                user_agent=settings.user_agent,
                locale="ja-JP",
                viewport={"width": 1366, "height": 900},
            )
            page = await context.new_page()

            for page_no in range(1, pages_needed + 1):
                url = resolve_category_url(category, page=page_no)
                logger.info(
                    "ranking fetch category=%s page=%d url=%s",
                    category, page_no, url,
                )

                try:
                    items = await _fetch_one_page(page, url)
                except RankingScrapeError as e:
                    logger.warning(
                        "ranking page failed category=%s page=%d err=%s",
                        category, page_no, e,
                    )
                    if page_no == 1:
                        raise
                    break

                added = 0
                for item in items:
                    if item["asin"] in seen_asins:
                        continue
                    seen_asins.add(item["asin"])
                    results.append(item)
                    added += 1

                logger.info(
                    "ranking page extracted category=%s page=%d added=%d total=%d",
                    category, page_no, added, len(results),
                )

                if len(results) >= max_items:
                    break

                if page_no < pages_needed:
                    await asyncio.sleep(settings.ranking_page_delay_seconds)

            return results[:max_items]
        finally:
            await browser.close()


@retry(
    stop=stop_after_attempt(settings.max_retries),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception_type((PWTimeout, RankingScrapeError)),
    reraise=True,
)
async def _fetch_one_page(page: Page, url: str) -> list[dict[str, Any]]:
    try:
        await page.goto(
            url,
            timeout=settings.request_timeout,
            wait_until="domcontentloaded",
        )
    except PWTimeout:
        raise RankingScrapeError(f"page load timeout url={url}")

    # ランキンググリッドが現れるのを待つ（出ない場合はそのまま続行）
    try:
        await page.wait_for_selector(
            '[id^="gridItemRoot"], [id^="p13n-asin-index-"]',
            timeout=settings.request_timeout,
        )
    except PWTimeout:
        logger.debug("ranking grid selector timeout url=%s (continuing)", url)

    # lazy-load された要素のためにスクロール
    await _scroll_to_bottom(page)

    items = await page.evaluate(
        """
        () => {
            const out = [];
            const seen = new Set();
            const containers = [
                ...document.querySelectorAll('[id^="gridItemRoot"]'),
                ...document.querySelectorAll('[id^="p13n-asin-index-"]'),
            ];
            for (const card of containers) {
                const link = card.querySelector('a[href*="/dp/"], a[href*="/gp/product/"]');
                if (!link) continue;
                const m = link.getAttribute('href').match(/\\/(?:dp|gp\\/product)\\/([A-Z0-9]{10})/);
                if (!m) continue;
                const asin = m[1];
                if (seen.has(asin)) continue;
                seen.add(asin);

                let title = '';
                const img = card.querySelector('img');
                if (img && img.alt) title = img.alt.trim();
                if (!title) {
                    const titleEl = card.querySelector(
                        'div._cDEzb_p13n-sc-css-line-clamp-1_1Fn1y, ' +
                        'div._cDEzb_p13n-sc-css-line-clamp-2_EWgCb, ' +
                        'div._cDEzb_p13n-sc-css-line-clamp-3_g3dy1, ' +
                        'div._cDEzb_p13n-sc-css-line-clamp-4_2q2cc'
                    );
                    if (titleEl) title = titleEl.textContent.trim();
                }
                if (!title) {
                    const aria = link.getAttribute('aria-label');
                    if (aria) title = aria.trim();
                }

                const rankBadge = card.querySelector('.zg-bdg-text');
                const rank_text = rankBadge ? rankBadge.textContent.trim() : '';

                let href = link.getAttribute('href') || '';
                if (href && !href.startsWith('http')) {
                    href = 'https://www.amazon.co.jp' + href;
                }

                out.push({ rank_text, title, href, asin });
            }
            return out;
        }
        """
    )

    parsed: list[dict[str, Any]] = []
    sequence = 1
    for item in items:
        rank = _parse_rank(item.get("rank_text", "")) or sequence
        parsed.append(
            {
                "rank": rank,
                "title": (item.get("title") or "").strip(),
                "amazon_url": _normalize_amazon_url(item["asin"]),
                "asin": item["asin"],
            }
        )
        sequence += 1

    if not parsed:
        raise RankingScrapeError(f"no items extracted from {url}")

    return parsed


async def _scroll_to_bottom(page: Page) -> None:
    try:
        for _ in range(5):
            await page.evaluate(
                "window.scrollBy(0, document.body.scrollHeight)"
            )
            await page.wait_for_timeout(500)
    except Exception as e:
        logger.debug("scroll error (ignored): %s", e)


def _parse_rank(rank_text: str) -> int | None:
    m = RANK_RE.search(rank_text)
    return int(m.group()) if m else None


def _normalize_amazon_url(asin: str) -> str:
    return f"https://www.amazon.co.jp/dp/{asin}"


# ── 公開: UnknownCategoryError も呼び出し側に見せる
__all__ = [
    "fetch_amazon_ranking",
    "RankingScrapeError",
    "UnknownCategoryError",
]
