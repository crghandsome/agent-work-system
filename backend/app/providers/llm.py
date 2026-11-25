"""LLM Provider：OpenAI 兼容 Chat Completions（平台网关当前形态）；未配密钥时回退 mock。

思考链处理：模型输出中的 <think>…</think>（或 reasoning_content 字段）从正文剥离，
以 usage["reasoning_content"] 单独返回，由上层决定展示方式（前端默认收起）。
"""
from __future__ import annotations

import json
import re
import time
from abc import ABC, abstractmethod

import httpx

from ..config import get_settings

RETRYABLE_STATUS = {408, 429, 500}  # 503/502/504 属服务不可用，快速失败由网关层熔断
RETRY_TIMES = 2
RETRY_BACKOFF_SECONDS = 1.0
THINK_RE = re.compile(r"<think>(.*?)</think>", re.S)


def split_reasoning(text: str) -> tuple[str, str]:
    """剥离 <think>…</think> 与未闭合的 <think> 前缀，返回 (正文, 思考链)。"""
    reasoning = ""
    m = THINK_RE.search(text)
    if m:
        reasoning = m.group(1).strip()
        text = THINK_RE.sub("", text).strip()
    elif text.lstrip().startswith("<think>"):
        # 未闭合：整段视作思考链（无正文）
        reasoning = text.lstrip()[len("<think>"):].strip()
        text = ""
    return text, reasoning


def split_stream_chunk(text: str, state: dict) -> tuple[str, str]:
    """流式增量分类器：把增量文本按状态分为 (思考增量, 正文增量)。

    state 跨 chunk 维护：{"in_think": bool}。处理跨 chunk 撕裂的标签。
    """
    think, body = "", ""
    buf = state.get("carry", "")
    stream = buf + text
    state["carry"] = ""
    while stream:
        if state["in_think"]:
            end = stream.find("</think>")
            if end >= 0:
                think += stream[:end]
                stream = stream[end + len("</think>"):]
                state["in_think"] = False
            else:
                # 可能是撕裂的闭合标签，留 8 字符
                keep = 8 if len(stream) >= 8 else len(stream)
                think += stream[:-keep] if keep < len(stream) else ""
                state["carry"] = stream[-keep:] if keep < len(stream) else stream
                stream = ""
        else:
            start = stream.find("<think>")
            if start >= 0:
                body += stream[:start]
                stream = stream[start + len("<think>"):]
                state["in_think"] = True
            else:
                keep = 7 if len(stream) >= 7 else len(stream)
                body += stream[:-keep] if keep < len(stream) else ""
                state["carry"] = stream[-keep:] if keep < len(stream) else stream
                stream = ""
    return think, body


def stream_llm(base_url: str, token: str, model: str, system: str, messages: list[dict]):
    """流式调用 OpenAI 兼容 /chat/completions（SSE）。

    产出 (kind, text)：kind ∈ {reasoning, content, usage, done}。
    思考链优先阶段：<think> 阶段产出 reasoning；正文阶段产出 content。
    """
    headers = {"Authorization": f"Bearer {token}", "content-type": "application/json"}
    payload = {
        "model": model, "max_tokens": 2048, "stream": True, "stream_options": {"include_usage": True},
        "messages": ([{"role": "system", "content": system}] if system else []) + messages,
    }
    state = {"in_think": False, "carry": ""}
    with httpx.Client(timeout=180) as client:
        with client.stream("POST", f"{base_url.rstrip('/')}/chat/completions", headers=headers, json=payload) as resp:
            resp.raise_for_status()
            usage = {}
            for line in resp.iter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    obj = json.loads(data)
                except json.JSONDecodeError:
                    continue
                choice = (obj.get("choices") or [{}])[0]
                delta = choice.get("delta", {}) or {}
                rc = delta.get("reasoning_content") or ""
                ct = delta.get("content") or ""
                if rc:
                    yield "reasoning", rc
                if ct:
                    t, b = split_stream_chunk(ct, state)
                    if t:
                        yield "reasoning", t
                    if b:
                        yield "content", b
                if obj.get("usage"):
                    usage = obj["usage"]
            # 收尾：残留 carry 按当前状态归类
            tail = state.get("carry", "")
            if tail:
                yield "reasoning" if state["in_think"] else "content", tail
            yield "usage", json.dumps(usage, ensure_ascii=False)


class GatewayUnavailableError(RuntimeError):
    """网关级不可用（503/502/504 等），上层应熔断而非逐模型重试。"""


class LLMProvider(ABC):
    @abstractmethod
    def complete(self, system: str, messages: list[dict]) -> tuple[str, dict]:
        """返回 (回复文本, token_usage)。messages: [{"role": "user"|"assistant", "content": str}]"""


class OpenAICompatLLM(LLMProvider):
    """OpenAI 兼容 /v1/chat/completions。base_url 形如 https://host/v1。"""

    def __init__(self, base_url: str, token: str, model: str):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.model = model

    def complete(self, system: str, messages: list[dict]) -> tuple[str, dict]:
        headers = {"Authorization": f"Bearer {self.token}", "content-type": "application/json"}
        payload = {
            "model": self.model,
            "max_tokens": 2048,
            "messages": ([{"role": "system", "content": system}] if system else []) + messages,
        }
        with httpx.Client(timeout=120) as client:
            last_exc: Exception | None = None
            data = None
            for attempt in range(RETRY_TIMES):
                try:
                    resp = client.post(f"{self.base_url}/chat/completions", headers=headers, json=payload)
                    if resp.status_code in RETRYABLE_STATUS:
                        last_exc = httpx.HTTPStatusError(
                            f"gateway {resp.status_code}", request=resp.request, response=resp
                        )
                        time.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
                        continue
                    if resp.status_code in (502, 503, 504):
                        raise GatewayUnavailableError(f"网关不可用: HTTP {resp.status_code}")
                    resp.raise_for_status()
                    data = resp.json()
                    break
                except (httpx.ConnectError, httpx.ReadTimeout) as exc:
                    last_exc = exc
                    time.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
            else:
                raise RuntimeError(f"LLM 网关重试 {RETRY_TIMES} 次后仍失败: {last_exc}")
        text = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
        usage = data.get("usage", {}) or {}
        # 思考链剥离：标签形态或独立 reasoning_content 字段
        text, reasoning = split_reasoning(text or "")
        field_reasoning = (data.get("choices") or [{}])[0].get("message", {}).get("reasoning_content", "")
        if field_reasoning and not reasoning:
            reasoning = field_reasoning
        usage_out = {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        }
        if reasoning:
            usage_out["reasoning_content"] = reasoning
        return text, usage_out


class MockLLM(LLMProvider):
    """未配密钥时的回退：证明闭环流程可用。"""

    def complete(self, system: str, messages: list[dict]) -> tuple[str, dict]:
        last_user = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
        reply = (
            "[mock-llm] 已按系统指令处理输入。\n"
            f"系统指令长度: {len(system)} 字符\n"
            f"用户输入: {last_user[:200]}"
        )
        return reply, {"input_tokens": len(system) // 4, "output_tokens": len(reply) // 4}


def get_llm_provider(model: str = "") -> LLMProvider:
    s = get_settings()
    if s.anthropic_auth_token and s.anthropic_base_url:
        return OpenAICompatLLM(s.anthropic_base_url, s.anthropic_auth_token, model or s.anthropic_model)
    return MockLLM()
