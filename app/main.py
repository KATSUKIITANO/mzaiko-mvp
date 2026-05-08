import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.routes import private_router, public_router
from app.config import settings
from app.db.database import init_db
from app.services.cache_service import close_redis, init_redis
from app.services.scheduler_service import start_scheduler, stop_scheduler


def _setup_logging() -> None:
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


_setup_logging()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(
        "starting up: init db & redis & scheduler (auth_enabled=%s)",
        settings.api_auth_enabled,
    )
    await init_db()
    await init_redis()
    await start_scheduler()
    logger.info("startup complete")
    try:
        yield
    finally:
        logger.info("shutting down")
        await stop_scheduler()
        await close_redis()


app = FastAPI(title="無在庫販売MVP", version="0.7.0", lifespan=lifespan)

# 認証なしルーター（/scrape, /calculate-profit, /ebay/search, /monitoring/products）
app.include_router(public_router)

# 認証必須ルーター（/analyze-product, /bulk-analyze, /amazon/ranking, /export/*, /dashboard/*）
app.include_router(private_router)


@app.get("/")
async def root() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "healthy"}
