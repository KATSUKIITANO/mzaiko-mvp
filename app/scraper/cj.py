import logging
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.config import settings
from app.services.cache_service import get_cached, set_cached

logger = logging.getLogger(__name__)

CJ_TOKEN_KEY = "cj:access_token"


class CJError(Exception):
    pass


class CJAuthError(CJError):
    pass


async def _fetch_new_token() -> str:
    if not settings.cj_api_key:
        raise CJAuthError("CJ_API_KEY not configured")

    url = f"{settings.cj_api_base}/authentication/getAccessToken"
    async with httpx.AsyncClient(timeout=settings.cj_request_timeout) as client:
        resp = await client.post(
            url,
            json={"apiKey": settings.cj_api_key},
            headers={"Content-Type": "application/json"},
        )
        resp.raise_for_status()
        body = resp.json()

    if not body.get("result"):
        raise CJAuthError(
            f"CJ token request failed: {body.get('message', 'unknown')}"
        )

    data = body.get("data") or {}
    token = data.get("accessToken")
    if not token:
        raise CJAuthError("CJ accessToken missing in response")

    await set_cached(CJ_TOKEN_KEY, {"token": token}, settings.cj_token_cache_ttl)
    logger.info("cj access token refreshed")
    return token


async def _get_access_token(force_refresh: bool = False) -> str:
    if not force_refresh:
        cached = await get_cached(CJ_TOKEN_KEY)
        if isinstance(cached, dict) and cached.get("token"):
            return cached["token"]
    return await _fetch_new_token()


@retry(
    stop=stop_after_attempt(settings.max_retries),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception_type((httpx.HTTPError, CJError)),
    reraise=True,
)
async def _request(
    method: str,
    path: str,
    params: dict | None = None,
    json_body: dict | None = None,
) -> dict[str, Any]:
    token = await _get_access_token()
    url = f"{settings.cj_api_base}{path}"
    headers = {
        "CJ-Access-Token": token,
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=settings.cj_request_timeout) as client:
        resp = await client.request(
            method, url, params=params, json=json_body, headers=headers
        )

    if resp.status_code == 401:
        token = await _get_access_token(force_refresh=True)
        headers["CJ-Access-Token"] = token
        async with httpx.AsyncClient(timeout=settings.cj_request_timeout) as client:
            resp = await client.request(
                method, url, params=params, json=json_body, headers=headers
            )

    resp.raise_for_status()
    body = resp.json()

    if not body.get("result"):
        raise CJError(f"CJ API error: {body.get('message', 'unknown')}")

    return body.get("data") or {}


async def cj_search(
    keyword: str, page: int = 1, page_size: int | None = None
) -> dict[str, Any]:
    params = {
        "pageNum": page,
        "pageSize": page_size or settings.cj_default_page_size,
        "productNameEn": keyword,
    }
    return await _request("GET", "/product/list", params=params)


async def cj_product_detail(pid: str) -> dict[str, Any]:
    return await _request("GET", "/product/query", params={"pid": pid})


def extract_image_urls(product_detail: dict[str, Any]) -> list[str]:
    raw = product_detail.get("productImageSet") or product_detail.get("productImage")
    if not raw:
        return []
    if isinstance(raw, list):
        return [u for u in raw if u]
    if isinstance(raw, str):
        if ";" in raw:
            return [u.strip() for u in raw.split(";") if u.strip()]
        return [raw]
    return []
