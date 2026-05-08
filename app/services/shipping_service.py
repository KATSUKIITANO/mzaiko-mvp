"""国際配送見積サービス。

純粋関数中心の estimator + zone mapping + rate table。

設計:
  - country → zone（US/EU/Asia/Oceania/SouthAmerica/Other）
  - 重量帯（small <500g / medium <1kg / large <2kg / xlarge >=2kg）
  - 容積重量(g) = (L×W×H)/SHIPPING_VOLUMETRIC_DIVISOR × 1000
  - 課金対象重量 = max(actual, volumetric)
  - method recommendation: rate table から最安を選択
  - rate 不在時 fallback: settings.default_shipping_cost + warning

将来:
  - Japan Post API / DHL API へ rate lookup を置換可
  - label purchase: 現在の `_recommend_method()` の戻り値で API 経由のラベル発行を実行
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.config import settings
from app.schemas.shipping import ShippingEstimateResponse

logger = logging.getLogger(__name__)


# ── Zone 定数 ────────────────────────
ZONE_US = "US"
ZONE_EU = "EU"
ZONE_ASIA = "Asia"
ZONE_OCEANIA = "Oceania"
ZONE_SOUTH_AMERICA = "SouthAmerica"
ZONE_OTHER = "Other"

# ── Method 定数 ──────────────────────
METHOD_SMALL_PACKET = "japan_post_small_packet"
METHOD_EPACKET = "epacket"
METHOD_EMS = "ems"
METHOD_DHL = "dhl"

METHOD_LABELS: dict[str, str] = {
    METHOD_SMALL_PACKET: "Japan Post Small Packet",
    METHOD_EPACKET: "ePacket",
    METHOD_EMS: "EMS",
    METHOD_DHL: "DHL",
}

# ── Country → Zone ────────────────────
_COUNTRY_ZONES: dict[str, str] = {
    "US": ZONE_US,
    # EU + UK + CH（簡略化）
    "DE": ZONE_EU, "FR": ZONE_EU, "IT": ZONE_EU, "ES": ZONE_EU,
    "NL": ZONE_EU, "BE": ZONE_EU, "AT": ZONE_EU, "PL": ZONE_EU,
    "SE": ZONE_EU, "NO": ZONE_EU, "FI": ZONE_EU, "DK": ZONE_EU,
    "PT": ZONE_EU, "IE": ZONE_EU, "GR": ZONE_EU, "CZ": ZONE_EU,
    "HU": ZONE_EU, "GB": ZONE_EU, "CH": ZONE_EU,
    # Asia
    "JP": ZONE_ASIA, "KR": ZONE_ASIA, "CN": ZONE_ASIA, "TW": ZONE_ASIA,
    "HK": ZONE_ASIA, "SG": ZONE_ASIA, "TH": ZONE_ASIA, "MY": ZONE_ASIA,
    "PH": ZONE_ASIA, "VN": ZONE_ASIA, "IN": ZONE_ASIA, "ID": ZONE_ASIA,
    # Oceania
    "AU": ZONE_OCEANIA, "NZ": ZONE_OCEANIA,
    # South America
    "BR": ZONE_SOUTH_AMERICA, "AR": ZONE_SOUTH_AMERICA, "CL": ZONE_SOUTH_AMERICA,
    "PE": ZONE_SOUTH_AMERICA, "CO": ZONE_SOUTH_AMERICA,
}


def country_to_zone(country: str | None) -> str:
    if not country:
        return ZONE_OTHER
    return _COUNTRY_ZONES.get(country.upper(), ZONE_OTHER)


# ── 重量帯 ──────────────────────────
TIER_SMALL = "small"
TIER_MEDIUM = "medium"
TIER_LARGE = "large"
TIER_XLARGE = "xlarge"


def weight_tier(weight_gram: int) -> str:
    if weight_gram < 500:
        return TIER_SMALL
    if weight_gram < 1000:
        return TIER_MEDIUM
    if weight_gram < 2000:
        return TIER_LARGE
    return TIER_XLARGE


def volumetric_weight_gram(length_cm: int, width_cm: int, height_cm: int) -> int:
    """容積重量(g) = (L × W × H) / SHIPPING_VOLUMETRIC_DIVISOR × 1000"""
    if length_cm <= 0 or width_cm <= 0 or height_cm <= 0:
        return 0
    cm3 = length_cm * width_cm * height_cm
    divisor = max(1, settings.shipping_volumetric_divisor)
    return int(cm3 / divisor * 1000)


def chargeable_weight_gram(
    actual_gram: int,
    length_cm: int | None,
    width_cm: int | None,
    height_cm: int | None,
) -> tuple[int, int]:
    """Returns (chargeable, volumetric)."""
    if not (length_cm and width_cm and height_cm):
        return actual_gram, 0
    vol = volumetric_weight_gram(length_cm, width_cm, height_cm)
    return max(actual_gram, vol), vol


# ── Rate table（JPY、MVP用静的データ。Japan Post / DHL API へ後日置換）
_RATE_TABLE: dict[str, dict[str, dict[str, int]]] = {
    ZONE_US: {
        METHOD_SMALL_PACKET: {TIER_SMALL: 1200, TIER_MEDIUM: 1800},
        METHOD_EPACKET:      {TIER_SMALL: 1400, TIER_MEDIUM: 2200, TIER_LARGE: 3500},
        METHOD_EMS:          {TIER_SMALL: 2200, TIER_MEDIUM: 3000, TIER_LARGE: 4800, TIER_XLARGE: 7500},
        METHOD_DHL:          {TIER_SMALL: 5000, TIER_MEDIUM: 6500, TIER_LARGE: 9500, TIER_XLARGE: 14000},
    },
    ZONE_EU: {
        METHOD_SMALL_PACKET: {TIER_SMALL: 1400, TIER_MEDIUM: 2100},
        METHOD_EPACKET:      {TIER_SMALL: 1700, TIER_MEDIUM: 2600, TIER_LARGE: 4000},
        METHOD_EMS:          {TIER_SMALL: 2600, TIER_MEDIUM: 3500, TIER_LARGE: 5500, TIER_XLARGE: 9000},
        METHOD_DHL:          {TIER_SMALL: 5500, TIER_MEDIUM: 7000, TIER_LARGE: 10000, TIER_XLARGE: 15000},
    },
    ZONE_ASIA: {
        METHOD_SMALL_PACKET: {TIER_SMALL: 800,  TIER_MEDIUM: 1300},
        METHOD_EPACKET:      {TIER_SMALL: 1000, TIER_MEDIUM: 1600, TIER_LARGE: 2400},
        METHOD_EMS:          {TIER_SMALL: 1500, TIER_MEDIUM: 2100, TIER_LARGE: 3300, TIER_XLARGE: 5500},
        METHOD_DHL:          {TIER_SMALL: 4000, TIER_MEDIUM: 5200, TIER_LARGE: 7500, TIER_XLARGE: 11000},
    },
    ZONE_OCEANIA: {
        METHOD_SMALL_PACKET: {TIER_SMALL: 1300, TIER_MEDIUM: 2000},
        METHOD_EPACKET:      {TIER_SMALL: 1600, TIER_MEDIUM: 2500, TIER_LARGE: 3800},
        METHOD_EMS:          {TIER_SMALL: 2400, TIER_MEDIUM: 3300, TIER_LARGE: 5200, TIER_XLARGE: 8500},
        METHOD_DHL:          {TIER_SMALL: 5200, TIER_MEDIUM: 6800, TIER_LARGE: 9800, TIER_XLARGE: 14500},
    },
    ZONE_SOUTH_AMERICA: {
        METHOD_EMS: {TIER_SMALL: 3000, TIER_MEDIUM: 4200, TIER_LARGE: 6500, TIER_XLARGE: 10500},
        METHOD_DHL: {TIER_SMALL: 6500, TIER_MEDIUM: 8500, TIER_LARGE: 12000, TIER_XLARGE: 18000},
    },
    ZONE_OTHER: {
        METHOD_EMS: {TIER_SMALL: 2800, TIER_MEDIUM: 3800, TIER_LARGE: 6000, TIER_XLARGE: 10000},
        METHOD_DHL: {TIER_SMALL: 6000, TIER_MEDIUM: 7800, TIER_LARGE: 11000, TIER_XLARGE: 17000},
    },
}


def _lookup_rate(zone: str, method: str, tier: str) -> int | None:
    return _RATE_TABLE.get(zone, {}).get(method, {}).get(tier)


def _recommend_method(zone: str, tier: str) -> tuple[str | None, int | None]:
    """rate table から最安の利用可能 method を返す。"""
    methods = _RATE_TABLE.get(zone, {})
    candidates: list[tuple[int, str]] = []
    for method, tiers in methods.items():
        cost = tiers.get(tier)
        if cost is not None:
            candidates.append((cost, method))
    if not candidates:
        return None, None
    candidates.sort()
    cost, method = candidates[0]
    return method, cost


# ── 結果型 ──────────────────────────
@dataclass
class ShippingEstimate:
    country: str
    zone: str
    actual_weight_gram: int
    volumetric_weight_gram: int
    chargeable_weight_gram: int
    weight_tier: str
    method: str | None
    method_label: str
    estimated_cost_jpy: int
    notes: list[str] = field(default_factory=list)

    def to_response(self) -> ShippingEstimateResponse:
        return ShippingEstimateResponse(
            country=self.country,
            zone=self.zone,
            actual_weight_gram=self.actual_weight_gram,
            volumetric_weight_gram=self.volumetric_weight_gram,
            chargeable_weight_gram=self.chargeable_weight_gram,
            weight_tier=self.weight_tier,
            method=self.method,
            method_label=self.method_label,
            estimated_cost_jpy=self.estimated_cost_jpy,
            notes=list(self.notes),
        )


# ── 公開関数 ────────────────────────
def estimate_shipping(
    country: str | None,
    weight_gram: int,
    length_cm: int | None = None,
    width_cm: int | None = None,
    height_cm: int | None = None,
) -> ShippingEstimate:
    """国・重量・寸法から配送料金を見積もる純粋関数。

    - 寸法が None なら容積重量を計算せず、actual を採用（warning に記録）
    - rate table にヒットしなければ DEFAULT_SHIPPING_COST にフォールバック
    """
    notes: list[str] = []
    country_norm = (country or "").upper() or "OTHER"
    zone = country_to_zone(country_norm)

    if zone == ZONE_OTHER and country_norm not in ("", "OTHER"):
        notes.append(f"unsupported country={country_norm}, fallback zone=Other")
        logger.warning(
            "unsupported country country=%s falling back to Other zone",
            country_norm,
        )

    if not (length_cm and width_cm and height_cm):
        notes.append("missing dimensions, using actual weight only")
        logger.warning(
            "missing dimensions for shipping estimate (using actual weight only)"
        )

    chargeable_g, vol_g = chargeable_weight_gram(
        weight_gram, length_cm, width_cm, height_cm
    )
    tier = weight_tier(chargeable_g)

    method, cost = _recommend_method(zone, tier)

    if method is None or cost is None:
        notes.append(
            f"no available method for zone={zone} tier={tier}, "
            f"fallback to DEFAULT_SHIPPING_COST"
        )
        logger.warning(
            "no shipping method available zone=%s tier=%s actual_g=%d "
            "(fallback %d)",
            zone, tier, chargeable_g, settings.default_shipping_cost,
        )
        method = None
        method_label = "UNAVAILABLE"
        cost = settings.default_shipping_cost
    else:
        method_label = METHOD_LABELS.get(method, method)

    estimate = ShippingEstimate(
        country=country_norm,
        zone=zone,
        actual_weight_gram=weight_gram,
        volumetric_weight_gram=vol_g,
        chargeable_weight_gram=chargeable_g,
        weight_tier=tier,
        method=method,
        method_label=method_label,
        estimated_cost_jpy=cost,
        notes=notes,
    )

    logger.info(
        "shipping estimated country=%s zone=%s tier=%s method=%s "
        "weight=%d (vol=%d charge=%d) cost=%d",
        country_norm, zone, tier, method or "NONE",
        weight_gram, vol_g, chargeable_g, cost,
    )

    return estimate


def estimate_with_defaults(
    country: str | None,
    weight_gram: int | None,
    length_cm: int | None,
    width_cm: int | None,
    height_cm: int | None,
) -> ShippingEstimate:
    """Product の寸法/重量が None の場合に env デフォルトで補完する wrapper。"""
    return estimate_shipping(
        country=country,
        weight_gram=weight_gram if weight_gram and weight_gram > 0 else settings.shipping_default_weight_gram,
        length_cm=length_cm if length_cm and length_cm > 0 else settings.shipping_default_length_cm,
        width_cm=width_cm if width_cm and width_cm > 0 else settings.shipping_default_width_cm,
        height_cm=height_cm if height_cm and height_cm > 0 else settings.shipping_default_height_cm,
    )
