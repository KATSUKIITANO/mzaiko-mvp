from pydantic import BaseModel, Field


class RiskAssessment(BaseModel):
    risk_level: str = Field(..., description="high / low")
    risk_reason: list[str] = Field(
        default_factory=list,
        description="ヒットしたreasonコードの配列（例: vero_brand, hazmat）",
    )
    is_allowed: bool = Field(..., description="DB保存・出品可否")

    model_config = {
        "json_schema_extra": {
            "example": {
                "risk_level": "high",
                "risk_reason": ["vero_brand", "hazmat"],
                "is_allowed": False,
            }
        }
    }
