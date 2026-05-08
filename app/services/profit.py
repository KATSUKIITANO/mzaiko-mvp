from decimal import ROUND_HALF_UP, Decimal

from app.schemas.profit import ProfitRequest, ProfitResponse


class ProfitCalculationError(Exception):
    pass


async def calculate_profit(payload: ProfitRequest) -> ProfitResponse:
    if payload.selling_price <= 0:
        raise ProfitCalculationError("selling_price は 0 より大きい必要があります")

    profit = (
        payload.selling_price
        - payload.purchase_price
        - payload.platform_fee
        - payload.shipping_cost
    )

    rate_decimal = (Decimal(profit) / Decimal(payload.selling_price)) * Decimal(100)
    profit_rate = float(
        rate_decimal.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    )

    return ProfitResponse(profit=profit, profit_rate=profit_rate)
