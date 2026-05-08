from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://mzaiko:mzaiko_pass@db:5432/mzaiko_db"
    redis_url: str = "redis://redis:6379/0"
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
    request_timeout: int = 30000
    max_retries: int = 3

    # eBay 共通
    ebay_client_id: str = ""
    ebay_client_secret: str = ""
    ebay_refresh_token: str = ""
    ebay_environment: str = "production"

    # Browse API
    ebay_oauth_scope: str = "https://api.ebay.com/oauth/api_scope"
    ebay_marketplace: str = "EBAY_US"
    ebay_search_limit: int = 50

    # User OAuth
    ebay_oauth_user_scope: str = (
        "https://api.ebay.com/oauth/api_scope/sell.inventory "
        "https://api.ebay.com/oauth/api_scope/sell.fulfillment.readonly"
    )
    ebay_inventory_api_timeout: float = 15.0
    ebay_token_request_timeout: float = 15.0

    # Listing
    ebay_listing_request_timeout: float = 30.0
    ebay_listing_marketplace_id: str = "EBAY_US"
    ebay_listing_currency: str = "USD"
    ebay_listing_category_id: str = ""
    ebay_listing_markup_percent: float = 5.0
    ebay_sku_prefix: str = "mzaiko-"
    ebay_merchant_location_key: str = ""
    ebay_fulfillment_policy_id: str = ""
    ebay_payment_policy_id: str = ""
    ebay_return_policy_id: str = ""
    ebay_listing_max_retries: int = 2
    ebay_max_image_count: int = 12
    listing_copy_provider: str = "template"

    # Auto stop / auto revise
    auto_stop_enabled: bool = True
    auto_revise_price: bool = True
    min_profit_rate: float = 10.0

    # eBay Order Sync
    ebay_order_sync_enabled: bool = True
    ebay_order_sync_interval_minutes: int = 15

    # eBay Buyer Message AI auto-reply
    ebay_message_sync_enabled: bool = True
    ebay_message_sync_interval_minutes: int = 10
    auto_reply_send_enabled: bool = False  # 既定OFF: 人間レビュー前提

    # eBay Auto Repricing
    auto_repricing_enabled: bool = True
    auto_repricing_interval_minutes: int = 60
    auto_repricing_dry_run: bool = False
    auto_repricing_batch_limit: int = 200

    # Revise Worker
    revise_worker_enabled: bool = True
    revise_worker_interval_minutes: int = 10
    revise_worker_batch_limit: int = 50
    revise_worker_concurrency: int = 3
    revise_worker_dry_run: bool = False
    out_of_stock_action: str = "set_zero"
    min_profit_yen: int = 500
    min_profit_margin_percent: float = 10.0

    # Sold Sync Worker
    sold_sync_enabled: bool = True
    sold_sync_interval_minutes: int = 5
    sold_sync_concurrency: int = 3
    sold_sync_lookback_hours: int = 24
    sold_sync_page_limit: int = 200
    sold_sync_request_timeout: float = 30.0
    sold_sync_max_retries: int = 2
    sold_sync_dry_run: bool = False

    # 配送見積
    shipping_volumetric_divisor: int = 5000
    shipping_default_weight_gram: int = 500
    shipping_default_length_cm: int = 25
    shipping_default_width_cm: int = 18
    shipping_default_height_cm: int = 5

    # 利益バッファ
    fx_buffer_percent: float = 2.0
    refund_buffer_percent: float = 3.0

    # Country Risk（CSV 空ならデフォルト使用）
    risk_blocked_countries: str = ""
    risk_high_countries: str = ""
    risk_medium_countries: str = ""
    risk_buffer_low_percent: float = 3.0
    risk_buffer_medium_percent: float = 7.0
    risk_buffer_high_percent: float = 15.0

    # 通貨換算
    fx_rate_usd_jpy: float = 150.0

    # 利益判定
    profit_threshold: float = 15.0
    default_shipping_cost: int = 2000
    ebay_fee_percent: float = 0.13

    # 一括解析
    max_concurrent_analysis: int = 3
    bulk_analyze_timeout_seconds: int = 120
    bulk_analyze_max_retries: int = 2
    bulk_analyze_max_urls: int = 500

    # キャッシュ
    amazon_cache_ttl: int = 3600
    ebay_cache_ttl: int = 1800

    # 定期監視
    monitor_interval_minutes: int = 10
    monitor_batch_size: int = 50
    monitor_enabled: bool = True

    # Slack
    slack_webhook_url: str = ""
    slack_notifications_enabled: bool = True
    slack_request_timeout_seconds: int = 10
    slack_max_retries: int = 2

    # Discord（optional）
    discord_webhook_url: str = ""

    # 需要スコアリング
    score_profit_weight: float = 0.5
    score_demand_weight: float = 0.3
    score_competition_weight: float = 0.2
    score_threshold: int = 70

    # リスクフィルタ（VeRO/Hazmat）
    risk_filter_enabled: bool = True
    risk_vero_brands: str = ""
    risk_hazmat_keywords: str = ""

    # Amazonランキング
    ranking_cache_ttl: int = 3600
    ranking_page_delay_seconds: float = 2.0
    ranking_max_pages: int = 2
    ranking_default_limit: int = 50

    # API Key 認証
    api_key: str = ""
    api_auth_enabled: bool = True

    # CJ Dropshipping
    cj_api_key: str = ""
    cj_api_base: str = "https://developers.cjdropshipping.com/api2.0/v1"
    cj_token_cache_ttl: int = 1036800
    cj_request_timeout: float = 30.0
    cj_default_page_size: int = 20

    # Rate Limit
    rate_limit_enabled: bool = True
    rate_limit_analyze_per_minute: int = 20
    rate_limit_bulk_per_hour: int = 10
    rate_limit_ranking_per_10min: int = 20

    # ログ
    log_level: str = "INFO"

    @property
    def ebay_api_base(self) -> str:
        if self.ebay_environment.lower() == "sandbox":
            return "https://api.sandbox.ebay.com"
        return "https://api.ebay.com"


settings = Settings()
