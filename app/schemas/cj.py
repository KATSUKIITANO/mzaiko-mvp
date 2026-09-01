from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class CJMappingUpsertRequest(BaseModel):
    """SKU ↔ CJ 商品の手動紐付け（既存出品の後追い登録用）。"""

    sku: str = Field(..., min_length=1, description="eBay SKU")
    cj_product_id: str = Field(..., min_length=1, description="CJ product id (pid)")
    cj_variant_id: str | None = Field(
        None, description="CJ variant id (vid)。省略時は発注時に自動解決"
    )
    product_id: int | None = None
    cj_sku: str | None = None
    note: str | None = None

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "sku": "mzaiko-1",
                "cj_product_id": "123456",
                "cj_variant_id": "789012",
            }
        }
    )


class CJMappingItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    sku: str
    product_id: int | None
    cj_product_id: str
    cj_variant_id: str | None
    cj_sku: str | None
    source: str
    note: str | None
    created_at: datetime
    updated_at: datetime


class CJOrderPlaceRequest(BaseModel):
    dry_run: bool | None = Field(
        None, description="未指定なら CJ_ORDER_DRY_RUN 設定に従う"
    )
    force: bool = Field(
        default=False, description="失敗回数の上限を無視して再発注する"
    )


class CJOrderPlaceResponse(BaseModel):
    success: bool
    skipped: bool = False
    reason: str | None = None
    dry_run: bool = False
    ebay_order_id: str
    cj_order_id: str | None = None
    cj_order_num: str | None = None
    status: str
    cj_status: str | None = None
    tracking_number: str | None = None
    tracking_pushed: bool = False
    attempts: int = 0
    request_payload: dict | list | None = None
    error: str | None = None


class CJOrderItem(BaseModel):
    """CJ 発注の一覧・詳細表示。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    order_id: int
    ebay_order_id: str
    sku: str | None
    cj_order_id: str | None
    cj_order_num: str | None
    cj_status: str | None
    status: str
    quantity: int
    shipping_country: str | None
    logistic_name: str | None
    tracking_number: str | None
    total_amount: str | None
    dry_run: bool
    attempts: int
    error_message: str | None
    placed_at: datetime | None
    last_synced_at: datetime | None
    created_at: datetime
    updated_at: datetime


class CJOrderWorkerMetrics(BaseModel):
    """CJ 発注ワーカーの直近実行メトリクス。"""

    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_seconds: float | None = None
    placed: int = 0
    place_failed: int = 0
    unmapped: int = 0
    synced: int = 0
    shipped: int = 0
    tracking_pushed: int = 0
    sync_failed: int = 0
