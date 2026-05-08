from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class BuyerMessageItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    ebay_message_id: str
    buyer_username: str | None
    ebay_order_id: str | None
    subject: str | None
    body: str | None
    category: str | None
    draft_reply: str | None
    confidence: float | None
    requires_human: bool
    status: str
    received_at: datetime | None
    replied_at: datetime | None
    created_at: datetime


class MessageSyncResponse(BaseModel):
    imported: int
    updated: int
    skipped: int


class AutoReplyRequest(BaseModel):
    message_id: int = Field(..., description="DB上のBuyerMessage.id")
    force: bool = Field(default=False, description="trueで AUTO_REPLY_SEND_ENABLED=false を上書き")
    approve: bool = Field(default=False, description="trueで承認(human review通過)に切替")


class AutoReplyResponse(BaseModel):
    success: bool
    skipped: bool = False
    reason: str | None = None
    message_id: int | None = None
    status: str | None = None
    draft: str | None = None
    confidence: float | None = None
    category: str | None = None
    requires_human: bool | None = None
