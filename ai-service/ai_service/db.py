"""数据库引擎与会话工厂：同一套代码支持 PostgreSQL（标准部署）与 SQLite（本地零依赖）。"""

import json
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import JSON, DateTime, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.types import TypeDecorator

from ai_service.settings import SERVICE_ROOT, get_settings

JSONType = JSON().with_variant(JSONB(), "postgresql")


class UTCDateTime(TypeDecorator):
    """统一以 UTC 存取时间；SQLite 不保存时区，读出时补回 UTC。"""

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
        db_path = Path(url.database).expanduser()
        if not db_path.is_absolute():
            db_path = SERVICE_ROOT / db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        url = url.set(database=db_path.as_posix())

    sqlite_engine = create_async_engine(url, connect_args={"timeout": 15}, json_serializer=_dumps)

    @event.listens_for(sqlite_engine.sync_engine, "connect")
    def _configure_sqlite(dbapi_connection, _):
        dbapi_connection.isolation_level = None
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=15000")
        cursor.close()

    @event.listens_for(sqlite_engine.sync_engine, "begin")
    def _begin_immediate(connection):
        # 工具调用审计与运行记录会并发写库：事务开始即拿写锁，排队而不是报 database is locked
        connection.exec_driver_sql("BEGIN IMMEDIATE")

    return sqlite_engine


engine: AsyncEngine = create_engine_from_url(get_settings().ai_database_url)
session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)


async def get_session() -> AsyncGenerator[AsyncSession]:
    async with session_factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
