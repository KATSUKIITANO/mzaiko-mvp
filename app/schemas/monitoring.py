from pydantic import BaseModel, ConfigDict, Field


class MonitoringProduct(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int = Field(..., description="商品ID")
    title: str = Field(..., description="商品名")
    price: int = Field(..., description="Amazon価格（円）")
    selling_price: int | None = Field(None, description="eBay販売価格（円）")
    profit: int | None = Field(None, description="利益（円・current優先）")
    current_profit_rate: float | None = Field(None, description="最新の利益率（%）")
    monitoring_status: str = Field(..., description="ok / warning / danger")

    profit_score: int | None = Field(None, description="利益スコア (0-100)")
    demand_score: int | None = Field(None, description="需要スコア (0-100)")
    competition_score: int | None = Field(None, description="競合スコア (0-100)")
    overall_score: int | None = Field(None, description="総合スコア (0-100)")

    risk_level: str = Field(..., description="high / low")
    risk_reason: list[str] | None = Field(None, description="リスク該当理由コード")
    is_profitable: bool = Field(..., description="利益商品か")
    is_allowed: bool = Field(..., description="リスクフィルタを通過したか")


class HealthResponse(BaseModel):
    overall: str
    db: dict
    redis: dict
    scheduler: dict
    checked_at: str


class MetricsResponse(BaseModel):
    window_hours: int
    publish: dict
    sync: dict
    repricing: dict
    orders: dict
    messages: dict


class AlertRequest(BaseModel):
    message: str = Field(..., min_length=1)
    severity: str = Field(default="info", description="info / warning / error")


class AlertResponse(BaseModel):
    sent: bool
    reason: str | None = None
    delivery: list[dict] | None = None
