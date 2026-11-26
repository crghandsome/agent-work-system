"""配置驱动插件执行器：支持四阶段生命周期钩子与 http_webhook 外部微服务调用。

四阶段标准钩子（hooks）：
- before_llm : 进入 LLM 前（意图识别、记忆召回、上下文压缩、指令注入、前置拦截）
- before_tool: 工具调用前（权限校验、敏感工具拦截、参数改写）
- after_tool : 工具调用后（结果脱敏、内容过滤、审计）
- after_llm  : LLM 生成最终答案后（合规审核、敏感词脱敏、长期记忆沉淀）

支持的 behavior：
- intent_keywords  : 关键词意图识别（before_llm）
- memory_echo      : 把用户输入作为记忆条目（before_llm）
- compress_truncate: 截断检索片段（before_llm）
- inject_system    : 向系统指令注入文字（before_llm）
- content_filter   : 关键词拦截或正则脱敏（支持全 4 个钩子）
- http_webhook     : HTTP POST 调用外部微服务，接收上下文改写或拦截指令（支持全 4 个钩子）
- passthrough      : 仅记录执行轨迹（默认）
"""
from __future__ import annotations

import json
import re
from urllib.request import Request, urlopen

from ..models import Plugin

VALID_HOOKS = ("before_llm", "before_tool", "after_tool", "after_llm")


def configured_hooks(cfg: dict) -> list[str]:
    """返回插件声明监听的生命周期钩子列表；未声明时默认仅在 before_llm 触发。"""
    raw = cfg.get("hooks")
    if isinstance(raw, list) and raw:
        valid = [h for h in raw if h in VALID_HOOKS]
        if valid:
            return valid
    return ["before_llm"]


