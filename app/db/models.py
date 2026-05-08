import enum
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class MonitoringStatus(str, enum.Enum):
    OK = "ok"
    WARNING = "warning"
    DANGER = "danger"


class ListingStatus(str, enum.Enum):
    PENDING = "pending"
    INVENTORY_CREATED = "inventory_created"
    OFFER_CREATED = "offer_created"
    PUBLISHED = "published"
    FAILED = "failed"
    DRY_RUN = "dry_run"
    ROLLED_BACK = "rolled_back"
    ENDED = "ended"
    SOLD = "sold"


class SyncStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    OK = "ok"
    PARTIAL = "partial"
    ERROR = "error"
    SKIPPED = "skipped"
    DRY_RUN = "dry_run"


class OrderStatus(str, enum.Enum):
    DETECTED = "detected"
    PROCESSING = "processing"
    ORDERED = "ordered"
    SHIPPED = "shipped"
    COMPLETED = "completed"
    FAILED = "failed"


class PaymentStatus(str, enum.Enum):
    PENDING = "pending"
    PAID = "paid"
    REFUNDED = "refunded"
    FAILED = "failed"


class FulfillmentStatus(str, enum.Enum):
    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    SHIPPED = "shipped"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"


class MessageCategory(str, enum.Enum):
    SHIPPING = "shipping"
    DISCOUNT = "discount"
    TRACKING = "tracking"
    GREETING = "greeting"
    COMPLAINT = "complaint"
    REFUND = "refund"
    DISPUTE = "dispute"
    UNSAFE = "unsafe"
    OTHER = "other"


class MessageStatus(str, enum.Enum):
    DRAFTED = "drafted"
    AWAITING_HUMAN = "awaiting_human"
    APPROVED = "approved"
    SENT = "sent"
    SKIPPED = "skipped"


class TaskStatus(str, enum.Enum):
    PENDING = "pending"
    REVIEWING = "reviewing"
    APPROVED = "approved"
    PURCHASED = "purchased"
    SHIPPED = "shipped"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"


class CountryRiskLevel(str, enum.Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    BLOCKED = "blocked"


class Product(Base):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    url: Mapped[str] = mapped_column(Text, unique=True, index=True, nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    price: Mapped[int] = mapped_column(Integer, nullable=False)
    in_stock: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    image_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    purchase_price: Mapped[int | None] = mapped_column(Integer, nullable=True)
    selling_price: Mapped[int | None] = mapped_column(Integer, nullable=True)
    platform_fee: Mapped[int | None] = mapped_column(Integer, nullable=True)
    shipping_cost: Mapped[int | None] = mapped_column(Integer, nullable=True)
    profit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    profit_rate: Mapped[float | None] = mapped_column(Float, nullable=True)

    last_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    current_profit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    current_profit_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    is_profitable: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False, index=True
    )
    monitoring_status: Mapped[str] = mapped_column(
        String(20), default=MonitoringStatus.OK.value, nullable=False, index=True
    )

    profit_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    demand_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    competition_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    overall_score: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)

    risk_level: Mapped[str] = mapped_column(
        String(20), default="low", nullable=False, index=True
    )
    risk_reason: Mapped[list | None] = mapped_column(JSON, nullable=True)
    is_allowed: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False, index=True
    )

    package_weight_gram: Mapped[int | None] = mapped_column(Integer, nullable=True)
    package_length_cm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    package_width_cm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    package_height_cm: Mapped[int | None] = mapped_column(Integer, nullable=True)

    ebay_sku: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    ebay_offer_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ebay_listing_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    ebay_listing_status: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    ebay_published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    ebay_last_sync_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    ebay_sync_status: Mapped[str | None] = mapped_column(
        String(32), nullable=True, index=True
    )
    ebay_sync_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class Listing(Base):
    __tablename__ = "listings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    product_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("products.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    sku: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    listing_price: Mapped[str] = mapped_column(String(32), nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="USD")
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    image_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    marketplace_id: Mapped[str] = mapped_column(
        String(32), nullable=False, default="EBAY_US"
    )
    category_id: Mapped[str | None] = mapped_column(String(32), nullable=True)

    listing_status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=ListingStatus.PENDING.value,
        index=True,
    )
    ebay_offer_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    ebay_listing_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    dry_run: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    request_payload: Mapped[dict | list | None] = mapped_column(JSON, nullable=True)
    response_payload: Mapped[dict | list | None] = mapped_column(JSON, nullable=True)

    revised_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    last_synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    sync_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    sync_finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    next_sync_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    sync_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    sync_status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=SyncStatus.PENDING.value,
        index=True,
    )
    sync_error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ebay_order_id: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False, index=True
    )
    listing_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("listings.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    sku: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    sold_price: Mapped[str] = mapped_column(String(32), nullable=False)
    sold_currency: Mapped[str] = mapped_column(String(8), nullable=False, default="USD")
    buyer_country: Mapped[str | None] = mapped_column(String(8), nullable=True)

    order_status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=OrderStatus.DETECTED.value,
        index=True,
    )
    raw_payload: Mapped[dict | list | None] = mapped_column(JSON, nullable=True)
    ebay_created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )

    ebay_listing_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    buyer_username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payment_status: Mapped[str | None] = mapped_column(
        String(20), nullable=True, index=True
    )
    fulfillment_status: Mapped[str | None] = mapped_column(
        String(20), nullable=True, index=True
    )
    tracking_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_modified_date: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    shipped_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    shipping_carrier: Mapped[str | None] = mapped_column(
        String(32), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class OrderItem(Base):
    __tablename__ = "order_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("orders.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    sku: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    price: Mapped[str | None] = mapped_column(String(32), nullable=True)
    currency: Mapped[str | None] = mapped_column(String(8), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class RepricingLog(Base):
    __tablename__ = "repricing_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    product_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("products.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    sku: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    old_price: Mapped[str | None] = mapped_column(String(32), nullable=True)
    new_price: Mapped[str | None] = mapped_column(String(32), nullable=True)
    currency: Mapped[str | None] = mapped_column(String(8), nullable=True)
    success: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    dry_run: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class BuyerMessage(Base):
    __tablename__ = "ebay_buyer_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ebay_message_id: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False, index=True
    )
    buyer_username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ebay_order_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    subject: Mapped[str | None] = mapped_column(String(512), nullable=True)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    received_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )

    category: Mapped[str | None] = mapped_column(
        String(20), nullable=True, index=True
    )
    draft_reply: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    requires_human: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(
        String(20),
        default=MessageStatus.DRAFTED.value,
        nullable=False,
        index=True,
    )
    replied_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    raw_payload: Mapped[dict | list | None] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class FulfillmentTask(Base):
    __tablename__ = "fulfillment_tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("orders.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    listing_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("listings.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    sku: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    amazon_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    amazon_price: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ebay_sold_price: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    expected_profit: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    estimated_shipping_cost: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    shipping_zone: Mapped[str | None] = mapped_column(String(20), nullable=True)
    shipping_method: Mapped[str | None] = mapped_column(String(40), nullable=True)

    # ── Country Risk
    country_risk_level: Mapped[str | None] = mapped_column(
        String(20), nullable=True, index=True
    )
    country_risk_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    risk_adjusted_profit: Mapped[int | None] = mapped_column(Integer, nullable=True)

    buyer_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    buyer_address_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    buyer_country: Mapped[str | None] = mapped_column(String(8), nullable=True, index=True)

    task_status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=TaskStatus.PENDING.value,
        index=True,
    )
    assigned_to: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
