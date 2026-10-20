from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.database import get_session
from app.db.models import (
    BuyerMessage,
    Listing,
    ListingStatus,
    MessageStatus,
    Order,
    OrderStatus,
    Product,
    SyncStatus,
)
from app.schemas.analyze import AnalyzeRequest, AnalyzeResponse
from app.schemas.bulk import BulkAnalyzeRequest, BulkAnalyzeResponse
from app.schemas.cj import (
    CJMappingItem,
    CJMappingUpsertRequest,
    CJOrderItem,
    CJOrderPlaceRequest,
    CJOrderPlaceResponse,
    CJOrderWorkerMetrics,
    CustomsRiskCheckResponse,
)
from app.schemas.dashboard import DashboardSummary
from app.schemas.ebay import EbaySearchRequest, EbaySearchResponse
from app.schemas.export import ExportFilters
from app.schemas.fulfillment import (
    FulfillmentTaskDetail,
    FulfillmentTaskListItem,
    TaskActionRequest,
    TaskApproveRequest,
    TaskCancelRequest,
)
from app.schemas.listing import (
    EbayListingCopyRequest,
    EbayListingCopyResponse,
    EbayListingCreateRequest,
    EbayListingCreateResponse,
    EbayListingPreviewRequest,
    EbayListingPreviewResponse,
    EbayListingPublishRequest,
    EbayListingPublishResponse,
    EbayWithdrawResponse,
    ListingDetail,
    ListItemRequest,
    PrimaryImageBulkRequest,
    PrimaryImageBulkResponse,
    PrimaryImageRequest,
    PrimaryImageResponse,
    ReviseItemRequest,
)
from app.schemas.monitoring import (
    AlertRequest,
    AlertResponse,
    HealthResponse,
    MetricsResponse,
    MonitoringProduct,
)
from app.schemas.message import (
    AutoReplyRequest,
    AutoReplyResponse,
    BuyerMessageItem,
    MessageSyncResponse,
)
from app.schemas.order import (
    OrderDetail,
    OrderListItem,
    OrderSyncResponse,
    SoldSyncMetricsResponse,
    TrackingUploadRequest,
    TrackingUploadResponse,
)
from app.schemas.product import ScrapeRequest, ScrapeResponse
from app.schemas.profit import ProfitRequest, ProfitResponse
from app.schemas.ranking import RankingProduct, RankingResponse
from app.schemas.risk_country import CountryRiskListResponse
from app.schemas.shipping import ShippingEstimateResponse
from app.schemas.sync import (
    SyncListingStatus,
    SyncMetricsResponse,
    SyncStatusResponse,
)
from app.scraper.amazon import ScrapeError, scrape_amazon
from app.scraper.amazon_ranking import (
    RankingScrapeError,
    UnknownCategoryError,
    fetch_amazon_ranking,
)
from app.scraper.cj import (
    CJError,
    cj_product_detail,
    cj_search,
    extract_image_urls,
)
from app.services.analyze_service import AnalyzeError, analyze_product
from app.services.auth_service import verify_api_key
from app.services.cj_mapping_service import list_mappings, upsert_mapping
from app.services.cj_order_service import (
    CJOrderError,
    get_cj_orders_for_ebay_order,
    get_last_metrics as get_cj_order_metrics,
    list_cj_orders,
    list_unmapped_skus,
    place_cj_order_for_ebay_order,
    run_cj_order_worker,
)
from app.services.bulk_analyze_service import bulk_analyze_products
from app.services.country_risk_service import (
    LEVEL_TO_SCORE,
    get_blocked_countries,
    get_buffer_adjustment_percent_table,
    get_high_risk_countries,
    get_medium_risk_countries,
)
from app.services.csv_service import stream_products_csv
from app.services.customs_risk_service import assess_customs_risk, split_quantity
from app.services.dashboard_service import get_dashboard_summary
from app.services.ebay import EbayApiError, search_ebay
from app.services.ebay_auth_service import EbayAuthError, ping_inventory
from app.services.ebay_image_service import (
    bulk_set_primary_image,
    set_primary_image,
)
from app.services.ebay_listing_cj_service import (
    create_listing_with_cj_images,
    generate_preview,
)
from app.services.ebay_fulfillment_tracking_service import (
    apply_tracking_to_order,
)
from app.services.ebay_message_service import send_reply, sync_messages
from app.services.monitoring_service import (
    get_health,
    get_metrics,
    send_alert,
)
from app.services.ebay_order_service import sync_orders
from app.services.repricing_service import bulk_reprice
from app.services.ebay_publish_service import publish_listing
from app.services.ebay_revise_service import withdraw_by_sku
from app.services.listing_copy_service import generate_copy
from app.services.ebay_listing_service import (
    ListingError,
    RiskBlockedError,
    list_product,
    revise_listing,
)
from app.services.fulfillment_service import (
    FulfillmentError,
    TaskNotFoundError,
    TaskTransitionError,
    approve_task,
    build_profit_breakdown,
    cancel_task,
    get_task,
    list_tasks,
    list_tasks_by_risk,
    mark_purchased,
    mark_shipped,
    recalculate_shipping_for_task,
)
from app.services.profit import ProfitCalculationError, calculate_profit
from app.services.rate_limit_service import (
    rate_limit_amazon_ranking,
    rate_limit_analyze_product,
    rate_limit_bulk_analyze,
)
from app.services.revise_worker_service import (
    get_last_metrics as get_revise_metrics,
    is_worker_running as is_revise_running,
)
from app.services.shipping_service import estimate_shipping
from app.services.sold_sync_service import (
    get_last_metrics as get_sold_metrics,
)


