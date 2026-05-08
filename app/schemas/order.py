from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class OrderListItem(BaseModel):
    """軽量一覧表示用（raw_payload なし）"""
    model_config = ConfigDict(from_attributes=True)

    id: int
    ebay_order_id: str
    listing_id: int | None
    sku: str
    quantity: int
    sold_price: str
    sold_currency: str
    buyer_country: str | None
    order_status: str
    ebay_created_at: datetime | None
    created_at: datetime


class OrderDetail(BaseModel):
    """詳細表示（raw_payload を含む）"""
    model_config = ConfigDict(from_attributes=True)

    id: int
    ebay_order_id: str
    listing_id: int | None
    sku: str
    quantity: int
    sold_price: str
    sold_currency: str
    buyer_country: str | None
    order_status: str
    ebay_created_at: datetime | None
    created_at: datetime
    updated_at: datetime
    raw_payload: dict | list | None = None


class OrderSyncResponse(BaseModel):
    imported: int
    updated: int
    skipped: int


class TrackingUploadRequest(BaseModel):
    tracking_number: str = Field(..., min_length=1)
    carrier_code: str = Field(..., min_length=1, description="例: USPS / FEDEX / DHL / JP")
    upload_to_ebay: bool = Field(default=True, description="false なら DB のみ更新")


class TrackingUploadResponse(BaseModel):
    success: bool
    ebay_order_id: str
    tracking_number: str
    carrier: str
    shipped_at: str
    ebay_upload: dict


class SoldSyncMetricsResponse(BaseModel):
    """sold sync worker の直近実行メトリクス。"""
    last_run_started_at: datetime | None = None
    last_run_finished_at: datetime | None = None
    duration_seconds: float | None = None
    orders_fetched: int = 0
    sold_detected: int = 0
    order_created: int = 0
    duplicate_skipped: int = 0
    unknown_sku: int = 0
    failed: int = 0

    # DB 全体集計
    total_orders: int = Field(0, description="orders テーブル総件数")
    total_detected: int = 0
    total_processing: int = 0
    total_completed: int = 0
    total_failed: int = 0
