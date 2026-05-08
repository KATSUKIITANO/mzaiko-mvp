"""eBay User OAuth (Refresh Token Flow) + Inventory API ping。

Browse API の App-Token (cache_service 内 _TokenCache) とは独立。
将来 Trading API / Sell API を追加する際もこのサービスから access_token を
得るパターンで再利用できる構造にしている。

スコープ:
  - get_access_token() : refresh_token → access_token
  - ping_inventory()   : Sell Inventory API getInventoryItems を1回呼んで疎通確認
"""
import asyncio
import base64
import logging
import time

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.config import settings

logger = logging.getLogger(__name__)


class EbayAuthError(Exception):
    pass


class _AccessTokenCache:
    def __init__(self) -> None:
        self.token: str | None = None
        self.expires_at: float = 0.0
        self.lock: asyncio.Lock = asyncio.Lock()


_cache = _AccessTokenCache()


# ────────────────────────────────────
# OAuth: refresh_token → access_token
# ────────────────────────────────────
@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=5),
    retry=retry_if_exception_type((httpx.HTTPError,)),
    reraise=True,
)
async def _request_refresh_token() -> dict:
    """refresh_token を使って新しい access_token を取得する。"""
    if not settings.ebay_refresh_token:
        raise EbayAuthError("EBAY_REFRESH_TOKEN が未設定です")
    if not settings.ebay_client_id or not settings.ebay_client_secret:
        raise EbayAuthError(
            "EBAY_CLIENT_ID / EBAY_CLIENT_SECRET が未設定です"
        )

    creds = base64.b64encode(
        f"{settings.ebay_client_id}:{settings.ebay_client_secret}".encode()
    ).decode()

    url = f"{settings.ebay_api_base}/identity/v1/oauth2/token"
    headers = {
        "Authorization": f"Basic {creds}",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    data = {
        "grant_type": "refresh_token",
        "refresh_token": settings.ebay_refresh_token,
        "scope": settings.ebay_oauth_user_scope,
    }

    logger.info(
        "ebay refresh token request env=%s url=%s",
        settings.ebay_environment, url,
    )

    async with httpx.AsyncClient(
        timeout=settings.ebay_token_request_timeout
    ) as client:
        res = await client.post(url, headers=headers, data=data)

    if res.status_code != 200:
        raise EbayAuthError(
            f"refresh token 失敗: status={res.status_code} body={res.text}"
        )

    body = res.json()
    if not body.get("access_token"):
        raise EbayAuthError("refresh 応答に access_token が含まれていません")
    return body


async def get_access_token() -> str:
    """access_token をキャッシュ優先で返す。期限切れ60秒前に refresh。"""
    async with _cache.lock:
        now = time.time()
        if _cache.token and _cache.expires_at > now + 60:
            return _cache.token

        body = await _request_refresh_token()
        token = body["access_token"]
        expires_in = int(body.get("expires_in", 7200))

        _cache.token = token
        _cache.expires_at = now + expires_in
        logger.info(
            "ebay access_token refreshed expires_in=%ds env=%s",
            expires_in, settings.ebay_environment,
        )
        return token


def invalidate_token_cache() -> None:
    """401 を受けた時などに強制的に再取得させるためにキャッシュを破棄。"""
    _cache.token = None
    _cache.expires_at = 0.0


# ────────────────────────────────────
# Inventory API ping
# ────────────────────────────────────
async def ping_inventory() -> dict:
    """Sell Inventory API の getInventoryItems を 1 回叩いて疎通確認する。

    401 時は token cache を破棄して 1 回だけリトライ。
    その他 HTTP エラーも 1 回だけリトライ。
    """
    last_error: Exception | None = None
    max_attempts = 2

    for attempt in range(1, max_attempts + 1):
        try:
            token = await get_access_token()
            return await _do_ping(token)
        except _Unauthorized:
            logger.warning(
                "ebay ping 401 (attempt=%d) → invalidate token cache",
                attempt,
            )
            invalidate_token_cache()
            last_error = EbayAuthError("Inventory API: 401 Unauthorized")
        except httpx.HTTPError as e:
            logger.warning(
                "ebay ping http error attempt=%d err=%s", attempt, e
            )
            last_error = e
            await asyncio.sleep(1)
        except EbayAuthError:
            raise

    raise EbayAuthError(f"ping 最終失敗: {last_error}")


async def _do_ping(token: str) -> dict:
    url = f"{settings.ebay_api_base}/sell/inventory/v1/inventory_item"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    params = {"limit": 1, "offset": 0}

    logger.info(
        "ebay ping start env=%s url=%s",
        settings.ebay_environment, url,
    )

    async with httpx.AsyncClient(
        timeout=settings.ebay_inventory_api_timeout
    ) as client:
        res = await client.get(url, headers=headers, params=params)

    if res.status_code == 401:
        raise _Unauthorized()

    if res.status_code != 200:
        raise EbayAuthError(
            f"Inventory API 応答エラー: status={res.status_code} body={res.text}"
        )

    body = res.json()
    total = int(body.get("total", 0) or 0)

    logger.info(
        "ebay ping ok env=%s inventory_total=%d",
        settings.ebay_environment, total,
    )

    return {
        "ok": True,
        "environment": settings.ebay_environment,
        "inventory_count": total,
    }


class _Unauthorized(Exception):
    """内部用: 401 を捕捉して上位でリトライさせるためのマーカ例外。"""
    pass
