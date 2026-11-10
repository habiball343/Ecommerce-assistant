"""启动入口：python -m ai_service.main"""

import asyncio
import selectors
import sys

import uvicorn

from ai_service.settings import get_settings


def main() -> None:
    settings = get_settings()
    server = uvicorn.Server(uvicorn.Config("ai_service.app:app", host=settings.api_host, port=settings.api_port))
    if sys.platform == "win32":
        # psycopg 的异步模式不支持 Windows 默认的 ProactorEventLoop，这里统一使用 SelectorEventLoop
        asyncio.run(
            server.serve(),
            loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector()),
        )
    else:
        asyncio.run(server.serve())


if __name__ == "__main__":
    main()
