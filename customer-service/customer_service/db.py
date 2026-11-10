"""数据库引擎与会话工厂。

同一套 ORM 代码同时支持两种存储：
- PostgreSQL（postgresql+psycopg://…）：标准部署；
- SQLite（sqlite+aiosqlite:///…）：本地零依赖运行。
"""

import json
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import JSON, DateTime, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.types import TypeDecorator

from customer_service.settings import SERVICE_ROOT, get_settings

# PostgreSQL 使用 JSONB，其余数据库退回通用 JSON
JSONType = JSON().with_variant(JSONB(), "postgresql")


class UTCDateTime(TypeDecorator):
    """统一以 UTC 存取时间。SQLite 不保存时区，读出时补回 UTC，保证时间可以直接比较。"""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


def _dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def create_engine_from_url(database_url: str) -> AsyncEngine:
    url = make_url(database_url)
    if url.get_backend_name() != "sqlite":
        return create_async_engine(url, pool_pre_ping=True, json_serializer=_dumps)

    if url.database and url.database != ":memory:":
        # 相对路径以服务根目录为基准，避免受启动目录影响
        db_path = Path(url.database).expanduser()
        if not db_path.is_absolute():
            db_path = SERVICE_ROOT / db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        url = url.set(database=db_path.as_posix())

    sqlite_engine = create_async_engine(url, connect_args={"timeout": 15}, json_serializer=_dumps)

    @event.listens_for(sqlite_engine.sync_engine, "connect")
    def _configure_sqlite(dbapi_connection, _):
        # 由下面的 begin 事件显式开启事务
        dbapi_connection.isolation_level = None
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=15000")
        cursor.close()

    @event.listens_for(sqlite_engine.sync_engine, "begin")
    def _begin_immediate(connection):
        # API 请求与后台 Worker 会并发写库：事务一开始就拿写锁，
        # 排队等待而不是在“先读后写”时报 database is locked。
        connection.exec_driver_sql("BEGIN IMMEDIATE")

    return sqlite_engine


engine: AsyncEngine = create_engine_from_url(get_settings().database_url)
session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)


async def get_session() -> AsyncGenerator[AsyncSession]:
    """FastAPI 依赖：每个请求一个会话，出错时回滚。"""
    async with session_factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
