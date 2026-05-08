from pydantic import BaseModel, Field


class ShippingEstimateResponse(BaseModel):
    country: str
    zone: str = Field(..., description="US / EU / Asia / Oceania / SouthAmerica / Other")
    actual_weight_gram: int
    volumetric_weight_gram: int
    chargeable_weight_gram: int
    weight_tier: str = Field(..., description="small / medium / large / xlarge")
    method: str | None = Field(
        None, description="japan_post_small_packet / epacket / ems / dhl / null=unavailable"
    )
    method_label: str = Field(..., description="人間可読な配送方法名")
    estimated_cost_jpy: int
    notes: list[str] = Field(default_factory=list, description="warning メッセージ")