public_router = APIRouter()


@public_router.post("/scrape", response_model=ScrapeResponse, tags=["scrape"])
async def scrape_endpoint(
    payload: ScrapeRequest,
    session: AsyncSession = Depends(get_session),
) -> ScrapeResponse:
    url = str(payload.url)
    try:
        data = await scrape_amazon(url)
    except ScrapeError as e:
        raise HTTPException(status_code=502, detail=f"スクレイピング失敗: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"予期しないエラー: {e}")

    result = await session.execute(select(Product).where(Product.url == url))
    existing = result.scalar_one_or_none()
    if existing:
        existing.title = data["title"]
        existing.price = data["price"]
        existing.in_stock = data["in_stock"]
        existing.image_url = data["image_url"]
        product = existing
    else:
        product = Product(url=url, **data)
        session.add(product)
    await session.commit()
    await session.refresh(product)
    return ScrapeResponse.model_validate(product)


@public_router.post("/calculate-profit", response_model=ProfitResponse, tags=["profit"])
async def calculate_profit_endpoint(payload: ProfitRequest) -> ProfitResponse:
    try:
        return await calculate_profit(payload)
    except ProfitCalculationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"予期しないエラー: {e}")


@public_router.post("/ebay/search", response_model=EbaySearchResponse, tags=["ebay"])
async def ebay_search_endpoint(payload: EbaySearchRequest) -> EbaySearchResponse:
    try:
        return await search_ebay(payload)
    except EbayApiError as e:
        raise HTTPException(status_code=502, detail=f"eBay API エラー: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"予期しないエラー: {e}")


@public_router.get("/monitoring/products", response_model=list[MonitoringProduct], tags=["monitoring"])
async def monitoring_products_endpoint(
    session: AsyncSession = Depends(get_session),
) -> list[MonitoringProduct]:
    result = await session.execute(
        select(Product).order_by(
            Product.last_checked_at.desc().nullslast(), Product.id.desc()
        )
    )
    products = result.scalars().all()
    return [MonitoringProduct.model_validate(p) for p in products]


# ────────────────────────────────────────
# private
# ────────────────────────────────────────
private_router = APIRouter(dependencies=[Depends(verify_api_key)])


@private_router.post(
    "/analyze-product",
    response_model=AnalyzeResponse,
    tags=["analyze"],
    dependencies=[Depends(rate_limit_analyze_product)],
)
async def analyze_product_endpoint(
    payload: AnalyzeRequest,
    session: AsyncSession = Depends(get_session),
) -> AnalyzeResponse:
    try:
        return await analyze_product(payload, session)
    except AnalyzeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"予期しないエラー: {e}")


@private_router.post(
    "/bulk-analyze",
    response_model=BulkAnalyzeResponse,
    tags=["analyze"],
    dependencies=[Depends(rate_limit_bulk_analyze)],
)
async def bulk_analyze_endpoint(payload: BulkAnalyzeRequest) -> BulkAnalyzeResponse:
    try:
        return await bulk_analyze_products(payload)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"予期しないエラー: {e}")


@private_router.get(
    "/amazon/ranking",
    response_model=RankingResponse,
    tags=["ranking"],
    dependencies=[Depends(rate_limit_amazon_ranking)],
)
async def amazon_ranking_endpoint(
    category: str = Query(...),
    limit: int | None = Query(None, ge=1, le=100),
) -> RankingResponse:
    effective_limit = limit if limit is not None else settings.ranking_default_limit
    try:
        products = await fetch_amazon_ranking(category=category, limit=effective_limit)
    except UnknownCategoryError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RankingScrapeError as e:
        raise HTTPException(status_code=502, detail=f"ランキング取得失敗: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"予期しないエラー: {e}")
    return RankingResponse(
        category=category, count=len(products),
        products=[RankingProduct(**p) for p in products],
    )


@private_router.get("/export/products.csv", tags=["export"])
async def export_products_csv(
    profitable_only: bool = Query(False),
    allowed_only: bool = Query(False),
    monitoring_status: str | None = Query(None),
    min_profit_rate: float | None = Query(None),
    min_overall_score: int | None = Query(None, ge=0, le=100),
) -> StreamingResponse:
    filters = ExportFilters(
        profitable_only=profitable_only, allowed_only=allowed_only,
        monitoring_status=monitoring_status, min_profit_rate=min_profit_rate,
        min_overall_score=min_overall_score,
    )
    return StreamingResponse(
        stream_products_csv(filters),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="products.csv"',
            "Cache-Control": "no-store",
        },
    )


