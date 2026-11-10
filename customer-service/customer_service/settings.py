from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

SERVICE_ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    """客服服务运行配置：环境变量优先，其次读取服务根目录下的 .env。"""

    database_url: str = Field(
        "sqlite+aiosqlite:///./data/customer_service.db",
        description="数据库连接串。标准部署为 postgresql+psycopg://…，本地零依赖模式为 SQLite",
    )
    redis_url: str = Field("", description="Redis 地址；留空则使用进程内事件总线")

    jwt_secret: str = Field("ecommerce-secret", description="JWT 签名密钥，三个后端必须一致")
    jwt_algorithm: str = Field("HS256", description="JWT 签名算法")
    internal_service_token: str = Field("customer-internal-token", description="调用 AI Service 的内部令牌")

    api_host: str = Field("127.0.0.1", description="监听地址")
    api_port: int = Field(8000, description="监听端口")
    cors_origins: list[str] = Field(
        ["http://localhost:5173", "http://127.0.0.1:5173", "http://localhost:5174", "http://127.0.0.1:5174"],
        description="允许跨域访问的前端地址",
    )

    conversation_idle_timeout_minutes: int = Field(30, description="AI 模式下会话空闲多久后关闭")
    message_merge_delay_ms: int = Field(1000, description="合并连续提问的等待窗口")
    message_merge_max_wait_ms: int = Field(4000, description="连续提问最多累计等待多久")
    max_message_chars: int = Field(2000, description="单条用户消息的最大长度")
    history_message_limit: int = Field(30, description="交给 AI 的历史消息条数上限")

    ai_service_url: str = Field("http://127.0.0.1:8002", description="AI Service 地址")
    ai_timeout_seconds: float = Field(75.0, description="请求 AI Service 的超时时间")
    ai_worker_poll_interval_ms: int = Field(150, description="AI Worker 领取轮次的轮询间隔")
    ai_worker_concurrency: int = Field(4, description="同时处理的 AI 轮次数量")
    ai_worker_max_attempts: int = Field(2, description="单个轮次调用 AI Service 的最大次数")
    ai_worker_retry_delay_seconds: float = Field(2.0, description="调用失败后的重试间隔")

    realtime_worker_poll_interval_ms: int = Field(200, description="实时事件发布 Worker 的轮询间隔")

    model_config = SettingsConfigDict(
        env_file=SERVICE_ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
