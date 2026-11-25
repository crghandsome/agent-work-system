"""平台配置：全部经环境变量注入（12-Factor），密钥不入代码。"""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AWS_", env_file=".env", extra="ignore")

    # MySQL
    mysql_host: str = "localhost"
    mysql_port: int = 23307
    mysql_db: str = "agent_work_system"
    mysql_user: str = "aws_app"
    mysql_password: str = "aws_app_7k4m8x"

    # Qdrant
    qdrant_host: str = "localhost"
    qdrant_port: int = 26334
    qdrant_collection: str = "aws_chunks"

    # LLM（Anthropic 兼容 API）
    anthropic_base_url: str = ""
    anthropic_auth_token: str = ""
    anthropic_model: str = "claude-sonnet-4-5"

    # 向量 / 重排 Provider
    embedding_provider: str = "hashing"  # hashing | tei | openai
    embedding_dim: int = 256
    embedding_api_base: str = ""
    embedding_model: str = ""
    rerank_provider: str = "hashing"  # hashing | tei | openai
    rerank_api_base: str = ""
    rerank_model: str = ""

    # A2A 对外暴露：外部系统访问本平台 A2A 端点时使用的基础 URL
    # 例：https://agent.mycompany.com 或 http://1.2.3.4:8900（不要用 localhost）
    a2a_public_base_url: str = ""

    # Runtime
    run_max_steps: int = 8
    rag_top_k: int = 5
    rag_score_threshold: float = 0.5   # 相关性阈值（低于此分不召回）
    rag_hybrid_k: int = 20             # 每路召回候选数（向量/BM25 各取前 N 再 RRF 融合）
    rag_rrf_k: int = 60                # RRF 平滑常数

    @property
    def mysql_url(self) -> str:
        return (
            f"mysql+pymysql://{self.mysql_user}:{self.mysql_password}"
            f"@{self.mysql_host}:{self.mysql_port}/{self.mysql_db}?charset=utf8mb4"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
