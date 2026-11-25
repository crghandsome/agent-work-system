"""配置驱动插件执行器：让无代码的用户安装插件也能在 Runtime 中生效。

对齐 DSH 思想：插件可以只是配置（如 MCP bundle），其能力由 manifest 的 config 声明。
本执行器支持以下 behavior：
- intent_keywords: 关键词意图识别（输出 intent/confidence）
- memory_echo: 把用户输入作为记忆条目
- compress_truncate: 截断检索片段（配置 max_chars）
- inject_system: 向系统指令注入一段文字（配置 prompt）
- passthrough: 仅记录执行轨迹（默认）

未知行为按 passthrough 处理，并在轨迹中标注，不影响运行。
"""
from __future__ import annotations

from ..models import Plugin


def execute(plugin: Plugin, ctx, config: dict | None = None) -> dict:
    """执行一个数据库注册的插件（含用户安装的无代码插件）。"""
    cfg = {**(plugin.config or {}), **(config or {})}
    behavior = cfg.get("behavior", "passthrough")

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

    return {"behavior": behavior, "note": "无代码插件按透传执行"}


def resolve(db, key: str) -> Plugin | None:
    return db.query(Plugin).filter(Plugin.key == key, Plugin.enabled.is_(True)).first()
