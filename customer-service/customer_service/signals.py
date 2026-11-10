"""进程内的轻量通知：有新的实时事件入库时唤醒发布 Worker，减少推送延迟。"""

import asyncio

_realtime_wakeup = asyncio.Event()


def notify_realtime() -> None:
    _realtime_wakeup.set()


async def wait_realtime(timeout_seconds: float) -> None:
    try:
        await asyncio.wait_for(_realtime_wakeup.wait(), timeout_seconds)
    except TimeoutError:
        pass
    _realtime_wakeup.clear()
