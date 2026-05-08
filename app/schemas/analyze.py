from pydantic import BaseModel, Field, HttpUrl


class AnalyzeRequest(BaseModel):
    amazon_url: HttpUrl = Field(..., description="Amazon商品URL")

    model_config = {
        "json_schema_extra": {
            "example": {"amazon_url": "https://www.amazon.co.jp/dp/B08N5WRWNW"}
        }
    }


class AnalyzeResponse(BaseModel):
    amazon_title: str = Field(..., description="Amazon商品名")
    amazon_price: int = Field(..., description="Amazon仕入価格（円）")
    ebay_average_price: int = Field(..., description="eBay平均販売価格（円）")
    platform_fee: int = Field(..., description="プラットフォーム手数料（円）")
    shipping_cost: int = Field(..., description="送料（円）")
    profit: int = Field(..., description="利益（円）")
    profit_rate: float = Field(..., description="利益率（%）")

    profit_score: int = Field(..., description="利益スコア (0-100)")
    demand_score: int = Field(..., description="需要スコア (0-100)")
    competition_score: int = Field(..., description="競合スコア (0-100)")
    overall_score: int = Field(..., description="総合スコア (0-100)")

    is_profitable: bool = Field(..., description="overall_score >= SCORE_THRESHOLD")

    risk_level: str = Field(..., description="high / low")
    risk_reason: list[str] = Field(default_factory=list, description="ヒットしたreasonコード")
    is_allowed: bool = Field(..., description="リスクフィルタを通過したか")

    model_config = {
        "json_schema_extra": {
            "example": {
                "amazon_title": "Nintendo Switch 本体",
                "amazon_price": 12000,
                "ebay_average_price": 0,
                "platform_fee": 0,
                "shipping_cost": 2000,
                "profit": 0,
                "profit_rate": 0.0,
                "profit_score": 0,
                "demand_score": 0,
                "competition_score": 0,
                "overall_score": 0,
                "is_profitable": False,
                "risk_level": "high",
                "risk_reason": ["vero_brand"],
                "is_allowed": False,
            }
        }
    }
