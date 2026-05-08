from pydantic import BaseModel, ConfigDict, Field


class TopProduct(BaseModel):
    id: int = Field(..., description="商品ID")
    title: str = Field(..., description="商品名")
    overall_score: int | None = Field(None, description="総合スコア (0-100)")
    profit_rate: float | None = Field(None, description="利益率 (%)")


class DashboardSummary(BaseModel):
    total_products: int = Field(..., description="DB の全商品数")
    profitable_products: int = Field(..., description="is_profitable=True の数")
    allowed_products: int = Field(..., description="is_allowed=True の数")
    blocked_products: int = Field(..., description="is_allowed=False の数")
    warning_products: int = Field(..., description="monitoring_status=warning の数")
    danger_products: int = Field(..., description="monitoring_status=danger の数")
    average_profit_rate: float = Field(..., description="全商品の平均利益率（%）")
    average_overall_score: float = Field(..., description="全商品の平均 overall_score")
    top_profitable_products: list[TopProduct] = Field(
        ..., description="利益率TOP（既定5件）"
    )
    top_scored_products: list[TopProduct] = Field(
        ..., description="総合スコアTOP（既定5件）"
    )
    today_orders: int = Field(0, description="今日（UTC）に作成された注文数")
    today_sales: float = Field(0.0, description="今日（UTC）の売上合計（USD等の生値）")
    pending_shipments: int = Field(
        0, description="未発送（fulfillment_status が SHIPPED/DELIVERED 以外）の件数"
    )

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "total_products": 1200,
                "profitable_products": 180,
                "allowed_products": 950,
                "blocked_products": 250,
                "warning_products": 23,
                "danger_products": 5,
                "average_profit_rate": 18.5,
                "average_overall_score": 72.1,
                "top_profitable_products": [
                    {
                        "id": 1,
                        "title": "Echo Dot (第4世代)",
                        "overall_score": 91,
                        "profit_rate": 28.5,
                    }
                ],
                "top_scored_products": [
                    {
                        "id": 7,
                        "title": "Kindle Paperwhite",
                        "overall_score": 95,
                        "profit_rate": 25.0,
                    }
                ],
            }
        }
    )
