import logging
import re
from typing import Any

from playwright.async_api import Page, TimeoutError as PWTimeout, async_playwright
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.config import settings
from app.services.cache_service import (
    get_cached,
    make_cache_key,
    set_cached,
)

logger = logging.getLogger(__name__)


class ScrapeError(Exception):
    pass


async def scrape_amazon(url: str) -> dict[str, Any]:
    """キャッシュ優先で Amazon 商品情報を取得する。

    キャッシュHit → 外部スクレイピングをスキップ
    キャッシュMiss → Playwrightで取得し、結果をキャッシュへ書き戻す
    """
    cache_key = make_cache_key("amazon", url)
    cached = await get_cached(cache_key)
    if cached is not None:
        logger.info("amazon cache hit url=%s", url)
        return cached

    logger.info("amazon cache miss url=%s", url)
    result = await _scrape_amazon_uncached(url)
    await set_cached(cache_key, result, settings.amazon_cache_ttl)
    return result


@retry(
    stop=stop_after_attempt(settings.max_retries),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception_type((PWTimeout, ScrapeError)),
    reraise=True,
)
async def _scrape_amazon_uncached(url: str) -> dict[str, Any]:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            context = await browser.new_context(
                user_agent=settings.user_agent,
                locale="ja-JP",
            )
            page = await context.new_page()
            await page.goto(
                url,
                timeout=settings.request_timeout,
                wait_until="domcontentloaded",
            )

            try:
                await page.wait_for_selector(
                    "#productTitle", timeout=settings.request_timeout
                )
            except PWTimeout:
                raise ScrapeError("商品タイトル要素が見つかりません")

            title = await _extract_title(page)
            if not title:
                raise ScrapeError("商品名が取得できませんでした")

            price = await _extract_price(page)
            in_stock = await _extract_stock(page)
            image_url = await _extract_image(page)

            return {
                "title": title,
                "price": price,
                "in_stock": in_stock,
                "image_url": image_url,
            }
        finally:
            await browser.close()


async def _extract_title(page: Page) -> str:
    el = await page.query_selector("#productTitle")
    if not el:
        return ""
    return (await el.inner_text()).strip()


async def _extract_price(page: Page) -> int:
    selectors = [
        ".a-price .a-offscreen",
        "#priceblock_ourprice",
        "#priceblock_dealprice",
        "#priceblock_saleprice",
        "span.a-price-whole",
    ]
    for sel in selectors:
        el = await page.query_selector(sel)
        if el:
            text = (await el.inner_text()).strip()
            digits = re.sub(r"[^\d]", "", text)
            if digits:
                return int(digits)
    return 0


async def _extract_stock(page: Page) -> bool:
    el = await page.query_selector("#availability")
    if not el:
        return False
    text = (await el.inner_text()).strip()
    out_of_stock_keywords = [
        "在庫切れ",
        "現在在庫切れ",
        "Currently unavailable",
        "入荷時期は未定",
    ]
    return not any(kw in text for kw in out_of_stock_keywords)


async def _extract_image(page: Page) -> str | None:
    selectors = ["#landingImage", "#imgBlkFront", "img#main-image"]
    for sel in selectors:
        el = await page.query_selector(sel)
        if el:
            src = await el.get_attribute("src")
            if src:
                return src
    return None
