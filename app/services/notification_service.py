import logging

from app.config import settings
from app.db.models import MonitoringStatus
from app.services.slack_service import SlackError, post_to_webhook

logger = logging.getLogger(__name__)

NOTIFY_TARGETS = {MonitoringStatus.WARNING.value, MonitoringStatus.DANGER.value}


async def notify_status_change(
    *,
    product_id: int,
    title: str,
    amazon_price: int,
    ebay_price: int | None,
    current_profit: int | None,
    current_profit_rate: float | None,
    old_status: str,
    new_status: str,
    amazon_url: str,
) -> None:
    """monitoring_status が warning/danger に変化した時のみ Slack 通知。

    - 同一 status 連続 → 何もしない
    - new_status が ok（回復） → 通知しない
    - 通信失敗 → warning ログのみ（呼び出し側を止めない）
    """
    if not settings.slack_notifications_enabled:
        logger.debug("slack notifications disabled id=%d", product_id)
        return

    if old_status == new_status:
        return

    if new_status not in NOTIFY_TARGETS:
        logger.info(
            "skip notify (recovery) id=%d %s -> %s",
            product_id, old_status, new_status,
        )
        return

    text = _build_message(
        title=title,
        amazon_price=amazon_price,
        ebay_price=ebay_price,
        current_profit=current_profit,
        current_profit_rate=current_profit_rate,
        new_status=new_status,
        amazon_url=amazon_url,
    )

    try:
        await post_to_webhook(text)
        logger.info(
            "slack notified id=%d status=%s -> %s",
            product_id, old_status, new_status,
        )
    except SlackError as e:
        logger.warning(
            "slack notify failed (graceful) id=%d err=%s",
            product_id, e,
        )
    except Exception as e:
        logger.warning(
            "slack notify unexpected (graceful) id=%d err=%s",
            product_id, e,
        )


def _build_message(
    *,
    title: str,
    amazon_price: int,
    ebay_price: int | None,
    current_profit: int | None,
    current_profit_rate: float | None,
    new_status: str,
    amazon_url: str,
) -> str:
    icon = "🚨" if new_status == MonitoringStatus.DANGER.value else "⚠️"

    rate_str = (
        f"{current_profit_rate}%"
        if current_profit_rate is not None
        else "N/A"
    )
    profit_str = (
        f"{current_profit} JPY"
        if current_profit is not None
        else "N/A"
    )
    ebay_str = f"{ebay_price} JPY" if ebay_price is not None else "N/A"

    return (
        f"{icon} *Profit Alert*\n"
        f"\n"
        f"*Title:*\n{title}\n"
        f"\n"
        f"*Profit Rate:*\n{rate_str}\n"
        f"\n"
        f"*Status:*\n{new_status}\n"
        f"\n"
        f"*Amazon:*\n{amazon_price} JPY\n"
        f"\n"
        f"*eBay:*\n{ebay_str}\n"
        f"\n"
        f"*Profit:*\n{profit_str}\n"
        f"\n"
        f"*URL:*\n<{amazon_url}>"
    )
