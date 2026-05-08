import asyncio
import base64
import logging
import time
from typing import Any

import httpx

from app.config import settings
from app.schemas.ebay import EbaySearchRequest, EbaySearchResponse
from app.services.cache_service import (
    get_cached,
    make_cache_key,
    set_cached,
)

logger = logging.getLogger(__name__)


class EbayApiError(Exception):
    pass


class _TokenCache:
    def __init__(self) -> None:
        self.token: str | None = None
        self.expires_at: float = 0.0
        self.lock: asyncio.Lock = asyncio.Lock()


_token_cache = _TokenCache()


async def search_ebay(payload: EbaySearchRequest) -> EbaySearchResponse:
    keyword = payload.keyword
    cache_key = make_cache_key("ebay", keyword)
    cached = await get_cached(cache_key)
    if cached is not None:
        logger.info("ebay cache hit keyword=%s", keyword)
        return EbaySearchResponse.model_validate(cached)

    logger.info("ebay cache miss keyword=%s", keyword)
    result = await _search_ebay_uncached(payload)
    await set_cached(cache_key, result.model_dump(), settings.ebay_cache_ttl)
    return result


async def _search_ebay_uncached(payload: EbaySearchRequest) -> EbaySearchResponse:
    token = await _get_oauth_token()

    url = f"{settings.ebay_api_base}/buy/browse/v1/item_summary/search"
    headers = {
        "Authorization": f"Bearer {token}",
        "X-EBAY-C-MARKETPLACE-ID": settings.ebay_marketplace,
        "Content-Type": "application/json",
    }
    params = {
        "q": payload.keyword,
        "limit": settings.ebay_search_limit,
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            res = await client.get(url, headers=headers, params=params)
    except httpx.HTTPError as e:
        raise EbayApiError(f"eBay APIリクエスト失敗: {e}")

    if res.status_code != 200:
        raise EbayApiError(
            f"eBay API応答エラー: status={res.status_code} body={res.text}"
        )

    body = res.json()
    items = body.get("itemSummaries", []) or []
    total = int(body.get("total", 0) or 0)

    prices_jpy: list[int] = []
    for item in items:
        price = _extract_jpy_price(item)
        if price > 0:
            prices_jpy.append(price)

    if not prices_jpy:
        return EbaySearchResponse(
            average_price=0,
            min_price=0,
            max_price=0,
            sold_count=total,
            total_item_count=total,
        )

    return EbaySearchResponse(
        average_price=int(sum(prices_jpy) / len(prices_jpy)),
        min_price=min(prices_jpy),
        max_price=max(prices_jpy),
        sold_count=total,
        total_item_count=total,
    )


async def _get_oauth_token() -> str:
    async with _token_cache.lock:
        now = time.time()
        if _token_cache.token and _token_cache.expires_at > now + 60:
            return _token_cache.token

        if not settings.ebay_client_id or not settings.ebay_client_secret:
            raise EbayApiError(
                "EBAY_CLIENT_ID / EBAY_CLIENT_SECRET が未設定です（.env を確認してください）"
            )

        credentials = base64.b64encode(
            f"{settings.ebay_client_id}:{settings.ebay_client_secret}".encode()
        ).decode()

        url = f"{settings.ebay_api_base}/identity/v1/oauth2/token"
        headers = {
            "Authorization": f"Basic {credentials}",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        data = {
            "grant_type": "client_credentials",
            "scope": settings.ebay_oauth_scope,
        }

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                res = await client.post(url, headers=headers, data=data)
        except httpx.HTTPError as e:
            raise EbayApiError(f"OAuthトークン取得通信失敗: {e}")

        if res.status_code != 200:
            raise EbayApiError(
                f"OAuthトークン取得失敗: status={res.status_code} body={res.text}"
            )

        body = res.json()
        access_token = body.get("access_token")
        if not access_token:
            raise EbayApiError("OAuth応答に access_token が含まれません")

        _token_cache.token = access_token
        _token_cache.expires_at = now + int(body.get("expires_in", 7200))
        return access_token


def _extract_jpy_price(item: dict[str, Any]) -> int:
    price_obj = item.get("price") or {}
    raw_value = price_obj.get("value")
    currency = price_obj.get("currency", "USD")

    if raw_value is None:
        return 0

    try:
        amount = float(raw_value)
    except (TypeError, ValueError):
        return 0

    return _to_jpy(amount, currency)


def _to_jpy(value: float, currency: str) -> int:
    if currency == "JPY":
        return int(value)
    return int(value * settings.fx_rate_usd_jpy)
