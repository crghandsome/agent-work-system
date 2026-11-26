"""内置插件实现（真实可用的生命周期中间件）。

四个核心扩展点插件：
1. MemoryPlugin (`memory_simple`, MemoryProvider)
   - 真正跨轮持久化记忆（落盘 <ARTIFACTS_ROOT>/memory/<scope>.json）
   - before_llm：按查询相关度 + 时间新鲜度召回历史记忆注入 ctx.memory
   - after_llm：从本轮对话自动提取关键事实与对话摘要持久化保存
2. IntentPlugin (`intent_keyword`, IntentRecognizer)
   - 识别 chitchat / search_knowledge / task_execute / summarize 及自定义规则
   - 真正驱动运行时行为：闲聊跳过无效 RAG、检索意图扩大召回 top_k、注入策略指引
3. ContextCompressorPlugin (`compress_truncate`, ContextCompressor)
   - 真正按总预算与查询相关度压缩：去重 + 按查询关键词优先提取高相关句子 + 句末边界截断
4. ChunkingPlugin (`chunking_fixed`, ChunkingStrategy)
   - 按插件配置 (chunk_size, overlap) 驱动 RAG 默认分块与长文本切片
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class PluginContext:
    """插件执行上下文：一次 Run 内共享，贯穿前置与后置钩子。"""

    session_key: str  # 记忆隔离键（如 agent:<name> 或 <agent_name>:<session>）
    user_input: str = ""
    agent_id: str = ""
    run_id: str = ""
    retrieved_chunks: list[dict] = field(default_factory=list)  # RAG 命中
    memory: list[str] = field(default_factory=list)  # 召回的记忆条目
    intent: dict = field(default_factory=dict)  # 意图识别结果
    skill_instructions: list[str] = field(default_factory=list)
    injected_system: str = ""  # 插件注入的策略/系统指令
    # 意图插件对后续流程的真实控制信号
    skip_rag: bool = False
    rag_top_k: int | None = None
    compressed: bool = False  # 是否已经过上下文压缩插件处理


def _extract_terms(text: str) -> set[str]:
    """轻量中英文词项提取（英文单词 + 中文 2-gram），用于记忆召回与句子相关性打分。"""
    s = (text or "").lower()
    en = set(re.findall(r"[a-z0-9_]{2,}", s))
    zh = {s[i:i + 2] for i in range(len(s) - 1)
          if "\u4e00" <= s[i] <= "\u9fff" and "\u4e00" <= s[i + 1] <= "\u9fff"}
    return en | zh


# ── 持久化记忆存储（线程安全 JSON，落盘 <ARTIFACTS_ROOT>/memory/）──

_MEM_LOCK = threading.RLock()


def _memory_file(scope: str) -> Path:
    root = Path(os.environ.get("AWS_ARTIFACTS_DIR", "/srv/data/artifacts")) / "memory"
    root.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^a-zA-Z0-9_-]", "_", scope or "default")[:80]
    return root / f"{safe}.json"


def _load_memories(scope: str) -> list[dict]:
    p = _memory_file(scope)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_memories(scope: str, items: list[dict]) -> None:
    p = _memory_file(scope)
    p.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


# ── 1. MemoryProvider（真正跨轮持久化记忆）──

class MemoryPlugin:
    key = "memory_simple"
    extension_point = "MemoryProvider"
    slot_semantics = "single"
    config_schema = {
        "max_items": {"type": "int", "default": 5},
        "max_stored": {"type": "int", "default": 50},
    }

    _FACT_PATTERNS = (
        "记住", "请记住", "我是", "我叫", "我的名字", "我的代号", "偏好", "喜欢",
        "以后", "默认", "不要", "称呼我", "叫我", "习惯", "要求", "背景",
    )

    def _scope(self, ctx: PluginContext) -> str:
        # 按 Agent 名称（session_key 前缀）持久化记忆，实现跨轮与跨会话回忆
        base = (ctx.session_key or "").split(":")[0].strip()
        return base or ctx.agent_id or "default"

    def apply(self, ctx: PluginContext, config: dict) -> dict:
        max_items = max(1, int(config.get("max_items", 5)))
        scope = self._scope(ctx)
        with _MEM_LOCK:
            stored = _load_memories(scope)

        if not stored:
            ctx.memory = []
            return {"recalled": 0, "total_stored": 0, "scope": scope}

        q_terms = _extract_terms(ctx.user_input)
        scored: list[tuple[float, dict]] = []
        total = len(stored)
        for idx, item in enumerate(stored):
            text = item.get("text", "")
            recency = (idx + 1) / max(total, 1)  # 越新分越高 (0~1]
            overlap = len(q_terms & _extract_terms(text)) if q_terms else 0
            kind_bonus = 1.5 if item.get("kind") == "fact" else 0.0
            score = overlap * 2.0 + kind_bonus + recency
            scored.append((score, item))

        scored.sort(key=lambda x: x[0], reverse=True)
        picked = [it["text"] for _, it in scored[:max_items] if it.get("text")]
        ctx.memory = picked
        return {
            "recalled": len(picked),
            "total_stored": total,
            "scope": scope,
            "preview": [t[:60] for t in picked[:3]],
        }

    def after_run(self, ctx: PluginContext, answer: str, config: dict) -> dict:
        """对话完成后提取并持久化本轮记忆（事实优先 + 对话摘要）。"""
        max_stored = max(5, int(config.get("max_stored", 50)))
        scope = self._scope(ctx)
        user_text = (ctx.user_input or "").strip()
        ans_text = (answer or "").strip()
        if not user_text:
            return {"saved": 0}

        new_entries: list[dict] = []
        now = int(time.time())
        # 1) 关键事实/偏好记忆
        if any(pat in user_text for pat in self._FACT_PATTERNS):
            new_entries.append({
                "kind": "fact",
                "text": f"[用户事实/偏好] {user_text[:200]}",
                "ts": now,
            })
        # 2) 本轮问答摘要（过滤过短寒暄）
        if len(user_text) >= 6 and ans_text:
            new_entries.append({
                "kind": "turn",
                "text": f"[历史对话] 问：{user_text[:100]} → 答：{ans_text[:140]}",
                "ts": now,
            })

        if not new_entries:
            return {"saved": 0, "scope": scope}

        with _MEM_LOCK:
            stored = _load_memories(scope)
            existing_texts = {it.get("text") for it in stored}
            added = 0
            for entry in new_entries:
                if entry["text"] not in existing_texts:
                    stored.append(entry)
                    existing_texts.add(entry["text"])
                    added += 1
            if len(stored) > max_stored:
                # 优先保留 fact 类记忆，淘汰最老的普通对话摘要
                facts = [x for x in stored if x.get("kind") == "fact"]
                turns = [x for x in stored if x.get("kind") != "fact"]
                keep_turns = max(0, max_stored - len(facts))
                stored = (facts[-max_stored:] + turns[-keep_turns:]) if keep_turns else facts[-max_stored:]
                stored.sort(key=lambda x: x.get("ts", 0))
            _save_memories(scope, stored)
        return {"saved": added, "total_stored": len(stored), "scope": scope}


# ── 2. ChunkingStrategy（真实驱动 RAG 分块与长文本切片）──

class ChunkingPlugin:
    key = "chunking_fixed"
    extension_point = "ChunkingStrategy"
    slot_semantics = "single"
    config_schema = {
        "chunk_size": {"type": "int", "default": 400},
        "overlap": {"type": "int", "default": 50},
    }

    def apply(self, ctx: PluginContext, config: dict) -> dict:
        chunk_size = max(100, int(config.get("chunk_size", 400)))
        overlap = max(0, min(int(config.get("overlap", 50)), chunk_size // 2))
        return {"chunk_size": chunk_size, "overlap": overlap}

    @staticmethod
    def chunk(text: str, config: dict) -> list[str]:
        chunk_size = max(100, int(config.get("chunk_size", 400)))
        overlap = max(0, min(int(config.get("overlap", 50)), chunk_size // 2))
        if not text:
            return []
        if len(text) <= chunk_size:
            return [text]
        step = max(chunk_size - overlap, 1)
        out: list[str] = []
        i = 0
        n = len(text)
        while i < n:
            end = min(i + chunk_size, n)
            # 尽量在句末/换行处切分，避免截断句子
            if end < n:
                window = text[i + chunk_size // 2:end]
                cut = max(window.rfind("\n"), window.rfind("。"), window.rfind("."), window.rfind("；"))
                if cut > 0:
                    end = i + chunk_size // 2 + cut + 1
            seg = text[i:end].strip()
            if seg:
                out.append(seg)
            if end >= n:
                break
            i = max(end - overlap, i + 1)
        return out


# ── 3. IntentRecognizer（真实路由与策略引导）──

class IntentPlugin:
    key = "intent_keyword"
    extension_point = "IntentRecognizer"
    slot_semantics = "single"
    config_schema = {
        "rules": {"type": "object", "default": {}},
        "skip_rag_on_chitchat": {"type": "bool", "default": True},
    }

    DEFAULT_RULES = {
        "chitchat": ["你好", "您好", "嗨", "hello", "hi", "谢谢", "感谢", "再见", "拜拜", "早上好", "晚上好"],
        "search_knowledge": ["查", "找", "检索", "搜索", "文档", "制度", "政策", "规定", "合同", "条款", "什么是", "为什么", "怎么"],
        "summarize": ["总结", "汇总", "归纳", "提炼", "摘要", "概览", "要点"],
        "task_execute": ["生成", "制作", "导出", "运行", "执行", "计算", "分析", "写一份", "报表", "日报", "周报", "月报"],
    }

    INTENT_GUIDANCE = {
        "chitchat": "当前为日常寒暄/闲聊意图，请自然简洁友好地回应即可，无需强行引用知识库。",
        "search_knowledge": "当前为知识检索问答意图，请优先依据知识库检索到的真实片段作答，并注明信息来源。",
        "summarize": "当前为总结提炼意图，请按条理清晰、分点归纳的方式输出核心要点。",
        "task_execute": "当前为任务执行意图，请结合已装配的 Skill 与工具按步骤完成交付。",
    }

    def apply(self, ctx: PluginContext, config: dict) -> dict:
        custom_rules = config.get("rules") if isinstance(config.get("rules"), dict) else {}
        rules = {**self.DEFAULT_RULES, **custom_rules}
        skip_rag_on_chitchat = bool(config.get("skip_rag_on_chitchat", True))

        text = (ctx.user_input or "").strip()
        text_lower = text.lower()
        best_intent, best_hits, best_matched = "general", 0, []

        for intent, keywords in rules.items():
            if not isinstance(keywords, list):
                continue
            matched = [kw for kw in keywords if kw and kw.lower() in text_lower]
            # 闲聊只在短句（<=16字）且未命中其他业务意图时优先
            if intent == "chitchat" and len(text) > 16:
                continue
            if len(matched) > best_hits:
                best_intent, best_hits, best_matched = intent, len(matched), matched

        confidence = min(0.55 + 0.15 * best_hits, 0.95) if best_hits > 0 else 0.35
        ctx.intent = {
            "intent": best_intent,
            "confidence": round(confidence, 2),
            "matched": best_matched[:5],
        }

        # 真正驱动后续生命周期行为
        action_taken = "default"
        if best_intent == "chitchat" and skip_rag_on_chitchat and confidence >= 0.65:
            ctx.skip_rag = True
            action_taken = "skip_rag"
        elif best_intent == "search_knowledge":
            ctx.rag_top_k = max(ctx.rag_top_k or 5, 8)
            action_taken = "boost_rag_top_k=8"

        guidance = self.INTENT_GUIDANCE.get(best_intent, "")
        if guidance:
            ctx.injected_system = (ctx.injected_system + "\n" + guidance).strip()

        return {**ctx.intent, "action": action_taken}


# ── 4. ContextCompressor（相关性句级抽取 + 总预算压缩）──

class ContextCompressorPlugin:
    key = "compress_truncate"
    extension_point = "ContextCompressor"
    slot_semantics = "pipeline"
    config_schema = {
        "max_chars": {"type": "int", "default": 600},
        "max_total_chars": {"type": "int", "default": 1800},
    }

    @staticmethod
    def _compress_text(text: str, query_terms: set[str], limit: int) -> str:
        if len(text) <= limit:
            return text
        # 按中英文句末拆句，优先保留与用户提问最相关的句子并维持原文顺序
        sents = [s.strip() for s in re.split(r"(?<=[。！？.!?\n])", text) if s.strip()]
        if len(sents) <= 1:
            return text[:limit].rstrip() + "…"

        scored = []
        for idx, s in enumerate(sents):
            hit = len(query_terms & _extract_terms(s)) if query_terms else 0
            # 首句通常含主题，给少量加权
            bonus = 0.5 if idx == 0 else 0.0
            scored.append((hit + bonus, idx, s))

        # 按相关度从高到低挑选直到塞满预算，再按原句顺序拼回
        scored.sort(key=lambda x: (-x[0], x[1]))
        chosen: list[tuple[int, str]] = []
        used = 0
        for _, idx, s in scored:
            if used + len(s) <= limit or not chosen:
                chosen.append((idx, s[:limit] if not chosen and len(s) > limit else s))
                used += len(chosen[-1][1])
            if used >= limit:
                break
        chosen.sort(key=lambda x: x[0])
        res = "".join(s for _, s in chosen)
        return res if len(res) >= len(text) else (res.rstrip("…") + "…")

    def apply(self, ctx: PluginContext, config: dict) -> dict:
        max_chars = max(120, int(config.get("max_chars", 600)))
        max_total = max(max_chars, int(config.get("max_total_chars", 1800)))
        chunks = ctx.retrieved_chunks or []
        before = sum(len(c.get("text", "")) for c in chunks)
        if not chunks:
            ctx.compressed = True
            return {"before_chars": 0, "after_chars": 0, "saved_pct": 0.0, "chunks_kept": 0}

        q_terms = _extract_terms(ctx.user_input)
        seen_sigs: set[str] = set()
        compressed: list[dict] = []
        total_used = 0

        for chunk in chunks:
            raw = (chunk.get("text") or "").strip()
            if not raw:
                continue
            sig = re.sub(r"\s+", "", raw[:80]).lower()
            if sig in seen_sigs:
                continue
            seen_sigs.add(sig)

            remaining = max_total - total_used
            if remaining < 80:
                break
            limit = min(max_chars, remaining)
            new_text = self._compress_text(raw, q_terms, limit)
            compressed.append({**chunk, "text": new_text})
            total_used += len(new_text)

        ctx.retrieved_chunks = compressed
        ctx.compressed = True
        after = sum(len(c.get("text", "")) for c in compressed)
        saved_pct = round((1 - after / before) * 100, 1) if before > 0 else 0.0
        return {
            "before_chars": before,
            "after_chars": after,
            "saved_pct": max(0.0, saved_pct),
            "chunks_kept": len(compressed),
            "chunks_dropped": len(chunks) - len(compressed),
        }


# ── WorkflowNode（声明式工作流校验）──

class WorkflowRunner:
    """声明式工作流：{"steps": [{"plugin": "intent_keyword"}, {"step": "rag"}, {"step": "llm"}]}"""

    @staticmethod
    def validate(workflow: dict) -> list[str]:
        errors: list[str] = []
        steps = workflow.get("steps") if isinstance(workflow, dict) else None
        if steps is None:
            return errors
        if not isinstance(steps, list):
            return ["workflow.steps 必须是列表"]
        for i, step in enumerate(steps):
            if not isinstance(step, dict):
                errors.append(f"workflow.steps[{i}] 必须是对象")
            elif not (step.get("plugin") or step.get("step")):
                errors.append(f"workflow.steps[{i}] 缺少 plugin 或 step 字段")
        return errors


PLUGIN_REGISTRY = {
    "memory_simple": MemoryPlugin(),
    "intent_keyword": IntentPlugin(),
    "compress_truncate": ContextCompressorPlugin(),
}
