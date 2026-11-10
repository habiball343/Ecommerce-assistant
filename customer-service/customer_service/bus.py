"""实时事件总线（发布/订阅）。

- RedisBus：使用 Redis Pub/Sub，标准部署方式，支持多进程 / 多实例部署；
- MemoryBus：进程内实现，本地零依赖运行时使用。

两者接口一致，业务代码不关心具体实现。
"""

import asyncio
import logging
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Protocol

logger = logging.getLogger(__name__)


class EventBus(Protocol):
    name: str

    async def publish(self, channel: str, payload: str) -> None: ...

    def subscribe(self, channel: str): ...

    async def ping(self) -> bool: ...

    async def close(self) -> None: ...


class MemoryBus:
    name = "memory"

    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue[str]]] = defaultdict(set)

    async def publish(self, channel: str, payload: str) -> None:
        for queue in list(self._subscribers.get(channel, ())):
            queue.put_nowait(payload)

    @asynccontextmanager
    async def subscribe(self, channel: str) -> AsyncIterator[AsyncIterator[str]]:
        queue: asyncio.Queue[str] = asyncio.Queue()
        self._subscribers[channel].add(queue)

        async def iterator() -> AsyncIterator[str]:
            while True:
                yield await queue.get()

        try:
            yield iterator()
        finally:
            self._subscribers[channel].discard(queue)
            if not self._subscribers[channel]:
                self._subscribers.pop(channel, None)

    async def ping(self) -> bool:
        return True

    async def close(self) -> None:
        self._subscribers.clear()


class RedisBus:
    name = "redis"

    def __init__(self, redis_url: str) -> None:
        import redis.asyncio as redis

        self._client = redis.from_url(redis_url, decode_responses=True)

    async def publish(self, channel: str, payload: str) -> None:
        await self._client.publish(channel, payload)

    @asynccontextmanager
    async def subscribe(self, channel: str) -> AsyncIterator[AsyncIterator[str]]:
        pubsub = self._client.pubsub()
        await pubsub.subscribe(channel)

        async def iterator() -> AsyncIterator[str]:
            async for item in pubsub.listen():
                if item["type"] == "message":
                    yield item["data"]

        try:
            yield iterator()
        finally:
            try:
                await pubsub.unsubscribe(channel)
                await pubsub.aclose()
            except Exception:  # noqa: BLE001 - 连接已断开时忽略清理错误
                logger.debug("关闭 Redis 订阅时出错", exc_info=True)

    async def ping(self) -> bool:
        try:
            return bool(await self._client.ping())
        except Exception:  # noqa: BLE001
            return False

    async def close(self) -> None:
        await self._client.aclose()


def create_bus(redis_url: str) -> EventBus:
    return RedisBus(redis_url) if redis_url.strip() else MemoryBus()


def user_channel(user_id: str) -> str:
    return f"user:{user_id}"


STAFF_CHANNEL = "staff"
