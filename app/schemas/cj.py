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


class CJParcelResult(BaseModel):
    """小包1つ分の発注結果（通関対策で分割された場合は複数返る）。"""

    success: bool
    skipped: bool = False
    reason: str | None = None
    split_index: int = 0
    split_total: int = 1
    dry_run: bool = False
    ebay_order_id: str
    cj_order_id: str | None = None
    cj_order_num: str | None = None
    status: str
    cj_status: str | None = None
    quantity: int = 1
    customs_risk_level: str | None = None
    tracking_number: str | None = None
    tracking_pushed: bool = False
    attempts: int = 0
    request_payload: dict | list | None = None
    error: str | None = None


class CJOrderPlaceResponse(BaseModel):
    """eBay 注文1件分の発注結果（全小包のまとめ）。"""

    success: bool
    skipped: bool = False
    reason: str | None = None
    dry_run: bool = False
    ebay_order_id: str
    status: str
    parcel_count: int = 0
    split: bool = False
    customs_risk_level: str | None = None
    total_quantity: int = 0
    cj_order_ids: list[str] = Field(default_factory=list)
    parcels: list[CJParcelResult] = Field(default_factory=list)
    error: str | None = None


class CJOrderItem(BaseModel):
    """CJ 発注の一覧・詳細表示。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    order_id: int
    ebay_order_id: str
    split_index: int
    split_total: int
    customs_risk_level: str | None
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


class CustomsRiskCheckResponse(BaseModel):
    """出品前・発注前に通関リスクを確認する。"""

    title: str
    level: str
    reasons: list[str] = Field(default_factory=list)
    matched_keyword: str | None = None
    max_units_per_parcel: int
    quantity: int
    parcel_split: list[int] = Field(
        default_factory=list, description="その数量を発注した場合の小包ごとの個数"
    )
