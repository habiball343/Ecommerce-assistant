from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

SERVICE_ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    """智能服务运行配置：环境变量优先，其次读取服务根目录下的 .env。"""

    ai_database_url: str = Field(
        "sqlite+aiosqlite:///./data/ai_service.db",
        description="数据库连接串。标准部署为 postgresql+psycopg://…，本地零依赖模式为 SQLite",
    )
    api_host: str = Field("127.0.0.1", description="监听地址")
    api_port: int = Field(8002, description="监听端口")
    cors_origins: list[str] = Field(
        ["http://localhost:5173", "http://127.0.0.1:5173", "http://localhost:5174", "http://127.0.0.1:5174"],
        description="允许跨域访问的前端地址（管理端会直接读取运行记录与知识库）",
    )

    internal_service_token: str = Field("customer-internal-token", description="客服服务调用内部接口的令牌")
    jwt_secret: str = Field("ecommerce-secret", description="JWT 签名密钥，三个后端必须一致")
    jwt_algorithm: str = Field("HS256", description="JWT 签名算法")

    ecommerce_base_url: str = Field("http://127.0.0.1:8001/api/v1", description="电商服务接口地址")
    ecommerce_timeout_seconds: float = Field(8.0, description="查询电商服务的超时时间")

    # ---- 回答引擎
    agent_engine: Literal["auto", "llm", "rules"] = Field(
        "auto",
        description="auto：配置了大模型就用 LangChain Agent，否则用内置规则引擎；llm / rules：强制指定",
    )
    llm_provider: str = Field("deepseek", description="deepseek / qwen / ollama / openai（任意 OpenAI 兼容接口）")
    llm_model: str = Field("deepseek-flash", description="模型名称")
    llm_api_key: str = Field("", description="模型 API Key；留空表示不接入大模型")
    llm_base_url: str = Field("https://api.deepseek.com", description="模型接口地址")
    llm_enable_thinking: bool = Field(False, description="是否开启深度思考（客服场景默认关闭以降低时延）")
    llm_temperature: float = Field(0.2, description="采样温度")
    llm_timeout_seconds: float = Field(30.0, description="单次模型调用的超时时间")
    agent_max_attempts: int = Field(2, description="回复未通过校验时，最多让模型重答几次")
    agent_timeout_seconds: float = Field(55.0, description="一次 Agent 运行的总时限，超时后降级到规则引擎")
    prompt_version: str = Field("xiaoer-support-v1", description="提示词版本号，记录在运行审计中")

    history_message_limit: int = Field(30, description="注入上下文的历史消息条数上限")
    history_character_budget: int = Field(12000, description="注入上下文的历史消息字符预算")

    # ---- 知识库
    knowledge_chunk_size: int = Field(420, description="知识切片的目标长度（字符）")
    knowledge_top_k: int = Field(4, description="每次检索返回的切片数量")
    knowledge_max_upload_mb: int = Field(10, description="上传文档的大小上限")
    embedding_base_url: str = Field("", description="可选：OpenAI 兼容的向量接口地址；留空则只用关键词检索")
    embedding_api_key: str = Field("", description="可选：向量接口的 API Key")
    embedding_model: str = Field("", description="可选：向量模型名称")

    model_config = SettingsConfigDict(
        env_file=SERVICE_ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    @property
    def llm_configured(self) -> bool:
        """是否具备调用大模型的条件。Ollama 在本机运行，不需要 API Key。"""
        return bool(self.llm_api_key.strip()) or self.llm_provider.lower() == "ollama"

    @property
    def use_llm(self) -> bool:
        if self.agent_engine == "rules":
            return False
        return self.llm_configured

    @property
    def embedding_configured(self) -> bool:
        return bool(self.embedding_base_url.strip() and self.embedding_model.strip())


@lru_cache
def get_settings() -> Settings:
    return Settings()
