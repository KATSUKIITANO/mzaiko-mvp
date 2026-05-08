from pydantic import BaseModel, Field


class ExportFilters(BaseModel):
    profitable_only: bool = Field(
        default=False, description="is_profitable=true のみ"
    )
    allowed_only: bool = Field(
        default=False, description="is_allowed=true のみ"
    )
    monitoring_status: str | None = Field(
        default=None, description="ok / warning / danger（完全一致）"
    )
    min_profit_rate: float | None = Field(
        default=None, description="この利益率(%)以上"
    )
    min_overall_score: int | None = Field(
        default=None, description="このoverall_score以上"
    )
