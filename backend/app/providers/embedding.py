"""Embedding Provider。

- hashing：零依赖确定性哈希向量（开发/联调用，语义弱但流程全通）
- tei / openai：HuggingFace Text Embeddings Inference 或任意 OpenAI 兼容 /embeddings 端点
  （接本地 bge-m3 等，只需在 .env 配 EMBEDDING_API_BASE/EMBEDDING_MODEL）
"""
from __future__ import annotations

import hashlib
import math
from abc import ABC, abstractmethod

import httpx

from ..config import get_settings


class EmbeddingProvider(ABC):
    dim: int

    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]: ...


class HashingEmbedding(EmbeddingProvider):
    """词级哈希 + 频率加权，确定性、无网络依赖。"""

    def __init__(self, dim: int = 256):
        self.dim = dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        results = []
        for text in texts:
            vec = [0.0] * self.dim
            tokens = _tokenize(text)
            for tok in tokens:
                h = int(hashlib.md5(tok.encode("utf-8")).hexdigest(), 16)
                idx = h % self.dim
                sign = 1.0 if (h >> 128) % 2 == 0 else -1.0
                vec[idx] += sign
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            results.append([v / norm for v in vec])
        return results


def _tokenize(text: str) -> list[str]:
    # 简易中英分词：连续拉丁字母为一个词，单个 CJK 字符为一个词
    tokens: list[str] = []
    buf = ""
    for ch in text.lower():
        if ch.isascii() and ch.isalnum():
            buf += ch
        else:
            if buf:
                tokens.append(buf)
                buf = ""
            if "\u4e00" <= ch <= "\u9fff":
                tokens.append(ch)
    if buf:
        tokens.append(buf)
    return tokens


class HttpEmbedding(EmbeddingProvider):
    """OpenAI 兼容 /embeddings 或 TEI /embed。"""

    def __init__(self, base_url: str, model: str, dim: int, mode: str = "openai"):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.dim = dim
        self.mode = mode

    def embed(self, texts: list[str]) -> list[list[float]]:
        with httpx.Client(timeout=30) as client:
            if self.mode == "tei":
                resp = client.post(f"{self.base_url}/embed", json={"inputs": texts})
            else:
                resp = client.post(
                    f"{self.base_url}/embeddings",
                    json={"model": self.model, "input": texts},
                )
            resp.raise_for_status()
            data = resp.json()
            vectors = data["data"] if self.mode == "openai" else data
            return [list(map(float, v)) for v in vectors]


def get_embedding_provider() -> EmbeddingProvider:
    s = get_settings()
    if s.embedding_provider == "hashing":
        return HashingEmbedding(dim=s.embedding_dim)
    if s.embedding_provider in ("tei", "openai"):
        if not s.embedding_api_base:
            raise RuntimeError("EMBEDDING_API_BASE 未配置")
        mode = "tei" if s.embedding_provider == "tei" else "openai"
        return HttpEmbedding(s.embedding_api_base, s.embedding_model, s.embedding_dim, mode)
    raise RuntimeError(f"未知 EMBEDDING_PROVIDER: {s.embedding_provider}")
