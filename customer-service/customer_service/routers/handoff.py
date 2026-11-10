from typing import Annotated, Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from customer_service.auth import AdminDep, StaffDep
from customer_service.db import get_session
from customer_service.schemas import ReplyRequest
from customer_service.services.handoff import HandoffService
from customer_service.signals import notify_realtime

router = APIRouter(prefix="/api/v1", tags=["人工工作台与运行指标"])


def get_handoff_service(session: Annotated[AsyncSession, Depends(get_session)]) -> HandoffService:
    return HandoffService(session)


HandoffServiceDep = Annotated[HandoffService, Depends(get_handoff_service)]


@router.get("/handoffs")
async def list_handoffs(service: HandoffServiceDep, _: StaffDep) -> list[dict[str, Any]]:
    """等待接入与处理中的人工工单。"""
    return await service.list_open_handoffs()


@router.get("/handoffs/{handoff_id}/messages")
async def get_handoff_messages(handoff_id: str, service: HandoffServiceDep, _: StaffDep) -> dict[str, Any]:
    return await service.get_messages(handoff_id)


@router.post("/handoffs/{handoff_id}/accept")
async def accept_handoff(handoff_id: str, service: HandoffServiceDep, staff: StaffDep) -> dict[str, Any]:
    result = await service.accept(handoff_id, staff.user_id)
    notify_realtime()
    return result


@router.post("/handoffs/{handoff_id}/messages")
async def reply_handoff(
    handoff_id: str, payload: ReplyRequest, service: HandoffServiceDep, staff: StaffDep
) -> dict[str, Any]:
    result = await service.reply(handoff_id, staff.user_id, payload.text)
    notify_realtime()
    return result


@router.post("/handoffs/{handoff_id}/resolve")
async def resolve_handoff(handoff_id: str, service: HandoffServiceDep, staff: StaffDep) -> dict[str, Any]:
    result = await service.resolve(handoff_id, staff.user_id)
    notify_realtime()
    return result


@router.get("/admin/metrics")
async def admin_metrics(service: HandoffServiceDep, _: AdminDep) -> dict[str, Any]:
    """管理员查看的会话与人工介入指标。"""
    return await service.metrics()
