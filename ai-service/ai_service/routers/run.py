from typing import Any

from fastapi import APIRouter, Request

from ai_service.auth import InternalDep, UserDep
from ai_service.schemas import AgentRunRequest

router = APIRouter(tags=["内部接口：处理一轮咨询"])


@router.post("/internal/v1/agent/runs")
async def start_run(payload: AgentRunRequest, user: UserDep, _: InternalDep, request: Request) -> dict[str, Any]:
    """客服服务把合并后的一轮用户消息交给 AI 处理。

    需要两种凭证：内部服务令牌（证明调用方是客服服务）和用户令牌（查询电商数据时使用该用户的身份）。
    """
    return await request.app.state.executor.start_run(payload, user)
