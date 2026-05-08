"""Rate Limiting via Redis sorted-set sliding window。

設計:
  - Redis ZSET (score=timestamp, member=unique id) で時間窓内の件数を管理
  - Lua script で「expire削除 + 件数チェック + 追加」を atomic に実行
  - dependency factory `make_rate_limit_dependency()` で route ごとに生成
  - Redis 障害時は fail-open（許可 + WARNING ログ）

将来拡張ポイント:
  - user tier化  : `_resolve_limit(api_key)` を挿入し tier 別 limit を返却
  - paid plan    : 上記を DB lookup に拡張
  - admin bypass : api_key の prefix チェックで dependency 冒頭で return
  - dynamic quota: limit を DB / Redis から取得
  - usage stats  : ZADD 後に別の ZSET にもメトリクスを書く
"""
import hashlib
import logging
import math
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import Header, HTTPException, Request, status
from redis.exceptions import RedisError

from app.config import settings
from app.services.cache_service import get_redis_client

logger = logging.getLogger(__name__)


# ── Redis key の名前空間（他キャッシュと衝突しない）
RATE_LIMIT_KEY_PREFIX: str = "ratelimit"


# ── Lua script: sliding window をアトミックに評価
# returns: {allowed_int, oldest_score_str, count_int}
SLIDING_WINDOW_SCRIPT: str = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local cutoff = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local window = tonumber(ARGV[4])
local member = ARGV[5]

redis.call('ZREMRANGEBYSCORE', key, '-inf', cutoff)
local count = tonumber(redis.call('ZCARD', key))

if count >= limit then
    local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
    local oldest_score = '0'
    if oldest[2] then
        oldest_score = oldest[2]
    end
    return {0, oldest_score, count}
end

redis.call('ZADD', key, now, member)
redis.call('EXPIRE', key, window + 1)
return {1, '0', count + 1}
"""


# ── public helpers ─────────────────
def get_client_ip(request: Request) -> str:
    """X-Forwarded-For 優先で client IP を取得。"""
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        first = xff.split(",")[0].strip()
        if first:
            return first
    xri = request.headers.get("X-Real-IP")
    if xri:
        s = xri.strip()
        if s:
            return s
    if request.client:
        return request.client.host
    return "unknown"


def build_rate_limit_key(route_name: str, ip: str, api_key: str | None) -> str:
    """{prefix}:{route}:{ip}:{api_key_hash16} 形式の Redis key を生成。

    api_key は sha256 16桁に短縮。生キーをRedisキー上に残さない。
    """
    if api_key:
        api_key_hash = hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]
    else:
        api_key_hash = "anon"
    return f"{RATE_LIMIT_KEY_PREFIX}:{route_name}:{ip}:{api_key_hash}"


# ── core check ─────────────────────
async def check_window(
    *,
    key: str,
    limit: int,
    window_seconds: int,
) -> tuple[bool, int]:
    """Returns (allowed, retry_after_seconds)."""
    client = get_redis_client()
    if client is None:
        logger.warning(
            "rate limit fail-open: redis unavailable key=%s", key
        )
        return True, 0

    now = time.time()
    cutoff = now - window_seconds
    member = f"{now}:{uuid.uuid4().hex}"

    try:
        result = await client.eval(
            SLIDING_WINDOW_SCRIPT,
            1,
            key,
            now,
            cutoff,
            limit,
            window_seconds,
            member,
        )
    except RedisError as e:
        logger.warning(
            "rate limit fail-open (RedisError) key=%s err=%s", key, e
        )
        return True, 0
    except Exception as e:
        logger.warning(
            "rate limit fail-open (unexpected) key=%s err=%s", key, e
        )
        return True, 0

    allowed_raw, oldest_score_raw, count_raw = result
    allowed = int(allowed_raw) == 1
    count = int(count_raw)

    if allowed:
        logger.debug(
            "rate limit ok key=%s count=%d/%d", key, count, limit
        )
        return True, 0

    try:
        oldest_score = float(oldest_score_raw)
    except (TypeError, ValueError):
        oldest_score = now

    retry_after = max(1, math.ceil(oldest_score + window_seconds - now))
    return False, retry_after


# ── dependency factory ─────────────
def make_rate_limit_dependency(
    *,
    route_name: str,
    limit: int,
    window_seconds: int,
) -> Callable[..., Awaitable[None]]:
    """指定 (route, limit, window) を持つ FastAPI dependency を生成。"""

    async def dependency(
        request: Request,
        x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
    ) -> None:
        if not settings.rate_limit_enabled:
            return

        ip = get_client_ip(request)
        key = build_rate_limit_key(route_name, ip, x_api_key)

        allowed, retry_after = await check_window(
            key=key, limit=limit, window_seconds=window_seconds,
        )

        if not allowed:
            logger.warning(
                "rate limit exceeded route=%s ip=%s limit=%d window=%ds "
                "retry_after=%ds",
                route_name, ip, limit, window_seconds, retry_after,
            )
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="rate limit exceeded",
                headers={"Retry-After": str(retry_after)},
            )

    return dependency


# ── Pre-built dependencies ─────────
rate_limit_analyze_product = make_rate_limit_dependency(
    route_name="analyze_product",
    limit=settings.rate_limit_analyze_per_minute,
    window_seconds=60,
)

rate_limit_bulk_analyze = make_rate_limit_dependency(
    route_name="bulk_analyze",
    limit=settings.rate_limit_bulk_per_hour,
    window_seconds=3600,
)

rate_limit_amazon_ranking = make_rate_limit_dependency(
    route_name="amazon_ranking",
    limit=settings.rate_limit_ranking_per_10min,
    window_seconds=600,
)
