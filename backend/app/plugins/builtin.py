"""内置插件实现（阶段2三个首批扩展点，对应设计文档 4.2 组合语义）。

每个插件实现统一接口：apply(ctx, config) -> dict，ctx 携带会话数据。
阶段2插件与宿主同进程运行（trusted），WASM 沙箱属后续阶段。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PluginContext:
    """插件执行上下文：一次 Run 内共享。"""

    session_key: str  # memory 隔离键（阶段2=agent_id:run_id 前缀级）
    user_input: str = ""
    agent_id: str = ""
    run_id: str = ""
    retrieved_chunks: list[dict] = field(default_factory=list)  # RAG 命中
    memory: list[str] = field(default_factory=list)  # 记忆条目
    intent: dict = field(default_factory=dict)  # 意图识别结果
    skill_instructions: list[str] = field(default_factory=list)


# ── MemoryProvider（单槽位）──
class MemoryPlugin:
    key = "memory_simple"
    extension_point = "MemoryProvider"
    slot_semantics = "single"
    config_schema = {"max_items": {"type": "int", "default": 5}}

    def apply(self, ctx: PluginContext, config: dict) -> dict:
        max_items = int(config.get("max_items", 5))
        # 阶段2：会话级记忆——把用户输入与 RAG 命中标题作为短期记忆
        ctx.memory = [f"用户输入: {ctx.user_input[:100]}"]
        ctx.memory += [f"知识片段: {c['text'][:80]}" for c in ctx.retrieved_chunks[:max_items]]
        return {"memory_items": len(ctx.memory)}


# ── ChunkingStrategy（单槽位）──
class ChunkingPlugin:
    key = "chunking_fixed"
    extension_point = "ChunkingStrategy"
    slot_semantics = "single"
    config_schema = {"chunk_size": {"type": "int", "default": 400}, "overlap": {"type": "int", "default": 50}}

    def apply(self, ctx: PluginContext, config: dict) -> dict:
        chunk_size = int(config.get("chunk_size", 400))
        overlap = int(config.get("overlap", 50))
        # 供 rag_service 调用：把文本切成固定窗口片段
        return {"chunk_size": chunk_size, "overlap": overlap}

    @staticmethod
    def chunk(text: str, config: dict) -> list[str]:
        chunk_size = int(config.get("chunk_size", 400))
        overlap = int(config.get("overlap", 50))
        if len(text) <= chunk_size:
            return [text] if text else []
        step = max(chunk_size - overlap, 1)
        return [text[i : i + chunk_size] for i in range(0, len(text), step)]


# ── IntentRecognizer（单槽位）──
class IntentPlugin:
    key = "intent_keyword"
    extension_point = "IntentRecognizer"
    slot_semantics = "single"
    config_schema = {"rules": {"type": "object", "default": {}}}

    def apply(self, ctx: PluginContext, config: dict) -> dict:
        rules = config.get("rules") or {
            "search_knowledge": ["查", "找", "检索", "搜索", "什么是", "为什么", "怎么"],
            "chat": [],
        }
        text = ctx.user_input
        matched, confidence = "chat", 0.3
        for intent, keywords in rules.items():
            hits = sum(1 for kw in keywords if kw in text)
            if hits > 0:
                matched, confidence = intent, min(0.5 + 0.2 * hits, 0.95)
                break
        ctx.intent = {"intent": matched, "confidence": confidence}
        return ctx.intent


# ── ContextCompressor（有序管道，可多个）──
class ContextCompressorPlugin:
    key = "compress_truncate"
    extension_point = "ContextCompressor"
    slot_semantics = "pipeline"
    config_schema = {"max_chars": {"type": "int", "default": 2000}}

    def apply(self, ctx: PluginContext, config: dict) -> dict:
        max_chars = int(config.get("max_chars", 2000))
        # 压缩对象：RAG 命中片段（阶段2最长的上下文来源）
        compressed = []
        for chunk in ctx.retrieved_chunks:
            text = chunk["text"]
            if len(text) > max_chars:
                text = text[:max_chars] + "…"
            compressed.append({**chunk, "text": text})
        before = sum(len(c["text"]) for c in ctx.retrieved_chunks)
        ctx.retrieved_chunks = compressed
        after = sum(len(c["text"]) for c in compressed)
        return {"before_chars": before, "after_chars": after, "ratio": round(after / before, 2) if before else 1.0}


# ── WorkflowNode（图引用，声明式 DSL）──
class WorkflowRunner:
    """声明式工作流：{"steps": [{"plugin": "intent_keyword"}, {"step": "rag"}, {"step": "llm"}]}

    阶段2语义：顺序执行声明的步骤，跳过未声明项；含 "llm" 步骤时才生成回复。
    任意代码工作流不开放（设计文档 4.2）。
    """

    @staticmethod
    def validate(workflow: dict) -> list[str]:
        """返回错误列表（组装校验矩阵用）。"""
        errors: list[str] = []
        steps = workflow.get("steps") if isinstance(workflow, dict) else None
        if workflow and not steps:
            errors.append("workflow.steps 缺失")
            return errors
        allowed = {"intent", "rag", "memory", "compress", "llm"}
        for s in steps or []:
            step = s if isinstance(s, str) else (s.get("step") if isinstance(s, dict) else None)
            if step not in allowed:
                errors.append(f"未知工作流步骤 '{step}'，允许: {sorted(allowed)}")
        return errors


PLUGIN_REGISTRY: dict[str, object] = {
    p.key: p for p in (MemoryPlugin(), ChunkingPlugin(), IntentPlugin(), ContextCompressorPlugin())
}
