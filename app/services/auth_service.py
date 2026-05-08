"""API Key 認証 dependency。

FastAPI の middleware ではなく Depends として注入する設計。
private_router の `dependencies=[Depends(verify_api_key)]` で
router レベルに一括適用、public_router は素のまま使う。

将来拡張ポイント:
  - multi-key 化 → `expected = settings.api_key` を `expected_keys = [...]` に変更
  - JWT/user 認証 → 並行する `get_current_user` dependency を追加
  - admin role  → AuthContext 戻り値型を導入し各 route で `Depends(get_current_user)`
  - rate limit  → 認証成功後に rate limiter dependency を chain
"""
import logging
import secrets
from typing import Annotated

from fastapi import Header, HTTPException, status

from app.config import settings

logger = logging.getLogger(__name__)


_UNAUTHORIZED_DETAIL: str = "invalid api key"


async def verify_api_key(
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> None:
    """X-API-Key ヘッダを検証し、不一致なら 401 で短絡する。

    成功時は何もせずに return（None）。
    失敗時は HTTPException(401, detail="invalid api key") を raise。
    """
    if not settings.api_auth_enabled:
        # 開発時など意図的に認証をオフにする運用
        return

    expected = settings.api_key

    if not expected:
        # auth有効なのにkey未設定 → 設定不備。fail-secure で全拒否。
        logger.error(
            "auth misconfigured: API_AUTH_ENABLED=true but API_KEY is empty"
        )
        _raise_unauthorized()

    if x_api_key is None:
        logger.warning("auth failed: X-API-Key header missing")
        _raise_unauthorized()

    if not _constant_time_equals(x_api_key, expected):
        # 値そのものはログに出さない。長さだけ記録。
        logger.warning(
            "auth failed: invalid X-API-Key (received_len=%d)",
            len(x_api_key),
        )
        _raise_unauthorized()

    # 成功時は静か（高頻度アクセスでログが肥大化するのを避ける）


def _constant_time_equals(a: str, b: str) -> bool:
    """secrets.compare_digest による定数時間比較。timing attack 対策。"""
    return secrets.compare_digest(a, b)


def _raise_unauthorized() -> None:
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=_UNAUTHORIZED_DETAIL,
    )
