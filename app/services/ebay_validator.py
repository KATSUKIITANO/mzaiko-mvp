"""eBay Listing payload の検証ロジック。

外部 API を呼ばずローカルで完結する。
preview / validate / publish の3段階構造のうち、
validate を担う。
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


TITLE_MAX_LENGTH: int = 80
DESCRIPTION_MIN_LENGTH: int = 20
IMAGE_RECOMMENDED_MIN: int = 1
IMAGE_HARD_MAX: int = 12


def validate_title(title: str) -> dict[str, Any]:
    cleaned = (title or "").strip()
    length = len(cleaned)
    ok = 0 < length <= TITLE_MAX_LENGTH
    warnings: list[str] = []
    if length == 0:
        warnings.append("title is empty")
    elif length > TITLE_MAX_LENGTH:
        warnings.append(
            f"title exceeds {TITLE_MAX_LENGTH} chars (got {length}); will be truncated"
        )
    return {"ok": ok, "length": length, "warnings": warnings}


def validate_description(description: str) -> dict[str, Any]:
    text = (description or "").strip()
    length = len(text)
    ok = length >= DESCRIPTION_MIN_LENGTH
    warnings: list[str] = []
    if length == 0:
        warnings.append("description is empty")
    elif length < DESCRIPTION_MIN_LENGTH:
        warnings.append(
            f"description shorter than {DESCRIPTION_MIN_LENGTH} chars"
        )
    return {"ok": ok, "length": length, "warnings": warnings}


def validate_image_urls(urls: list[str]) -> dict[str, Any]:
    count = len(urls or [])
    ok = count >= IMAGE_RECOMMENDED_MIN
    warnings: list[str] = []
    if count == 0:
        warnings.append("no images attached (eBay strongly recommends at least 1)")
    if count > IMAGE_HARD_MAX:
        warnings.append(
            f"image count {count} exceeds eBay max {IMAGE_HARD_MAX}; will be truncated"
        )
    return {"ok": ok, "count": count, "warnings": warnings}


def validate_price(price_str: str, currency: str) -> dict[str, Any]:
    warnings: list[str] = []
    try:
        value = float(price_str)
    except (TypeError, ValueError):
        return {
            "ok": False,
            "value": None,
            "currency": currency,
            "warnings": [f"price is not a valid number: {price_str!r}"],
        }

    ok = value > 0
    if value <= 0:
        warnings.append("price must be > 0")
    if currency.upper() not in {"USD", "JPY", "EUR", "GBP", "AUD", "CAD"}:
        warnings.append(f"unfamiliar currency: {currency}")
    return {"ok": ok, "value": value, "currency": currency, "warnings": warnings}


def validate_payloads(
    *,
    title: str,
    description: str,
    image_urls: list[str],
    price_str: str,
    currency: str,
) -> dict[str, Any]:
    """個別 validation をまとめて実行。"""
    title_v = validate_title(title)
    desc_v = validate_description(description)
    image_v = validate_image_urls(image_urls)
    price_v = validate_price(price_str, currency)

    all_warnings: list[str] = []
    for v in (title_v, desc_v, image_v, price_v):
        all_warnings.extend(v.get("warnings", []))

    overall_ok = all(v["ok"] for v in (title_v, desc_v, image_v, price_v))

    return {
        "title_length_ok": title_v["ok"],
        "description_ok": desc_v["ok"],
        "image_count_ok": image_v["ok"],
        "price_ok": price_v["ok"],
        "overall_ok": overall_ok,
        "warnings": all_warnings,
        "details": {
            "title": title_v,
            "description": desc_v,
            "images": image_v,
            "price": price_v,
        },
    }
