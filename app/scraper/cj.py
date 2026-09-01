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


# ─────────────────────────────────────
# Variant（発注単位）
# ─────────────────────────────────────
async def cj_variant_list(pid: str) -> list[dict[str, Any]]:
    """商品の variant 一覧を取得（発注に必要な vid を得る）。"""
    data = await _request("GET", "/product/variant/query", params={"pid": pid})
    if isinstance(data, list):
        return [v for v in data if isinstance(v, dict)]
    if isinstance(data, dict):
        for key in ("list", "variants", "content"):
            raw = data.get(key)
            if isinstance(raw, list):
                return [v for v in raw if isinstance(v, dict)]
    return []


def extract_variant_id(variants: list[dict[str, Any]]) -> str | None:
    """variant 一覧から先頭の vid を取り出す（キー表記ゆれを吸収）。"""
    for v in variants:
        for key in ("vid", "variantId", "variantID", "id"):
            vid = v.get(key)
            if vid:
                return str(vid)
    return None


# ─────────────────────────────────────
# Order（eBay 受注 → CJ 発注）
# ─────────────────────────────────────
async def cj_create_order(payload: dict[str, Any]) -> dict[str, Any]:
    """CJ に注文を作成する（createOrderV2）。

    payload は cj_order_service._build_order_payload() が組み立てる。
    支払いは行わない（CJ 側で未払い注文として残り、人間が確認できる）。
    """
    return await _request("POST", "/shopping/order/createOrderV2", json_body=payload)


async def cj_order_detail(cj_order_id: str) -> dict[str, Any]:
    """CJ 注文の詳細（ステータス・追跡番号）を取得。"""
    return await _request(
        "GET", "/shopping/order/getOrderDetail", params={"orderId": cj_order_id}
    )


def extract_cj_order_id(created: dict[str, Any]) -> str | None:
    for key in ("orderId", "orderID", "id"):
        value = created.get(key)
        if value:
            return str(value)
    return None


def extract_cj_order_num(created: dict[str, Any]) -> str | None:
    for key in ("orderNum", "orderNumber", "cjOrderNum"):
        value = created.get(key)
        if value:
            return str(value)
    return None


def extract_tracking_number(detail: dict[str, Any]) -> str | None:
    """注文詳細から追跡番号を取り出す（トップレベル → 明細の順に探索）。"""
    for key in ("trackNumber", "trackingNumber", "logisticTrackingNumber"):
        value = detail.get(key)
        if value:
            return str(value)

    items = detail.get("productList") or detail.get("orderProductList") or []
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            for key in ("trackNumber", "trackingNumber"):
                value = item.get(key)
                if value:
                    return str(value)
    return None
