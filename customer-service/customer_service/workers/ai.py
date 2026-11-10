"""AI Worker：领取收集完成的轮次 → 调用 AI Service → 结算（保存回复或降级）。"""

import asyncio
import logging
from typing import Any

import httpx

from customer_service.auth import encode_user_token
from customer_service.db import session_factory
from customer_service.services.chat import ChatService, ClaimedTurn
from customer_service.settings import get_settings

logger = logging.getLogger(__name__)


class AIWorker:
    def __init__(self, http_client: httpx.AsyncClient):
        self._settings = get_settings()
        self._http = http_client
        self._tasks: set[asyncio.Task] = set()

    async def run(self) -> None:
        logger.info("AI Worker 已启动（并发 %s）", self._settings.ai_worker_concurrency)
        slots = asyncio.Semaphore(self._settings.ai_worker_concurrency)
        poll_seconds = self._settings.ai_worker_poll_interval_ms / 1000
        try:
            while True:
                await slots.acquire()
                claimed = None
                try:
                    claimed = await self._claim()
                except Exception:  # noqa: BLE001 - Worker 不能因为一次数据库错误退出
                    logger.exception("领取轮次失败")
                if claimed is None:
                    slots.release()
                    await asyncio.sleep(poll_seconds)
                    continue
                task = asyncio.create_task(self._process(claimed))
                self._tasks.add(task)
                task.add_done_callback(lambda done: (self._tasks.discard(done), slots.release()))
        finally:
            for task in self._tasks:
                task.cancel()

    async def _claim(self) -> ClaimedTurn | None:
        async with session_factory() as session:
            return await ChatService(session).claim_next_turn()

    async def _process(self, claimed: ClaimedTurn) -> None:
        # 调用 AI 期间不占用数据库连接，结算时再开新的会话
        result, error = await self._call_ai_service(claimed)
        try:
            async with session_factory() as session:
                await ChatService(session).finish_turn(claimed.turn_id, result, error)
        except Exception:  # noqa: BLE001
            logger.exception("结算轮次 %s 失败", claimed.turn_id)

    async def _call_ai_service(self, claimed: ClaimedTurn) -> tuple[Any, str | None]:
        headers = {
            # 用户身份：AI Service 查询电商数据时只能看到该用户自己的订单
            "Authorization": f"Bearer {encode_user_token(claimed.user_id)}",
            # 服务间信任：内部接口只接受客服服务的调用
            "X-Internal-Service-Token": self._settings.internal_service_token,
        }
        try:
            response = await self._http.post(
                f"{self._settings.ai_service_url}/internal/v1/agent/runs",
                headers=headers,
                json=claimed.payload,
            )
            response.raise_for_status()
            return response.json(), None
        except Exception as exc:  # noqa: BLE001
            logger.warning("会话 %s 调用 AI Service 失败：%r", claimed.conversation_id, exc)
            return None, repr(exc)