@private_router.get("/dashboard/summary", response_model=DashboardSummary, tags=["dashboard"])
async def dashboard_summary_endpoint(
    session: AsyncSession = Depends(get_session),
) -> DashboardSummary:
    try:
        return await get_dashboard_summary(session)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"集計エラー: {e}")


@private_router.get("/ebay/ping", tags=["ebay"])
async def ebay_ping_endpoint() -> dict:
    try:
        return await ping_inventory()
    except EbayAuthError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"予期しないエラー: {e}")


@private_router.post("/ebay/list-item/{product_id}", response_model=ListingDetail, tags=["listing"])
async def list_item_endpoint(
    product_id: int,
    payload: ListItemRequest,
    session: AsyncSession = Depends(get_session),
) -> ListingDetail:
    try:
        return await list_product(product_id, payload, session)
    except RiskBlockedError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except ListingError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"予期しないエラー: {e}")


@private_router.post("/ebay/revise-item/{listing_id}", response_model=ListingDetail, tags=["listing"])
async def revise_item_endpoint(
    listing_id: int,
    payload: ReviseItemRequest,
    session: AsyncSession = Depends(get_session),
) -> ListingDetail:
    try:
        return await revise_listing(listing_id, payload, session)
    except RiskBlockedError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except ListingError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"予期しないエラー: {e}")


