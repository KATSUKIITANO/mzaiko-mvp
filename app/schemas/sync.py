from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class SyncListingStatus(BaseModel):
    """個別 listing の sync 状態。"""
    model_config = ConfigDict(from_attributes=True)

    id: int
    sku: str
    listing_status: str
    sync_status: str
    sync_started_at: datetime | None = None
    sync_finished_at: datetime | None = None
    last_synced_at: datetime | None = None
    next_sync_at: datetime | None = None
    sync_attempts: int = 0
    sync_error_message: str | None = None


class SyncStatusResponse(BaseModel):
    """worker 全体状態 + 直近 N 件の listing 状態。"""
    worker_enabled: bool
    worker_running: bool
    interval_minutes: int
    dry_run: bool
    last_run_started_at: datetime | None = None
    last_run_finished_at: datetime | None = None
    listings: list[SyncListingStatus]


class SyncMetricsResponse(BaseModel):
    """直近 1 回の worker run + DB 全体集計。"""
    # 直近 worker run
    last_run_started_at: datetime | None = None
    last_run_finished_at: datetime | None = None
    duration_seconds: float | None = None
    target_count: int = 0
    synced_count: int = 0
    revised_count: int = 0
    skipped_count: int = 0
    failed_count: int = 0
    no_change_count: int = 0

    # DB 全体集計
    total_published: int = Field(0, description="listing_status=published")
    total_running: int = Field(0, description="sync_status=running")
    total_pending: int = Field(0, description="sync_status=pending")
    total_error: int = Field(0, description="sync_status=error")
    total_skipped: int = Field(0, description="sync_status=skipped")
    total_partial: int = Field(0, description="sync_status=partial")