def _post_json(url: str, payload: dict, headers: dict | None = None, timeout: float = 5.0) -> dict:
    """发送 HTTP POST JSON 请求（优先 httpx，降级 urllib），严格限制 scheme 为 http/https。"""
    if not isinstance(url, str) or not (url.startswith("http://") or url.startswith("https://")):
        raise ValueError("http_webhook 仅允许 http:// 或 https:// 地址")
    safe_timeout = min(max(float(timeout or 5.0), 0.5), 30.0)
    req_headers = {"Content-Type": "application/json", **(headers or {})}
    try:
        import httpx

        with httpx.Client(timeout=safe_timeout, trust_env=False) as client:
            resp = client.post(url, json=payload, headers=req_headers)
            resp.raise_for_status()
            data = resp.json()
        return data if isinstance(data, dict) else {"raw": data}
    except ImportError:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = Request(url, data=body, headers=req_headers, method="POST")
        with urlopen(req, timeout=safe_timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace") or "{}")
            return data if isinstance(data, dict) else {"raw": data}


def _apply_hook_mutations(ctx, hook: str, data: dict, hook_data: dict | None) -> dict:
    """将插件返回的改写/拦截结果应用到 ctx 与 hook_data，返回规范化结果字典。"""
    out: dict = {}
    if not isinstance(data, dict):
        return out

    # 1. 上下文改写（任意阶段均允许补充）
    if isinstance(data.get("injected_system"), str) and data["injected_system"].strip():
        ctx.injected_system = (getattr(ctx, "injected_system", "") + "\n" + data["injected_system"].strip()).strip()
        out["injected_system"] = True
    if isinstance(data.get("memory_append"), list):
        ctx.memory = list(getattr(ctx, "memory", []) or []) + [str(x) for x in data["memory_append"] if x]
        out["memory_appended"] = len(data["memory_append"])
    if isinstance(data.get("skip_rag"), bool):
        ctx.skip_rag = data["skip_rag"]
        out["skip_rag"] = data["skip_rag"]
    if isinstance(data.get("rag_top_k"), int) and data["rag_top_k"] > 0:
        ctx.rag_top_k = data["rag_top_k"]
        out["rag_top_k"] = data["rag_top_k"]

    # 2. 拦截控制
    if data.get("block"):
        out["block"] = True
        out["block_message"] = str(data.get("block_message") or "请求已被安全插件拦截")

    # 3. 工具参数 / 工具结果 / 最终答案改写
    if hook == "before_tool" and isinstance(data.get("args"), dict):
        out["args"] = data["args"]
    if hook == "after_tool" and isinstance(data.get("observation"), str):
        out["observation"] = data["observation"]
    if hook == "after_llm" and isinstance(data.get("answer"), str):
        out["answer"] = data["answer"]

    if "note" in data:
        out["note"] = str(data["note"])
    return out


def _exec_content_filter(ctx, cfg: dict, hook: str, hook_data: dict | None) -> dict:
    """内置配置型审核/脱敏行为：支持 block_keywords 拦截与 redact_patterns 正则脱敏。"""
    hd = hook_data or {}
    block_kws = [str(k) for k in (cfg.get("block_keywords") or []) if k]
    redact_rules = cfg.get("redact_patterns") or []  # [{"pattern": "...", "replace": "***"}]
    block_msg = str(cfg.get("block_message") or "内容触发合规策略，已拦截")

    if hook == "before_llm":
        target = ctx.user_input or ""
        for kw in block_kws:
            if kw in target:
                return {"block": True, "block_message": block_msg, "matched": kw}
        return {"checked": True, "hook": hook}

    if hook == "before_tool":
        tool_name = str(hd.get("tool") or "")
        blocked_tools = [str(t) for t in (cfg.get("block_tools") or []) if t]
        if tool_name in blocked_tools:
            return {"block": True, "block_message": block_msg, "blocked_tool": tool_name}
        args_str = json.dumps(hd.get("args") or {}, ensure_ascii=False)
        for kw in block_kws:
            if kw in args_str:
                return {"block": True, "block_message": block_msg, "matched": kw}
        return {"checked": True, "tool": tool_name}

    if hook in ("after_tool", "after_llm"):
        field = "observation" if hook == "after_tool" else "answer"
        text = str(hd.get(field) or "")
        for kw in block_kws:
            if kw in text:
                return {"block": True, "block_message": block_msg, "matched": kw}
        redacted = text
        hits = 0
        for rule in redact_rules:
            if not isinstance(rule, dict) or not rule.get("pattern"):
                continue
            pat = str(rule["pattern"])
            rep = str(rule.get("replace", "***"))
            new_text, cnt = re.subn(pat, rep, redacted)
            if cnt:
                redacted = new_text
                hits += cnt
        if hits:
            return {field: redacted, "redacted_count": hits}
        return {"checked": True, "redacted_count": 0}

    return {"checked": True}


def execute(plugin: Plugin, ctx, config: dict | None = None, *,
            hook: str = "before_llm", hook_data: dict | None = None) -> dict:
    """执行一个数据库注册的插件（支持四阶段钩子与 http_webhook）。"""
    cfg = {**(plugin.config or {}), **(config or {})}
    if hook not in configured_hooks(cfg):
        return {"skipped": True, "hook": hook}

    behavior = cfg.get("behavior", "passthrough")

    if behavior == "http_webhook":
        url = str(cfg.get("url") or "").strip()
        if not url:
            return {"error": "http_webhook 缺少 url 配置"}
        hd = hook_data or {}
        payload = {
            "hook": hook,
            "plugin_key": plugin.key,
            "agent_id": getattr(ctx, "agent_id", ""),
            "session_key": getattr(ctx, "session_key", ""),
            "user_input": getattr(ctx, "user_input", ""),
            "intent": getattr(ctx, "intent", {}),
            "memory": getattr(ctx, "memory", []),
            "retrieved_chunks_count": len(getattr(ctx, "retrieved_chunks", []) or []),
            **{k: v for k, v in hd.items() if k in ("tool", "args", "observation", "answer")},
        }
        headers = cfg.get("headers") if isinstance(cfg.get("headers"), dict) else {}
        timeout = float(cfg.get("timeout", 5.0))
        fail_open = bool(cfg.get("fail_open", True))
        try:
            resp_data = _post_json(url, payload, headers=headers, timeout=timeout)
            mut = _apply_hook_mutations(ctx, hook, resp_data, hd)
            return {"behavior": "http_webhook", "hook": hook, "ok": True, **mut}
        except Exception as exc:
            if not fail_open:
                return {
                    "behavior": "http_webhook", "hook": hook, "ok": False,
                    "block": True, "block_message": f"外部校验服务不可用（{exc}），已按安全策略拦截",
                }
            return {"behavior": "http_webhook", "hook": hook, "ok": False, "error": str(exc)}

    if behavior == "content_filter":
        return {"behavior": "content_filter", **_exec_content_filter(ctx, cfg, hook, hook_data)}

    if behavior == "intent_keywords":
        rules = cfg.get("rules") or {"general": ["请", "帮", "如何", "什么"]}
        text = ctx.user_input or ""
        intent, confidence = "chat", 0.3
        for name, kws in rules.items():
            hits = sum(1 for kw in kws if kw in text)
            if hits:
                intent, confidence = name, min(0.5 + 0.2 * hits, 0.95)
                break
        ctx.intent = {"intent": intent, "confidence": confidence}
        return ctx.intent

    if behavior == "memory_echo":
        ctx.memory = [f"用户输入: {(ctx.user_input or '')[:100]}"]
        return {"memory_items": len(ctx.memory)}

    if behavior == "compress_truncate":
        max_chars = int(cfg.get("max_chars", 2000))
        before = sum(len(c.get("text", "")) for c in ctx.retrieved_chunks)
        ctx.retrieved_chunks = [
            {**c, "text": c.get("text", "")[:max_chars]} for c in ctx.retrieved_chunks
        ]
        after = sum(len(c.get("text", "")) for c in ctx.retrieved_chunks)
        return {"before_chars": before, "after_chars": after}

    if behavior == "inject_system":
        prompt = cfg.get("prompt", "")
        if prompt:
            ctx.injected_system = (getattr(ctx, "injected_system", "") + "\n" + prompt).strip()
        return {"injected": bool(prompt)}

    return {"behavior": behavior, "hook": hook, "note": "无代码插件按透传执行"}


def resolve(db, key: str) -> Plugin | None:
    return db.query(Plugin).filter(Plugin.key == key, Plugin.enabled.is_(True)).first()
