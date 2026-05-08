import logging

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.config import settings

logger = logging.getLogger(__name__)


class SlackError(Exception):
    pass


def _retry_decorator():
    return retry(
        stop=stop_after_attempt(max(1, settings.slack_max_retries + 1)),
        wait=wait_exponential(multiplier=1, min=1, max=5),
        retry=retry_if_exception_type((httpx.HTTPError, SlackError)),
        reraise=True,
    )


@_retry_decorator()
async def post_to_webhook(text: str) -> None:
    """Slack Incoming Webhook へ Markdown テキストを投稿する。

    失敗時は SlackError を raise（呼び出し側で graceful fallback 制御）。
    """
    if not settings.slack_webhook_url:
        raise SlackError("SLACK_WEBHOOK_URL が未設定")

    payload = {"text": text, "mrkdwn": True}

    try:
        async with httpx.AsyncClient(
            timeout=settings.slack_request_timeout_seconds
        ) as client:
            res = await client.post(settings.slack_webhook_url, json=payload)
    except httpx.HTTPError as e:
        raise SlackError(f"Slack webhook 通信失敗: {e}")

    if res.status_code >= 400:
        raise SlackError(
            f"Slack webhook 応答エラー: status={res.status_code} body={res.text}"
        )

    logger.debug("slack post ok status=%d", res.status_code)
