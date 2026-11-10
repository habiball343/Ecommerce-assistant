from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

SERVICE_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    # 标准部署使用 PostgreSQL；也支持 sqlite:///./data/ecommerce.db 用于本地零依赖运行
    ecom_database_url: str = (
        "postgresql+psycopg://customer_service:customer_service@127.0.0.1:5432/ecommerce"
    )
    jwt_secret: str = "replace-this-in-production"
    jwt_algorithm: str = "HS256"
    api_host: str = "127.0.0.1"
    api_port: int = 8001
    cors_origins: list[str] = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:5174",
        "http://127.0.0.1:5174",
    ]

    model_config = SettingsConfigDict(
        env_file=SERVICE_ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
