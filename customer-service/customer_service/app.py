import asyncio
import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from customer_service.bus import create_bus
from customer_service.db import engine, session_factory
from customer_service.models import Base
from customer_service.routers import chat, handoff, realtime
from customer_service.services.chat import ChatService
from customer_service.settings import get_settings
from customer_service.workers.ai import AIWorker
from customer_service.workers.realtime import RealtimeWorker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
logger = logging.getLogger("customer_service")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()

    # 1. 建表（已存在则跳过），并把上次未处理完的轮次放回队列
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with session_factory() as session:
        recovered = await ChatService(session).recover_stale_turns()
        if recovered:
            logger.info("恢复了 %s 个未完成的 AI 轮次", recovered)

    # 2. 事件总线与后台 Worker
    bus = create_bus(settings.redis_url)
    if not await bus.ping():
        logger.warning("事件总线 %s 暂时不可用，实时事件会保留在发件箱中稍后重试", bus.name)
    app.state.bus = bus
    http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(settings.ai_timeout_seconds, connect=5.0),
        trust_env=False,  # 服务间调用不走系统代理
    )
    realtime_worker = RealtimeWorker(bus)
    await realtime_worker.purge_old_events()
    workers = [
        asyncio.create_task(AIWorker(http_client).run(), name="ai-worker"),
        asyncio.create_task(realtime_worker.run(), name="realtime-worker"),
    ]
    logger.info("客服服务已启动：数据库 %s，事件总线 %s", engine.dialect.name, bus.name)

    yield

    for worker in workers:
        worker.cancel()
    await asyncio.gather(*workers, return_exceptions=True)
    await http_client.aclose()
    await bus.close()
    await engine.dispose()


app = FastAPI(title="Customer Service", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(chat.router)
app.include_router(handoff.router)
app.include_router(realtime.router)


@app.get("/health", tags=["健康检查"])
async def health() -> dict:
    return {
        "status": "ok",
        "service": "customer-service",
        "database": engine.dialect.name,
        "event_bus": app.state.bus.name,
    }
