"""模型网关（设计文档 7）：多模型目录 + fallback 链 + 预算硬限。

平台级模型目录在配置中定义；Agent 的 model 字段可指向目录中的模型 id，
网关按 fallback 链依次尝试，全部失败才抛错（由 Runtime 降级 mock）。
"""
from __future__ import annotations

from ..config import get_settings
from .audit_service import record_usage

# 平台模型目录：id → 形态（网关为 OpenAI 兼容；fallback 链按序尝试，全部失败才降级 mock）
MODEL_CATALOG: dict[str, str] = {
    "MiniMax-M3.1-Flash": "openai",
    "MiniMax-M3": "openai",
    "MiniMax-M2.7": "openai",
    "MiniMax-M2.5": "openai",
}


class ModelGateway:
    """按 fallback 链路由 LLM 请求；token 计量到 llm 主体。"""

    def __init__(self, db=None):
        self.db = db

    def complete(self, system: str, messages: list[dict], *, model: str = "",
                 budget_tokens: int | None = None) -> tuple[str, dict, str]:
        """返回 (text, usage, used_model)。"""
        from ..providers.llm import GatewayUnavailableError, OpenAICompatLLM

        s = get_settings()
        requested = model or s.anthropic_model
        # 请求模型不在目录（或已失效）时，目录中其余模型构成 fallback 链
        chain = [requested] + [m for m in MODEL_CATALOG if m != requested]
        last_err: Exception | None = None
        for mid in chain:
            if not s.anthropic_auth_token or not s.anthropic_base_url:
                break  # 未配网关 → mock
            try:
                llm = OpenAICompatLLM(s.anthropic_base_url, s.anthropic_auth_token, mid)
                text, usage = llm.complete(system, messages)
                if budget_tokens and (usage.get("output_tokens", 0) > budget_tokens):
                    raise RuntimeError(f"输出 {usage.get('output_tokens')} 超预算 {budget_tokens}")
                if self.db is not None:
                    record_usage(self.db, "llm", "llm_token", input_tokens=usage.get("input_tokens", 0),
                                 output_tokens=usage.get("output_tokens", 0))
                return text, usage, mid
            except GatewayUnavailableError as exc:
                # 服务级不可用：同一网关的其他模型也不可用，立即熔断
                last_err = exc
                break
            except Exception as exc:  # noqa: BLE001 — 单模型失效可继续下一个
                last_err = exc
                continue
        # 网关不可用：不再返回 mock 伪回答（避免 [mock-llm] 文本泄漏给用户）
        return "模型服务当前不可用（网关返回错误或限流），请稍后重试。", {}, \
            f"unavailable(all_failed: {last_err})" if last_err else "unavailable(no-gateway)"
