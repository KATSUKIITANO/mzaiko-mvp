from pydantic import BaseModel, Field


class ProfitRequest(BaseModel):
    purchase_price: int = Field(..., ge=0, description="仕入価格（円）")
    selling_price: int = Field(..., gt=0, description="販売価格（円）")
    platform_fee: int = Field(..., ge=0, description="プラットフォーム手数料（円）")
    shipping_cost: int = Field(..., ge=0, description="送料（円）")

    model_config = {
        "json_schema_extra": {
            "example": {
                "purchase_price": 10000,
                "selling_price": 15000,
                "platform_fee": 1500,
                "shipping_cost": 1000,
            }
        }
    }


class ProfitResponse(BaseModel):
    profit: int = Field(..., description="利益（円）")
    profit_rate: float = Field(..., description="利益率（%・小数第2位）")

    model_config = {
        "json_schema_extra": {
            "example": {
                "profit": 2500,
                "profit_rate": 16.67,
            }
        }
    }
