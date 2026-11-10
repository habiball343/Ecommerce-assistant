"""WebSocket 实时通道：用户端收自己的消息，管理端（客服）收工单与会话动态。"""

import asyncio
import logging

from fastapi import APIRouter, WebSocket, status

from customer_service.auth import decode_token
from customer_service.bus import STAFF_CHANNEL, EventBus, user_channel

logger = logging.getLogger(__name__)
router = APIRouter()

AUTH_TIMEOUT_SECONDS = 10


@router.websocket("/api/v1/realtime")
async def realtime_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()

    # 1. 连接建立后的第一条消息必须是身份认证：{"type": "authenticate", "token": "..."}
    try:
        auth_message = await asyncio.wait_for(websocket.receive_json(), AUTH_TIMEOUT_SECONDS)
        current_user = decode_token(str(auth_message.get("token") or ""))
    except Exception:  # noqa: BLE001
        await _safe_close(websocket, status.WS_1008_POLICY_VIOLATION, "authentication failed")
        return

    # 2. 普通用户订阅自己的频道；客服 / 管理员订阅公共的 staff 频道
    channel = user_channel(current_user.user_id) if current_user.role == "user" else STAFF_CHANNEL
    bus: EventBus = websocket.app.state.bus

    try:
        async with bus.subscribe(channel) as events:
            # 先订阅成功再告知前端“已连接”，保证之后的事件不会漏掉
            await websocket.send_json({"event_type": "connected"})

            async def forward_events() -> None:
                async for payload in events:
                    await websocket.send_text(payload)

            async def watch_disconnect() -> None:
                while True:
                    await websocket.receive_text()

            tasks = [asyncio.create_task(forward_events()), asyncio.create_task(watch_disconnect())]
            _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    except Exception:  # noqa: BLE001
        logger.warning("实时通道异常关闭（频道 %s）", channel, exc_info=True)
    finally:
        await _safe_close(websocket, status.WS_1011_INTERNAL_ERROR, "realtime channel closed")


async def _safe_close(websocket: WebSocket, code: int, reason: str) -> None:
    try:
        await websocket.close(code=code, reason=reason)
    except Exception:  # noqa: BLE001 - 连接可能已经断开
        pass
