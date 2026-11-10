import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from ai_service.agent.executor import AgentExecutor
from ai_service.agent.llm_engine import LLMEngine
from ai_service.db import engine
from ai_service.models import Base
from ai_service.routers import admin, run
from ai_service.services.knowledge import KnowledgeService
from ai_service.settings import get_settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
logger = logging.getLogger("ai_service")


def _build_llm_engine() -> LLMEngine | None:
    settings = get_settings()
    if settings.agent_engine == "rules":
        logger.info("回答引擎：规则引擎（AGENT_ENGINE=rules）")
        return None
    if not settings.llm_configured:
        logger.info("回答引擎：规则引擎（未配置 LLM_API_KEY；在 .env 中填写后重启即可启用大模型）")
        return None
    try:
        llm_engine = LLMEngine(settings)
    except Exception:  # noqa: BLE001 - 模型配置有误时不影响服务启动，退回规则引擎
        logger.exception("大模型引擎初始化失败，退回规则引擎")
        return None
    logger.info(
        "回答引擎：大模型 %s（%s），规则引擎作为降级方案", settings.llm_model, settings.llm_provider
    )
    return llm_engine


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    # 服务间调用不走系统代理
    http_client = httpx.AsyncClient(trust_env=False, timeout=10.0)
    knowledge = KnowledgeService(http_client)
    seeded = await knowledge.seed_builtin()
    if seeded:
        logger.info("知识库为空，已写入内置平台规则（%s 个切片）", seeded)

    app.state.knowledge = knowledge
    app.state.executor = AgentExecutor(
        http_client=http_client, knowledge=knowledge, llm_engine=_build_llm_engine()
    )
    logger.info("智能服务已启动：数据库 %s", engine.dialect.name)

    yield

    await http_client.aclose()
    await engine.dispose()


app = FastAPI(title="AI Service", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(run.router)
app.include_router(admin.router)


@app.get("/health", tags=["健康检查"])
async def health() -> dict:
    settings = get_settings()
    return {
        "status": "ok",
        "service": "ai-service",
        "database": engine.dialect.name,
        "engine": app.state.executor.engine_label,
        "llm_enabled": app.state.executor.engine_label != "rules-engine",
        "knowledge_chunks": await app.state.knowledge.count(),
        "prompt_version": settings.prompt_version,
    }
