from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from ecommerce_service.common.config import SERVICE_ROOT, get_settings


class Base(DeclarativeBase):
    pass


def _create_engine():
    """按连接串创建引擎：PostgreSQL 为标准部署方式，SQLite 用于本地零依赖运行。"""
    database_url = get_settings().ecom_database_url
    url = make_url(database_url)
    if url.get_backend_name() != "sqlite":
        return create_engine(database_url, pool_pre_ping=True)

    if url.database and url.database != ":memory:":
        # 相对路径统一以服务根目录为基准，避免受启动目录影响
        db_path = Path(url.database).expanduser()
        if not db_path.is_absolute():
            db_path = SERVICE_ROOT / db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        url = url.set(database=db_path.as_posix())
    sqlite_engine = create_engine(
        url,
        # FastAPI 的同步接口运行在线程池中，同一个连接可能被不同线程使用
        connect_args={"check_same_thread": False, "timeout": 15},
    )

    @event.listens_for(sqlite_engine, "connect")
    def _configure_sqlite(dbapi_connection, _):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=15000")
        cursor.close()

    return sqlite_engine


engine = _create_engine()
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
