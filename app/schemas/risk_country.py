from pydantic import BaseModel, Field


class CountryRiskListResponse(BaseModel):
    """国別リスク mapping の一覧。"""
    blocked: list[str] = Field(..., description="ブロック対象国コード")
    high: list[str] = Field(..., description="高リスク国コード")
    medium: list[str] = Field(..., description="中リスク国コード")
    default_low: str = Field(
        "all other countries default to low",
        description="上記未収載は low にフォールバック",
    )
    score_table: dict[str, int] = Field(
        ..., description="risk_level → 数値スコア"
    )
    buffer_adjustment_percent: dict[str, float] = Field(
        ..., description="risk_level → 追加 refund_buffer (%)"
    )
