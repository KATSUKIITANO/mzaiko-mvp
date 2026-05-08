from pydantic import BaseModel, Field


class EbaySearchRequest(BaseModel):
    keyword: str = Field(..., min_length=1, description="検索キーワード（商品タイトル）")

    model_config = {
        "json_schema_extra": {
            "example": {"keyword": "Nintendo Switch"}
        }
    }


class EbaySearchResponse(BaseModel):
    average_price: int = Field(..., description="平均販売価格（円）")
    min_price: int = Field(..., description="最低価格（円）")
    max_price: int = Field(..., description="最高価格（円）")
    sold_count: int = Field(..., description="検索ヒット件数（出品数の近似・後方互換）")
    total_item_count: int = Field(..., description="検索ヒット件数（出品数）")

    model_config = {
        "json_schema_extra": {
            "example": {
                "average_price": 32000,
                "min_price": 28000,
                "max_price": 36000,
                "sold_count": 18,
                "total_item_count": 18,
            }
        }
    }
