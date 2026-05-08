"""リスク判定（純粋関数）。

入力: amazon_title / ebay_title / brand
出力: RiskAssessment (risk_level, risk_reason[], is_allowed)
"""
import logging

from app.config import settings
from app.schemas.risk import RiskAssessment
from app.services.risk_rules import (
    RISK_LEVEL_HIGH,
    RISK_LEVEL_LOW,
    RISK_REASON_HAZMAT,
    RISK_REASON_VERO_BRAND,
    get_hazmat_keywords,
    get_vero_brands,
)

logger = logging.getLogger(__name__)


def assess_risk(
    *,
    amazon_title: str = "",
    ebay_title: str = "",
    brand: str = "",
) -> RiskAssessment:
    if not settings.risk_filter_enabled:
        logger.debug("risk filter disabled, allow all")
        return RiskAssessment(
            risk_level=RISK_LEVEL_LOW,
            risk_reason=[],
            is_allowed=True,
        )

    haystack = _build_haystack(amazon_title, ebay_title, brand)

    reasons: list[str] = []
    matched_brand = _find_first_match(haystack, get_vero_brands())
    if matched_brand:
        reasons.append(RISK_REASON_VERO_BRAND)

    matched_hazmat = _find_first_match(haystack, get_hazmat_keywords())
    if matched_hazmat:
        reasons.append(RISK_REASON_HAZMAT)

    is_allowed = len(reasons) == 0
    risk_level = RISK_LEVEL_LOW if is_allowed else RISK_LEVEL_HIGH

    if reasons:
        logger.info(
            "risk detected level=%s reasons=%s vero_match=%s hazmat_match=%s title=%s",
            risk_level, reasons, matched_brand, matched_hazmat,
            (amazon_title or ebay_title)[:80],
        )
    else:
        logger.info(
            "risk_assessed level=%s title=%s",
            risk_level, (amazon_title or ebay_title)[:80],
        )

    return RiskAssessment(
        risk_level=risk_level,
        risk_reason=reasons,
        is_allowed=is_allowed,
    )


def _build_haystack(amazon_title: str, ebay_title: str, brand: str) -> str:
    parts = [s for s in (amazon_title, ebay_title, brand) if s]
    return " ".join(parts).lower()


def _find_first_match(haystack: str, needles: list[str]) -> str | None:
    for needle in needles:
        if needle and needle in haystack:
            return needle
    return None
