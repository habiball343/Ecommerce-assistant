from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from ai_service.auth import AdminDep
from ai_service.db import get_session
from ai_service.services.admin import AdminService
from ai_service.services.knowledge import KnowledgeError, KnowledgeService

router = APIRouter(prefix="/api/v1", tags=["管理端：运行观测与知识库"])


def get_admin_service(session: Annotated[AsyncSession, Depends(get_session)]) -> AdminService:
    return AdminService(session)


def get_knowledge_service(request: Request) -> KnowledgeService:
    return request.app.state.knowledge


AdminServiceDep = Annotated[AdminService, Depends(get_admin_service)]
KnowledgeServiceDep = Annotated[KnowledgeService, Depends(get_knowledge_service)]


@router.get("/admin/metrics")
async def get_metrics(service: AdminServiceDep, _: AdminDep) -> dict[str, Any]:
    """AI 运行次数、平均耗时与 Token 用量。"""
    return await service.metrics()


@router.get("/runs")
async def list_runs(service: AdminServiceDep, _: AdminDep, limit: int = 50) -> list[dict[str, Any]]:
    return await service.list_runs(limit)


@router.get("/runs/{run_id}")
async def get_run(run_id: str, service: AdminServiceDep, _: AdminDep) -> dict[str, Any]:
    """一次运行的最终结果与只读工具调用审计。"""
    return await service.get_run(run_id)


@router.get("/knowledge")
async def list_knowledge(service: KnowledgeServiceDep, _: AdminDep) -> list[dict[str, Any]]:
    return await service.list_chunks()


@router.post("/knowledge/upload")
async def upload_knowledge(service: KnowledgeServiceDep, _: AdminDep, file: UploadFile) -> dict[str, Any]:
    """上传平台规则文档（PDF）：提取文字 → 切片 → 入库（配置了 Embedding 时同时写入向量）。"""
    file_name = file.filename or "未命名文档.pdf"
    try:
        chunks = await service.ingest_upload(file_name, await file.read())
    except KnowledgeError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    return {"file_name": file_name, "chunks": chunks}


@router.delete("/knowledge/{file_name}")
async def delete_knowledge(file_name: str, service: KnowledgeServiceDep, _: AdminDep) -> dict[str, Any]:
    """按文件名删除一份知识文档的全部切片。"""
    return {"file_name": file_name, "deleted": await service.delete_file(file_name)}
