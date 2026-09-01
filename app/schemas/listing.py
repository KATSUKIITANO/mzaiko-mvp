from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


# ── /ebay/list-item リクエスト ──────────
class ListItemRequest(BaseModel):
    dry_run: bool = Field(default=True, description="true なら eBay コールせず DB ログのみ")
    sku: str | None = Field(default=None, description="未指定時は EBAY_SKU_PREFIX + product_id")
    markup_percent: float | None = Field(default=None, description="未指定時は EBAY_LISTING_MARKUP_PERCENT")
    category_id: str | None = Field(default=None)
    marketplace_id: str | None = Field(default=None)
    currency: str | None = Field(default=None)

    model_config = {
        "json_schema_extra": {
            "example": {"dry_run": True, "markup_percent": 7.5}
        }
    }


# ── /ebay/revise-item リクエスト ────────
class ReviseItemRequest(BaseModel):
    dry_run: bool = Field(default=True, description="true なら eBay コールせず DB のみ更新")
    refetch_amazon: bool = Field(
        default=False,
        description="true なら revise 前に Amazon を再スクレイプして Product を更新",
    )
    markup_percent: float | None = Field(
        default=None, description="未指定時は EBAY_LISTING_MARKUP_PERCENT"
    )
    out_of_stock_action: str = Field(
        default="set_zero",
        description="Amazon在庫なし時の動作: 'set_zero' (quantity=0) or 'end_listing' (withdraw)",
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "dry_run": False,
                "refetch_amazon": True,
                "markup_percent": 8.0,
                "out_of_stock_action": "set_zero",
            }
        }
    }


# ── レスポンス（list / revise 共用）─────
class ListingDetail(BaseModel):
    """Listing 詳細（list_product / revise_listing 共通）"""
    model_config = ConfigDict(from_attributes=True)

    id: int
    product_id: int | None
    sku: str
    title: str
    listing_price: str
    currency: str
    quantity: int
    marketplace_id: str
    listing_status: str
    ebay_offer_id: str | None
    ebay_listing_id: str | None
    error_message: str | None
    dry_run: bool

    # revise 関連
    revised_at: datetime | None
    last_synced_at: datetime | None
    sync_status: str
    sync_error_message: str | None


# 後方互換のための alias
ListItemResponse = ListingDetail
ReviseItemResponse = ListingDetail


# ── /ebay/listing/create（CJ画像→eBay統合）────
class EbayListingCreateRequest(BaseModel):
    product_id: int = Field(..., description="DB上のProduct.id")
    cj_product_id: str = Field(..., min_length=1, description="CJ Dropshipping product id (pid)")
    dry_run: bool = Field(default=True, description="true なら payload 生成のみで eBay API は呼ばない")

    model_config = {
        "json_schema_extra": {
            "example": {"product_id": 1, "cj_product_id": "123456", "dry_run": True}
        }
    }


class EbayListingCreateResponse(BaseModel):
    success: bool
    image_count: int
    inventory_item_id: str | None = None
    offer_id: str | None = None
    published: bool = False
    dry_run: bool = False
    inventory_payload: dict | None = Field(
        default=None, description="dry_run=true の時のみ入る"
    )
    offer_payload: dict | None = Field(
        default=None, description="dry_run=true の時のみ入る"
    )


# ── /ebay/listing/preview（eBay未接続でもpayloadを完全確認）────
class EbayListingPreviewRequest(BaseModel):
    product_id: int = Field(..., description="DB上のProduct.id")
    cj_product_id: str = Field(..., min_length=1, description="CJ Dropshipping pid")

    model_config = {
        "json_schema_extra": {
            "example": {"product_id": 1, "cj_product_id": "123456"}
        }
    }


class EbayListingPreviewResponse(BaseModel):
    sku: str
    inventory_payload: dict
    offer_payload: dict
    publish_payload: dict
    images: list[str]
    validation: dict


# ── /ebay/listing/copy（販売文章AI生成）────
class EbayListingCopyRequest(BaseModel):
    product_id: int = Field(..., description="DB上のProduct.id")
    dry_run: bool = Field(
        default=False, description="true なら AI を呼ばず template fallback で生成"
    )
    provider: str | None = Field(
        default=None, description="template / openai / claude のいずれか。未指定時は env"
    )


class EbayListingCopyResponse(BaseModel):
    optimized_title: str
    subtitle: str
    html_description: str
    bullet_points: list[str]
    seo_keywords: list[str]
    condition_note: str
    shipping_note: str
    return_note: str

    @property
    def keywords(self) -> list[str]:
        return self.seo_keywords


# ── /ebay/listing/publish（本番出品）────
class EbayListingPublishRequest(BaseModel):
    product_id: int = Field(..., description="DB上のProduct.id")
    cj_product_id: str = Field(..., min_length=1, description="CJ Dropshipping pid")
    marketplace_id: str | None = Field(
        default=None, description="EBAY_US / EBAY_GB など。未指定時は env デフォルト"
    )
    dry_run: bool = Field(
        default=False, description="true なら eBay APIを呼ばず payload のみ返す"
    )


class EbayListingPublishResponse(BaseModel):
    success: bool
    sku: str
    offer_id: str | None = None
    listing_id: str | None = None
    listing_url: str | None = None
    published: bool = False
    dry_run: bool = False
    inventory_payload: dict | None = Field(default=None, description="dry_run時のみ")
    offer_payload: dict | None = Field(default=None, description="dry_run時のみ")
    publish_payload: dict | None = Field(default=None, description="dry_run時のみ")


# ── /ebay/listing/{sku}/withdraw（手動停止）────
class EbayWithdrawResponse(BaseModel):
    success: bool
    action: str
    sku: str
    reason: str
    error: str | None = None


class PrimaryImageRequest(BaseModel):
    """トップ画像を「現在の n 枚目」に差し替える（n は 1 始まり）。"""

    position: int = Field(
        ..., ge=1, description="トップにしたい画像の位置（1始まり / 2 なら2枚目）"
    )
    dry_run: bool = Field(
        default=False, description="true なら並び替え結果を返すだけ（eBay を呼ばない）"
    )
    republish: bool = Field(
        default=True,
        description="published な listing を再publishして即時反映させる",
    )

    model_config = ConfigDict(
        json_schema_extra={"example": {"position": 2, "dry_run": False}}
    )


class PrimaryImageItem(BaseModel):
    ref: str = Field(
        ...,
        min_length=1,
        description="SKU / SKUの数字部分 / product id のいずれか（例: 5222）",
    )
    position: int = Field(..., ge=1, description="トップにしたい画像の位置（1始まり）")


class PrimaryImageBulkRequest(BaseModel):
    items: list[PrimaryImageItem] = Field(..., min_length=1)
    dry_run: bool = Field(default=False)
    republish: bool = Field(default=True)

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "items": [
                    {"ref": "5222", "position": 2},
                    {"ref": "5248", "position": 5},
                    {"ref": "5259", "position": 2},
                    {"ref": "5297", "position": 2},
                ],
                "dry_run": True,
            }
        }
    )


class PrimaryImageResponse(BaseModel):
    success: bool
    sku: str | None = None
    ref: str
    position: int
    changed: bool
    dry_run: bool
    reason: str | None = None
    image_count: int | None = None
    old_primary: str | None = None
    new_primary: str | None = None
    image_urls: list[str] | None = None
    republished: bool = False
    listing_id: str | None = None
    error: str | None = None


class PrimaryImageBulkResponse(BaseModel):
    total: int
    succeeded: int
    failed: int
    dry_run: bool
    results: list[PrimaryImageResponse]
