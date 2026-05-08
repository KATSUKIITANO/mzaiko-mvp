from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class TaskApproveRequest(BaseModel):
    assigned_to: str | None = Field(default=None)
    notes: str | None = Field(default=None)


class TaskActionRequest(BaseModel):
    notes: str | None = Field(default=None)
    tracking_number: str | None = Field(default=None)


class TaskCancelRequest(BaseModel):
    reason: str | None = Field(default=None)


class BuyerAddress(BaseModel):
    country: str | None = None
    state: str | None = None
    city: str | None = None
    postal_code: str | None = None
    address_line1: str | None = None
    address_line2: str | None = None
    phone: str | None = None


class ProfitBreakdown(BaseModel):
    ebay_sold_jpy: int
    amazon_price_jpy: int
    estimated_fee_jpy: int
    estimated_shipping_jpy: int
    fx_buffer_jpy: int
    refund_buffer_jpy: int
    risk_buffer_jpy: int
    expected_profit_jpy: int
    risk_adjusted_profit_jpy: int


class FulfillmentTaskListItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    order_id: int
    listing_id: int | None
    sku: str
    amazon_url: str | None
    amazon_price: int
    ebay_sold_price: int
    expected_profit: int
    risk_adjusted_profit: int | None
    estimated_shipping_cost: int | None
    shipping_zone: str | None
    shipping_method: str | None
    country_risk_level: str | None
    country_risk_score: int | None
    buyer_name: str | None
    buyer_country: str | None
    task_status: str
    assigned_to: str | None
    created_at: datetime
    updated_at: datetime


class FulfillmentTaskDetail(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    order_id: int
    listing_id: int | None
    sku: str
    amazon_url: str | None
    amazon_price: int
    ebay_sold_price: int
    expected_profit: int
    risk_adjusted_profit: int | None
    estimated_shipping_cost: int | None
    shipping_zone: str | None
    shipping_method: str | None
    country_risk_level: str | None
    country_risk_score: int | None
    buyer_name: str | None
    buyer_address_json: dict | None
    buyer_country: str | None
    task_status: str
    assigned_to: str | None
    notes: str | None
    created_at: datetime
    updated_at: datetime

    profit_breakdown: ProfitBreakdown | None = None
