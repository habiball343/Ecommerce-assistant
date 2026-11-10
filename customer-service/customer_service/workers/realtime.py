"""实时推送 Worker：把发件箱中的事件发布到事件总线（Redis Pub/Sub 或进程内总线）。"""

import asyncio
import json
import logging
from datetime import timedelta

from customer_service.bus import EventBus
from customer_service.db import session_factory
from customer_service.services.realtime import RealtimeService
from customer_service.settings import get_settings
from customer_service.signals import wait_realtime
from customer_service.utils import utcnow

logger = logging.getLogger(__name__)


class RealtimeWorker:
    def __init__(self, bus: EventBus):
        self._settings = get_settings()
        self._bus = bus

    async def run(self) -> None:
        logger.info("Realtime Worker 已启动（事件总线：%s）", self._bus.name)
        poll_seconds = self._settings.realtime_worker_poll_interval_ms / 1000
        while True:
            try:
                published = await self._publish_pending()
            except Exception:  # noqa: BLE001 - 总线暂时不可用时稍后重试，事件仍保留在发件箱
                logger.exception("发布实时事件失败，稍后重试")
                await asyncio.sleep(1.0)
                continue
            if not published:
                await wait_realtime(poll_seconds)

    async def _publish_pending(self) -> int:
        async with session_factory() as session:
            service = RealtimeService(session)
            events = await service.list_unpublished()
            if not events:
                await session.commit()
                return 0
            published = 0
            try:
                for event in events:
                    await self._bus.publish(event.target, json.dumps(event.data, ensure_ascii=False))
                    event.published_at = utcnow()
                    published += 1
            finally:
                # 已发布的先落库，避免重复推送
                await session.commit()
            return published

    async def purge_old_events(self) -> None:
        async with session_factory() as session:
            await RealtimeService(session).purge_published(utcnow() - timedelta(days=1))
            await session.commit()