@private_router.get("/ebay/sync-status", response_model=SyncStatusResponse, tags=["sync"])
async def ebay_sync_status_endpoint(
    limit: int = Query(50, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
) -> SyncStatusResponse:
    metrics = get_revise_metrics()
    stmt = (
        select(Listing)
        .order_by(Listing.last_synced_at.desc().nullslast(), Listing.id.desc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    listings = list(result.scalars().all())
    return SyncStatusResponse(
        worker_enabled=settings.revise_worker_enabled,
        worker_running=is_revise_running(),
        interval_minutes=settings.revise_worker_interval_minutes,
        dry_run=settings.revise_worker_dry_run,
        last_run_started_at=metrics.started_at if metrics else None,
        last_run_finished_at=metrics.finished_at if metrics else None,
        listings=[SyncListingStatus.model_validate(l) for l in listings],
    )


@private_router.get("/ebay/sync-metrics", response_model=SyncMetricsResponse, tags=["sync"])
async def ebay_sync_metrics_endpoint(
    session: AsyncSession = Depends(get_session),
) -> SyncMetricsResponse:
    metrics = get_revise_metrics()
    counts_stmt = select(
        func.count().filter(Listing.listing_status == ListingStatus.PUBLISHED.value).label("published"),
        func.count().filter(Listing.sync_status == SyncStatus.RUNNING.value).label("running"),
        func.count().filter(Listing.sync_status == SyncStatus.PENDING.value).label("pending"),
        func.count().filter(Listing.sync_status == SyncStatus.ERROR.value).label("error"),
        func.count().filter(Listing.sync_status == SyncStatus.SKIPPED.value).label("skipped"),
        func.count().filter(Listing.sync_status == SyncStatus.PARTIAL.value).label("partial"),
    )
    row = (await session.execute(counts_stmt)).one()
    return SyncMetricsResponse(
        last_run_started_at=metrics.started_at if metrics else None,
        last_run_finished_at=metrics.finished_at if metrics else None,
        duration_seconds=metrics.duration_seconds if metrics else None,
        target_count=metrics.target_count if metrics else 0,
        synced_count=metrics.synced_count if metrics else 0,
        revised_count=metrics.revised_count if metrics else 0,
        skipped_count=metrics.skipped_count if metrics else 0,
        failed_count=metrics.failed_count if metrics else 0,
        no_change_count=metrics.no_change_count if metrics else 0,
        total_published=int(row.published or 0),
        total_running=int(row.running or 0),
        total_pending=int(row.pending or 0),
        total_error=int(row.error or 0),
        total_skipped=int(row.skipped or 0),
        total_partial=int(row.partial or 0),
    )


@private_router.get("/ebay/orders", response_model=list[OrderListItem], tags=["orders"])
async def ebay_orders_endpoint(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    order_status: str | None = Query(None),
    session: AsyncSession = Depends(get_session),
) -> list[OrderListItem]:
    stmt = select(Order).order_by(
        Order.ebay_created_at.desc().nullslast(), Order.id.desc()
    )
    if order_status:
        stmt = stmt.where(Order.order_status == order_status)
    stmt = stmt.offset(offset).limit(limit)
    result = await session.execute(stmt)
    orders = list(result.scalars().all())
    return [OrderListItem.model_validate(o) for o in orders]


@private_router.get(
    "/ebay/messages",
    response_model=list[BuyerMessageItem],
    tags=["messages"],
    summary="buyer message 一覧（DB）",
)
async def ebay_messages_list_endpoint(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    requires_human: bool | None = Query(None),
    status: str | None = Query(None),
    session: AsyncSession = Depends(get_session),
) -> list[BuyerMessageItem]:
    stmt = select(BuyerMessage).order_by(
        BuyerMessage.received_at.desc().nullslast(), BuyerMessage.id.desc()
    )
    if requires_human is not None:
        stmt = stmt.where(BuyerMessage.requires_human.is_(requires_human))
    if status:
        stmt = stmt.where(BuyerMessage.status == status)
    stmt = stmt.offset(offset).limit(limit)
    rows = (await session.execute(stmt)).scalars().all()
    return [BuyerMessageItem.model_validate(r) for r in rows]


@private_router.post(
    "/ebay/messages/sync",
    response_model=MessageSyncResponse,
    tags=["messages"],
    summary="eBayからbuyer messageを取得→分類→下書き生成",
)
async def ebay_messages_sync_endpoint(
    session: AsyncSession = Depends(get_session),
) -> MessageSyncResponse:
    counters = await sync_messages(session)
    return MessageSyncResponse(**counters)


@private_router.post(
    "/ebay/messages/auto-reply",
    response_model=AutoReplyResponse,
    tags=["messages"],
    summary="下書きの承認/送信（既定: 人間承認後のみ送信）",
)
async def ebay_messages_auto_reply_endpoint(
    payload: AutoReplyRequest,
    session: AsyncSession = Depends(get_session),
) -> AutoReplyResponse:
    msg = await session.get(BuyerMessage, payload.message_id)
    if msg is None:
        raise HTTPException(status_code=404, detail="message not found")

    if payload.approve:
        msg.status = MessageStatus.APPROVED.value
        await session.commit()

    try:
        result = await send_reply(
            message_id=payload.message_id, session=session, force=payload.force,
        )
    except ListingError as e:
        raise HTTPException(status_code=502, detail=f"send failed: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"予期しないエラー: {e}")

    msg = await session.get(BuyerMessage, payload.message_id)
    return AutoReplyResponse(
        success=bool(result.get("success")),
        skipped=bool(result.get("skipped", False)),
        reason=result.get("reason"),
        message_id=payload.message_id,
        status=msg.status if msg else None,
        draft=msg.draft_reply if msg else None,
        confidence=msg.confidence if msg else None,
        category=msg.category if msg else None,
        requires_human=msg.requires_human if msg else None,
    )


@private_router.get(
    "/monitoring/health",
    response_model=HealthResponse,
    tags=["monitoring"],
    summary="DB/Redis/scheduler のヘルスチェック",
)
async def monitoring_health_endpoint(
    session: AsyncSession = Depends(get_session),
) -> HealthResponse:
    return HealthResponse(**(await get_health(session)))


@private_router.get(
    "/monitoring/metrics",
    response_model=MetricsResponse,
    tags=["monitoring"],
    summary="過去24hの publish/sync/repricing/orders/messages 集計",
)
async def monitoring_metrics_endpoint(
    session: AsyncSession = Depends(get_session),
) -> MetricsResponse:
    return MetricsResponse(**(await get_metrics(session)))


@private_router.post(
    "/monitoring/alert",
    response_model=AlertResponse,
    tags=["monitoring"],
    summary="Slack/Discord webhook へアラート送信（webhook未設定なら no-op）",
)
async def monitoring_alert_endpoint(payload: AlertRequest) -> AlertResponse:
    return AlertResponse(**(await send_alert(
        message=payload.message, severity=payload.severity,
    )))


@private_router.post(
    "/ebay/orders/{order_id}/tracking",
    response_model=TrackingUploadResponse,
    tags=["fulfillment"],
    summary="tracking upload（DB反映＋eBay Fulfillment APIへPOST）",
)
async def ebay_orders_tracking_endpoint(
    order_id: str,
    payload: TrackingUploadRequest,
    session: AsyncSession = Depends(get_session),
) -> TrackingUploadResponse:
    try:
        result = await apply_tracking_to_order(
            session,
            order_id_or_ebay_id=order_id,
            tracking_number=payload.tracking_number,
            carrier_code=payload.carrier_code,
            upload_to_ebay=payload.upload_to_ebay,
        )
    except ListingError as e:
        raise HTTPException(status_code=404, detail=f"tracking failed: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"予期しないエラー: {e}")
    return TrackingUploadResponse(**result)


@private_router.post(
    "/ebay/webhooks/tracking",
    tags=["fulfillment"],
    summary="webhook 入口（CJなど外部システム→tracking反映 placeholder）",
)
async def ebay_tracking_webhook_endpoint(
    payload: dict,
    session: AsyncSession = Depends(get_session),
) -> dict:
    order_ref = (
        payload.get("ebay_order_id") or payload.get("order_id") or ""
    )
    tn = payload.get("tracking_number") or payload.get("tracking") or ""
    carrier = payload.get("carrier_code") or payload.get("carrier") or ""
    if not order_ref or not tn or not carrier:
        raise HTTPException(
            status_code=400,
            detail="missing fields: ebay_order_id / tracking_number / carrier_code",
        )
    try:
        return await apply_tracking_to_order(
            session,
            order_id_or_ebay_id=str(order_ref),
            tracking_number=str(tn),
            carrier_code=str(carrier),
            upload_to_ebay=True,
        )
    except ListingError as e:
        raise HTTPException(status_code=404, detail=f"webhook failed: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"予期しないエラー: {e}")


@private_router.post(
    "/ebay/repricing/run",
    tags=["repricing"],
    summary="bulk repricing（dry_runで安全に試せる）",
)
async def ebay_repricing_run_endpoint(
    dry_run: bool = Query(False),
    limit: int = Query(200, ge=1, le=2000),
    session: AsyncSession = Depends(get_session),
) -> dict:
    try:
        return await bulk_reprice(session, dry_run=dry_run, limit=limit)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"repricing failed: {e}")


@private_router.post(
    "/ebay/orders/sync",
    response_model=OrderSyncResponse,
    tags=["orders"],
    summary="eBay Fulfillment APIから注文を同期（incremental）",
)
async def ebay_orders_sync_endpoint(
    session: AsyncSession = Depends(get_session),
) -> OrderSyncResponse:
    try:
        counters = await sync_orders(session)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"order sync failed: {e}")
    return OrderSyncResponse(**counters)


@private_router.get("/ebay/orders/{order_id}", response_model=OrderDetail, tags=["orders"])
async def ebay_order_detail_endpoint(
    order_id: str,
    session: AsyncSession = Depends(get_session),
) -> OrderDetail:
    stmt = select(Order).where(Order.ebay_order_id == order_id)
    result = await session.execute(stmt)
    order = result.scalar_one_or_none()
    if order is None and order_id.isdigit():
        order = await session.get(Order, int(order_id))
    if order is None:
        raise HTTPException(status_code=404, detail=f"order not found: {order_id}")
    return OrderDetail.model_validate(order)


@private_router.get("/ebay/sold-sync-metrics", response_model=SoldSyncMetricsResponse, tags=["orders"])
async def ebay_sold_sync_metrics_endpoint(
    session: AsyncSession = Depends(get_session),
) -> SoldSyncMetricsResponse:
    metrics = get_sold_metrics()
    counts_stmt = select(
        func.count(Order.id).label("total"),
        func.count().filter(Order.order_status == OrderStatus.DETECTED.value).label("detected"),
        func.count().filter(Order.order_status == OrderStatus.PROCESSING.value).label("processing"),
        func.count().filter(Order.order_status == OrderStatus.COMPLETED.value).label("completed"),
        func.count().filter(Order.order_status == OrderStatus.FAILED.value).label("failed"),
    )
    row = (await session.execute(counts_stmt)).one()
    return SoldSyncMetricsResponse(
        last_run_started_at=metrics.started_at if metrics else None,
        last_run_finished_at=metrics.finished_at if metrics else None,
        duration_seconds=metrics.duration_seconds if metrics else None,
        orders_fetched=metrics.orders_fetched if metrics else 0,
        sold_detected=metrics.sold_detected if metrics else 0,
        order_created=metrics.order_created if metrics else 0,
        duplicate_skipped=metrics.duplicate_skipped if metrics else 0,
        unknown_sku=metrics.unknown_sku if metrics else 0,
        failed=metrics.failed if metrics else 0,
        total_orders=int(row.total or 0),
        total_detected=int(row.detected or 0),
        total_processing=int(row.processing or 0),
        total_completed=int(row.completed or 0),
        total_failed=int(row.failed or 0),
    )


# ── Fulfillment ─────────────────────
@private_router.get(
    "/fulfillment/tasks",
    response_model=list[FulfillmentTaskListItem],
    tags=["fulfillment"],
)
async def fulfillment_tasks_list_endpoint(
    task_status: str | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> list[FulfillmentTaskListItem]:
    tasks = await list_tasks(
        task_status=task_status, limit=limit, offset=offset, session=session
    )
    return [FulfillmentTaskListItem.model_validate(t) for t in tasks]


@private_router.get(
    "/fulfillment/tasks/{task_id}",
    response_model=FulfillmentTaskDetail,
    tags=["fulfillment"],
)
async def fulfillment_task_detail_endpoint(
    task_id: int,
    session: AsyncSession = Depends(get_session),
) -> FulfillmentTaskDetail:
    try:
        task = await get_task(task_id, session)
    except TaskNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return _to_detail(task)


@private_router.post(
    "/fulfillment/tasks/{task_id}/approve",
    response_model=FulfillmentTaskDetail,
    tags=["fulfillment"],
)
async def fulfillment_approve_endpoint(
    task_id: int,
    payload: TaskApproveRequest,
    session: AsyncSession = Depends(get_session),
) -> FulfillmentTaskDetail:
    try:
        task = await approve_task(
            task_id, assigned_to=payload.assigned_to,
            notes=payload.notes, session=session,
        )
    except TaskNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except TaskTransitionError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return _to_detail(task)


@private_router.post(
    "/fulfillment/tasks/{task_id}/mark-purchased",
    response_model=FulfillmentTaskDetail,
    tags=["fulfillment"],
)
async def fulfillment_mark_purchased_endpoint(
    task_id: int,
    payload: TaskActionRequest,
    session: AsyncSession = Depends(get_session),
) -> FulfillmentTaskDetail:
    try:
        task = await mark_purchased(
            task_id, notes=payload.notes,
            tracking_number=payload.tracking_number, session=session,
        )
    except TaskNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except TaskTransitionError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return _to_detail(task)


@private_router.post(
    "/fulfillment/tasks/{task_id}/mark-shipped",
    response_model=FulfillmentTaskDetail,
    tags=["fulfillment"],
)
async def fulfillment_mark_shipped_endpoint(
    task_id: int,
    payload: TaskActionRequest,
    session: AsyncSession = Depends(get_session),
) -> FulfillmentTaskDetail:
    try:
        task = await mark_shipped(
            task_id, notes=payload.notes,
            tracking_number=payload.tracking_number, session=session,
        )
    except TaskNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except TaskTransitionError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return _to_detail(task)


@private_router.post(
    "/fulfillment/tasks/{task_id}/cancel",
    response_model=FulfillmentTaskDetail,
    tags=["fulfillment"],
)
async def fulfillment_cancel_endpoint(
    task_id: int,
    payload: TaskCancelRequest,
    session: AsyncSession = Depends(get_session),
) -> FulfillmentTaskDetail:
    try:
        task = await cancel_task(
            task_id, reason=payload.reason, session=session,
        )
    except TaskNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except TaskTransitionError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return _to_detail(task)


# ── Shipping ────────────────────────
@private_router.get("/shipping/estimate", response_model=ShippingEstimateResponse, tags=["shipping"])
async def shipping_estimate_endpoint(
    country: str = Query(...),
    weight_gram: int = Query(..., gt=0),
    length_cm: int | None = Query(None, ge=1),
    width_cm: int | None = Query(None, ge=1),
    height_cm: int | None = Query(None, ge=1),
) -> ShippingEstimateResponse:
    estimate = estimate_shipping(
        country=country, weight_gram=weight_gram,
        length_cm=length_cm, width_cm=width_cm, height_cm=height_cm,
    )
    return estimate.to_response()


@private_router.post(
    "/shipping/recalculate/{task_id}",
    response_model=FulfillmentTaskDetail,
    tags=["shipping"],
)
async def shipping_recalculate_endpoint(
    task_id: int,
    session: AsyncSession = Depends(get_session),
) -> FulfillmentTaskDetail:
    try:
        task, _estimate = await recalculate_shipping_for_task(task_id, session)
    except TaskNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except FulfillmentError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _to_detail(task)


# ── Country Risk ─────────────────────
@private_router.get(
    "/risk/countries",
    response_model=CountryRiskListResponse,
    tags=["risk"],
    summary="国別リスク mapping の一覧",
)
async def risk_countries_endpoint() -> CountryRiskListResponse:
    return CountryRiskListResponse(
        blocked=sorted(get_blocked_countries()),
        high=sorted(get_high_risk_countries()),
        medium=sorted(get_medium_risk_countries()),
        score_table=dict(LEVEL_TO_SCORE),
        buffer_adjustment_percent=get_buffer_adjustment_percent_table(),
    )


@private_router.get(
    "/risk/tasks",
    response_model=list[FulfillmentTaskListItem],
    tags=["risk"],
    summary="リスクレベル別 fulfillment task 一覧",
)
async def risk_tasks_endpoint(
    risk_level: str | None = Query(
        None, description="low/medium/high/blocked のいずれか"
    ),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> list[FulfillmentTaskListItem]:
    tasks = await list_tasks_by_risk(
        risk_level=risk_level, limit=limit, offset=offset, session=session,
    )
    return [FulfillmentTaskListItem.model_validate(t) for t in tasks]


@private_router.get(
    "/cj/search",
    tags=["cj"],
    summary="CJ Dropshipping 商品検索",
)
async def cj_search_endpoint(
    keyword: str = Query(..., min_length=1, description="検索キーワード（英語）"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> dict:
    try:
        return await cj_search(keyword, page=page, page_size=page_size)
    except CJError as e:
        raise HTTPException(status_code=502, detail=f"CJ API error: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"予期しないエラー: {e}")


@private_router.get(
    "/cj/product/{pid}",
    tags=["cj"],
    summary="CJ 商品詳細（画像URL含む）",
)
async def cj_product_endpoint(pid: str) -> dict:
    try:
        return await cj_product_detail(pid)
    except CJError as e:
        raise HTTPException(status_code=502, detail=f"CJ API error: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"予期しないエラー: {e}")


@private_router.get(
    "/cj/product/{pid}/images",
    tags=["cj"],
    summary="CJ 商品の画像URL一覧",
)
async def cj_product_images_endpoint(pid: str) -> dict:
    try:
        data = await cj_product_detail(pid)
    except CJError as e:
        raise HTTPException(status_code=502, detail=f"CJ API error: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"予期しないエラー: {e}")
    return {"pid": pid, "image_urls": extract_image_urls(data)}


# ─────────────────────────────────────
# CJ 発注（eBay で売れた → CJ に反映）
# ─────────────────────────────────────
@private_router.post(
    "/cj/orders/{ebay_order_id}/place",
    response_model=CJOrderPlaceResponse,
    tags=["cj"],
    summary="eBay 注文を CJ に発注（冪等 / dry_run 対応）",
)
async def cj_order_place_endpoint(
    ebay_order_id: str,
    payload: CJOrderPlaceRequest | None = None,
    session: AsyncSession = Depends(get_session),
) -> CJOrderPlaceResponse:
    body = payload or CJOrderPlaceRequest()
    try:
        result = await place_cj_order_for_ebay_order(
            session, ebay_order_id, dry_run=body.dry_run, force=body.force,
        )
    except CJOrderError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except CJError as e:
        raise HTTPException(status_code=502, detail=f"CJ API error: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"予期しないエラー: {e}")
    return CJOrderPlaceResponse(**result)


@private_router.get(
    "/cj/orders",
    response_model=list[CJOrderItem],
    tags=["cj"],
    summary="CJ 発注一覧（status で絞り込み）",
)
async def cj_orders_list_endpoint(
    status_filter: str | None = Query(
        None,
        alias="status",
        description="pending/dry_run/created/shipped/delivered/canceled/unmapped/failed",
    ),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> list[CJOrderItem]:
    rows = await list_cj_orders(
        session, status=status_filter, limit=limit, offset=offset
    )
    return [CJOrderItem.model_validate(r) for r in rows]


@private_router.get(
    "/cj/orders/{ebay_order_id}",
    response_model=list[CJOrderItem],
    tags=["cj"],
    summary="eBay 注文に紐づく CJ 発注の状態（分割時は小包ごとに複数）",
)
async def cj_order_detail_endpoint(
    ebay_order_id: str,
    session: AsyncSession = Depends(get_session),
) -> list[CJOrderItem]:
    try:
        cj_orders = await get_cj_orders_for_ebay_order(session, ebay_order_id)
    except CJOrderError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return [CJOrderItem.model_validate(c) for c in cj_orders]


@private_router.get(
    "/cj/customs-risk",
    response_model=CustomsRiskCheckResponse,
    tags=["cj"],
    summary="通関リスクと1小包あたりの上限個数を確認（出品前チェック用）",
)
async def cj_customs_risk_endpoint(
    title: str = Query(..., min_length=1, description="商品タイトル"),
    sku: str = Query("", description="SKU（任意）"),
    quantity: int = Query(1, ge=1, le=100, description="発注予定の数量"),
) -> CustomsRiskCheckResponse:
    risk = assess_customs_risk(title=title, sku=sku)
    return CustomsRiskCheckResponse(
        title=title,
        level=risk.level,
        reasons=risk.reasons,
        matched_keyword=risk.matched_keyword,
        max_units_per_parcel=risk.max_units_per_parcel,
        quantity=quantity,
        parcel_split=split_quantity(quantity, risk.max_units_per_parcel),
    )


@private_router.post(
    "/cj/orders/sync",
    response_model=CJOrderWorkerMetrics,
    tags=["cj"],
    summary="CJ 発注ワーカーを手動実行（未発注の発注＋状態同期＋tracking反映）",
)
async def cj_orders_sync_endpoint() -> CJOrderWorkerMetrics:
    metrics = await run_cj_order_worker()
    return CJOrderWorkerMetrics(**metrics.as_dict())


@private_router.get(
    "/cj/orders/worker/metrics",
    response_model=CJOrderWorkerMetrics,
    tags=["cj"],
    summary="CJ 発注ワーカーの直近実行メトリクス",
)
async def cj_orders_metrics_endpoint() -> CJOrderWorkerMetrics:
    metrics = get_cj_order_metrics()
    if metrics is None:
        return CJOrderWorkerMetrics()
    return CJOrderWorkerMetrics(**metrics.as_dict())


@private_router.get(
    "/cj/mappings/unmapped",
    tags=["cj"],
    summary="売れたのにCJへ発注できないSKU一覧（マッピング未登録）",
)
async def cj_unmapped_skus_endpoint(
    limit: int = Query(200, ge=1, le=1000, description="走査する直近注文数"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    rows = await list_unmapped_skus(session, limit=limit)
    return {
        "unmapped_sku_count": len(rows),
        "affected_order_count": sum(r["order_count"] for r in rows),
        "items": rows,
    }


@private_router.post(
    "/cj/mappings",
    response_model=CJMappingItem,
    tags=["cj"],
    summary="SKU ↔ CJ 商品の紐付けを登録・更新",
)
async def cj_mapping_upsert_endpoint(
    payload: CJMappingUpsertRequest,
    session: AsyncSession = Depends(get_session),
) -> CJMappingItem:
    try:
        mapping = await upsert_mapping(
            session,
            sku=payload.sku,
            cj_product_id=payload.cj_product_id,
            product_id=payload.product_id,
            cj_variant_id=payload.cj_variant_id,
            cj_sku=payload.cj_sku,
            note=payload.note,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"mapping 登録失敗: {e}")
    return CJMappingItem.model_validate(mapping)


@private_router.get(
    "/cj/mappings",
    response_model=list[CJMappingItem],
    tags=["cj"],
    summary="SKU ↔ CJ 商品の紐付け一覧",
)
async def cj_mappings_list_endpoint(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> list[CJMappingItem]:
    rows = await list_mappings(session, limit=limit, offset=offset)
    return [CJMappingItem.model_validate(r) for r in rows]


@private_router.post(
    "/ebay/listing/copy",
    response_model=EbayListingCopyResponse,
    tags=["listing"],
    summary="eBay販売文章をAI生成（dry_run/templateフォールバック対応）",
)
async def ebay_listing_copy_endpoint(
    payload: EbayListingCopyRequest,
    session: AsyncSession = Depends(get_session),
) -> EbayListingCopyResponse:
    product = await session.get(Product, payload.product_id)
    if product is None:
        raise HTTPException(status_code=404, detail=f"product not found: id={payload.product_id}")
    try:
        result = await generate_copy(
            product, dry_run=payload.dry_run, provider_name=payload.provider
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"予期しないエラー: {e}")
    return EbayListingCopyResponse(**result)


@private_router.post(
    "/ebay/listing/preview",
    response_model=EbayListingPreviewResponse,
    tags=["listing"],
    summary="eBay未接続でも最終payloadを確認（API一切呼ばない）",
)
async def ebay_listing_preview_endpoint(
    payload: EbayListingPreviewRequest,
    session: AsyncSession = Depends(get_session),
) -> EbayListingPreviewResponse:
    try:
        result = await generate_preview(
            product_id=payload.product_id,
            cj_product_id=payload.cj_product_id,
            session=session,
        )
    except ListingError as e:
        raise HTTPException(status_code=400, detail=f"preview失敗: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"予期しないエラー: {e}")
    return EbayListingPreviewResponse(**result)


@private_router.post(
    "/ebay/listing/{ref}/primary-image",
    response_model=PrimaryImageResponse,
    tags=["listing"],
    summary="トップ画像を現在のn枚目に差し替え（1始まり / dry_run対応）",
)
async def ebay_listing_primary_image_endpoint(
    ref: str,
    payload: PrimaryImageRequest,
    session: AsyncSession = Depends(get_session),
) -> PrimaryImageResponse:
    try:
        result = await set_primary_image(
            session,
            ref=ref,
            position=payload.position,
            dry_run=payload.dry_run,
            republish=payload.republish,
        )
    except ListingError as e:
        raise HTTPException(status_code=404, detail=f"画像差し替え失敗: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"予期しないエラー: {e}")
    return PrimaryImageResponse(**result)


@private_router.post(
    "/ebay/listings/primary-image/bulk",
    response_model=PrimaryImageBulkResponse,
    tags=["listing"],
    summary="複数listingのトップ画像をまとめて差し替え（1件失敗しても続行）",
)
async def ebay_listings_primary_image_bulk_endpoint(
    payload: PrimaryImageBulkRequest,
    session: AsyncSession = Depends(get_session),
) -> PrimaryImageBulkResponse:
    try:
        result = await bulk_set_primary_image(
            session,
            items=[(i.ref, i.position) for i in payload.items],
            dry_run=payload.dry_run,
            republish=payload.republish,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"予期しないエラー: {e}")
    return PrimaryImageBulkResponse(**result)


@private_router.post(
    "/ebay/listing/{sku}/withdraw",
    response_model=EbayWithdrawResponse,
    tags=["listing"],
    summary="手動でeBay listingを停止（offer withdraw）",
)
async def ebay_listing_withdraw_endpoint(
    sku: str,
    session: AsyncSession = Depends(get_session),
) -> EbayWithdrawResponse:
    try:
        result = await withdraw_by_sku(sku, session)
    except ListingError as e:
        raise HTTPException(status_code=404, detail=f"withdraw失敗: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"予期しないエラー: {e}")
    return EbayWithdrawResponse(**result)


@private_router.post(
    "/ebay/listing/publish",
    response_model=EbayListingPublishResponse,
    tags=["listing"],
    summary="本番出品（preview→inventory→offer→publish, dry_run対応）",
)
async def ebay_listing_publish_endpoint(
    payload: EbayListingPublishRequest,
    session: AsyncSession = Depends(get_session),
) -> EbayListingPublishResponse:
    try:
        result = await publish_listing(
            product_id=payload.product_id,
            cj_product_id=payload.cj_product_id,
            marketplace_id=payload.marketplace_id,
            session=session,
            dry_run=payload.dry_run,
        )
    except ListingError as e:
        raise HTTPException(status_code=502, detail=f"出品失敗: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"予期しないエラー: {e}")
    return EbayListingPublishResponse(**result)


@private_router.post(
    "/ebay/listing/create",
    response_model=EbayListingCreateResponse,
    tags=["listing"],
    summary="CJ画像を取得してeBay出品（dry_run対応）",
)
async def ebay_listing_create_endpoint(
    payload: EbayListingCreateRequest,
    session: AsyncSession = Depends(get_session),
) -> EbayListingCreateResponse:
    try:
        result = await create_listing_with_cj_images(
            product_id=payload.product_id,
            cj_product_id=payload.cj_product_id,
            session=session,
            dry_run=payload.dry_run,
        )
    except ListingError as e:
        raise HTTPException(status_code=502, detail=f"出品失敗: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"予期しないエラー: {e}")
    return EbayListingCreateResponse(**result)


def _to_detail(task) -> FulfillmentTaskDetail:
    detail = FulfillmentTaskDetail.model_validate(task)
    detail.profit_breakdown = build_profit_breakdown(task)
    return detail
