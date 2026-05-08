from pydantic import BaseModel, Field, HttpUrl

from app.config import settings


class BulkAnalyzeRequest(BaseModel):
    amazon_urls: list[HttpUrl] = Field(
        ...,
        min_length=1,
        max_length=settings.bulk_analyze_max_urls,
        description="Amazon商品URLリスト",
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "amazon_urls": [
                    "https://www.amazon.co.jp/dp/B08N5WRWNW",
                    "https://www.amazon.co.jp/dp/B0BJL1HW4Y",
                ]
            }
        }
    }


class BulkAnalyzeItemResult(BaseModel):
    title: str = Field(..., description="商品名")
    profit: int = Field(..., description="利益（円）")
    profit_rate: float = Field(..., description="利益率（%）")
    profit_score: int = Field(..., description="利益スコア (0-100)")
    demand_score: int = Field(..., description="需要スコア (0-100)")
    competition_score: int = Field(..., description="競合スコア (0-100)")
    overall_score: int = Field(..., description="総合スコア (0-100)")
    is_profitable: bool = Field(..., description="overall_score >= SCORE_THRESHOLD")
    risk_level: str = Field(..., description="high / low")
    risk_reason: list[str] = Field(default_factory=list, description="reasonコード")
    is_allowed: bool = Field(..., description="リスクフィルタを通過したか")


class BulkAnalyzeResponse(BaseModel):
    total_count: int = Field(..., description="リクエスト総数")
    success_count: int = Field(..., description="解析成功件数")
    profitable_count: int = Field(..., description="利益商品件数")
    blocked_count: int = Field(..., description="リスクフィルタで除外された件数")
    failed_count: int = Field(..., description="失敗件数")
    results: list[BulkAnalyzeItemResult]

    model_config = {
        "json_schema_extra": {
            "example": {
                "total_count": 100,
                "success_count": 90,
                "profitable_count": 12,
                "blocked_count": 14,
                "failed_count": 10,
                "results": [
                    {
                        "title": "Nintendo Switch",
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
                ],
            }
        }
    }
