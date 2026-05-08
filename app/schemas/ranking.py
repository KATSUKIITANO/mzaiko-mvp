from pydantic import BaseModel, Field


class RankingProduct(BaseModel):
    rank: int = Field(..., description="ランキング順位")
    title: str = Field(..., description="商品名")
    amazon_url: str = Field(..., description="正規化された https://www.amazon.co.jp/dp/{ASIN}")
    asin: str = Field(..., description="Amazon ASIN")


class RankingResponse(BaseModel):
    category: str = Field(..., description="カテゴリ識別子")
    count: int = Field(..., description="返却件数")
    products: list[RankingProduct] = Field(..., description="ランキング商品の配列")

    model_config = {
        "json_schema_extra": {
            "example": {
                "category": "videogames",
                "count": 3,
                "products": [
                    {
                        "rank": 1,
                        "title": "Nintendo Switch（有機ELモデル）...",
                        "amazon_url": "https://www.amazon.co.jp/dp/B09H6F8K12",
                        "asin": "B09H6F8K12",
                    },
                    {
                        "rank": 2,
                        "title": "PlayStation 5 ...",
                        "amazon_url": "https://www.amazon.co.jp/dp/B0BCNKKZ91",
                        "asin": "B0BCNKKZ91",
                    },
                    {
                        "rank": 3,
                        "title": "...",
                        "amazon_url": "https://www.amazon.co.jp/dp/B0XXXXXXXX",
                        "asin": "B0XXXXXXXX",
                    },
                ],
            }
        }
    }
