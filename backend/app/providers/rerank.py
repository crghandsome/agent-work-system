"""Rerank Provider。

- hashing：零依赖占位（按 embedding 相似度本身排序）
- tei / openai：TEI /rerank 或 OpenAI 兼容 /rerank 端点（接本地 bge-reranker）
"""
from __future__ import annotations

from abc import ABC, abstractmethod

import httpx

from ..config import get_settings


class RerankProvider(ABC):
    @abstractmethod
    def rerank(self, query: str, documents: list[str]) -> list[tuple[int, float]]:
        """返回 [(原文档下标, 相关性分数)，按分数降序]。"""


class HashingRerank(RerankProvider):
    """占位实现：保持原顺序（上游已按向量相似度排序）。"""

    def rerank(self, query: str, documents: list[str]) -> list[tuple[int, float]]:
        return [(i, 1.0 / (i + 1)) for i in range(len(documents))]


class HttpRerank(RerankProvider):
    def __init__(self, base_url: str, model: str, mode: str):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.mode = mode

    def rerank(self, query: str, documents: list[str]) -> list[tuple[int, float]]:
        with httpx.Client(timeout=30) as client:
            if self.mode == "tei":
                resp = client.post(f"{self.base_url}/rerank", json={"query": query, "texts": documents})
                results = resp.json()
                return [(int(r["index"]), float(r["score"])) for r in results]
            # openai 兼容
            resp = client.post(
                f"{self.base_url}/rerank",
                json={"model": self.model, "query": query, "documents": documents},
            )
            resp.raise_for_status()
            results = resp.json().get("results", [])
            return [(int(r["index"]), float(r["relevance_score"])) for r in results]


def get_rerank_provider() -> RerankProvider:
    s = get_settings()
    if s.rerank_provider == "hashing":
        return HashingRerank()
    if s.rerank_provider in ("tei", "openai"):
        if not s.rerank_api_base:
            raise RuntimeError("RERANK_API_BASE 未配置")
        return HttpRerank(s.rerank_api_base, s.rerank_model, "tei" if s.rerank_provider == "tei" else "openai")
    raise RuntimeError(f"未知 RERANK_PROVIDER: {s.rerank_provider}")
