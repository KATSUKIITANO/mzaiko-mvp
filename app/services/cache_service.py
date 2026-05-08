import hashlib
import json
import logging
from typing import Any

import redis.asyncio as redis_async
from redis.exceptions import RedisError

from app.config import settings

logger = logging.getLogger(__name__)

_redis_client: redis_async.Redis | None = None


async def init_redis() -> None:
    """アプリ起動時に呼ぶ。接続失敗してもアプリは起動継続（キャッシュ無効化）。"""
    global _redis_client
    try:
        client = redis_async.from_url(
            settings.redis_url,
            encoding="utf-8",
            decode_responses=True,
        )
        await client.ping()
        _redis_client = client
        logger.info("redis connected url=%s", settings.redis_url)
    except RedisError as e:
        logger.warning(
            "redis connect failed (cache disabled, fallback mode): %s", e
        )
        _redis_client = None
    except Exception as e:
        logger.warning(
            "redis init unexpected error (cache disabled): %s", e
        )
        _redis_client = None


async def close_redis() -> None:
    global _redis_client
    if _redis_client is None:
        return
    try:
        await _redis_client.aclose()
        logger.info("redis closed")
    except Exception as e:
        logger.warning("redis close error: %s", e)
    finally:
        _redis_client = None


def get_redis_client() -> redis_async.Redis | None:
    """他サービス（rate_limit_service など）から共有 Redis client を取得する。

    起動前に呼ぶと None。Redis 障害時にも None。
    呼び出し側は None を fail-open で扱うこと。
    """
    return _redis_client


def make_cache_key(prefix: str, raw_value: str) -> str:
    h = hashlib.sha256(raw_value.encode("utf-8")).hexdigest()
    return f"{prefix}:{h}"


async def get_cached(key: str) -> Any | None:
    if _redis_client is None:
        return None
    try:
        raw = await _redis_client.get(key)
    except RedisError as e:
        logger.warning("redis GET failed key=%s err=%s", key, e)
        return None
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError) as e:
        logger.warning("redis JSON decode failed key=%s err=%s", key, e)
        return None


async def set_cached(key: str, value: Any, ttl_seconds: int) -> None:
    if _redis_client is None:
        return
    try:
        payload = json.dumps(value, ensure_ascii=False, default=_json_default)
    except (TypeError, ValueError) as e:
        logger.warning("redis JSON encode failed key=%s err=%s", key, e)
        return
    try:
        await _redis_client.set(key, payload, ex=ttl_seconds)
    except RedisError as e:
        logger.warning("redis SET failed key=%s err=%s", key, e)


def _json_default(obj: Any) -> Any:
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if hasattr(obj, "__str__"):
        return str(obj)
    raise TypeError(
        f"Object of type {type(obj).__name__} is not JSON serializable"
    )
