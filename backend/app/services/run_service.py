"""Agent Runtime（扩展点自动挂载版）。

核心：装了什么插件就自动获得什么能力，不依赖 workflow.steps。
插件按 extension_point 自动映射到生命周期阶段：
  IntentRecognizer   → 意图识别（LLM 前）
  ChunkingStrategy   → RAG 入库分块策略（本 Agent 运行时不影响，注册供 RAG 使用）
  ContextCompressor  → 压缩检索片段（RAG 后）
  MemoryProvider     → 会话记忆（LLM 前）
  WorkflowNode       → 流程节点（LLM 前执行，可注入上下文）
  (内置 RAG / LLM 为固定阶段)

workflow.steps 仅作为高级手动编排：存在时按它规定顺序执行，未配置时走自动计划。
"""
from __future__ import annotations

import time

from sqlalchemy.orm import Session

from ..models import Agent, AgentRun, Skill, new_id
from ..plugins.builtin import PLUGIN_REGISTRY, PluginContext, WorkflowRunner
from . import rag_service, plugin_runner
from .audit_service import record_usage
from .model_gateway import ModelGateway


def _apply_plugin(db, key: str, ctx) -> tuple[dict, bool]:
    """执行插件：内置走 PLUGIN_REGISTRY，用户安装走配置驱动执行器。"""
    if key in PLUGIN_REGISTRY:
        return PLUGIN_REGISTRY[key].apply(ctx, {}), True
    p = plugin_runner.resolve(db, key)
    if p:
        return plugin_runner.execute(p, ctx, {}), False
    return {"error": f"插件 {key} 不可用"}, False


# 扩展点 → 自动执行的生命周期阶段（顺序即执行顺序）
EP_STAGE = {
    "IntentRecognizer": "intent",
    "ContextCompressor": "compress",
    "MemoryProvider": "memory",
    "WorkflowNode": "workflow_node",
}
AUTO_STAGES = ["intent", "rag", "compress", "memory", "workflow_node", "llm"]


def _effective_ep(db, key: str) -> str:
    """取插件扩展点：内置查注册表，用户插件查 DB。"""
    p = PLUGIN_REGISTRY.get(key)
    if p:
        return getattr(p, "extension_point", "")
    row = plugin_runner.resolve(db, key)
    return row.extension_point if row else ""


def build_execution_plan(db: Session, agent: Agent) -> list[dict]:
    """按扩展点自动生成执行计划；workflow.steps 仅用于高级手动排序。

    返回 [{"stage": str, "key": str|None, "ep": str}, ...]
    """
    plugins = []
    for slot, key in (agent.plugin_keys or {}).items():
        ep = _effective_ep(db, key)
        if ep in EP_STAGE:
            plugins.append({"stage": EP_STAGE[ep], "key": key, "ep": ep, "slot": slot})
        # ChunkingStrategy 不影响本轮运行（供 RAG 入库使用），跳过

    # 有显式 workflow：只保留其中声明的阶段（仍按扩展点取插件，不按字符串 key 猜）
    declared = None
    if isinstance(agent.workflow, dict):
        declared = agent.workflow.get("steps") or None

    plan = []
    for stage in AUTO_STAGES:
        if stage in ("rag", "llm"):
            if declared is None or stage in declared:
                plan.append({"stage": stage, "key": None, "ep": stage})
            continue
        if declared is not None and stage not in declared:
            continue
        for p in plugins:
            if p["stage"] == stage:
                plan.append(p)
    return plan


def run_agent(db: Session, agent: Agent, input_text: str, use_rag: bool = True) -> AgentRun:
    run = AgentRun(id=new_id("run"), agent_id=agent.id, agent_version=agent.version, input_text=input_text)
    db.add(run)
    db.flush()

    # 独立智能体模式：所有 Agent 默认走 ReAct 工作循环。
    # 插件提供的基础能力（记忆/意图/检索/注入）在循环前置阶段执行，与自主工具调用叠加，不冲突。
    # 仅显式 _agent_mode=legacy 时保留旧编排驱动行为。
    _pc = agent.plugin_config or {}
    if _pc.get("_agent_mode") != "legacy":
        from . import agent_loop

        result = agent_loop.run_agent_loop(db, agent, input_text)
        run.output_text = result["answer"]
        run.status = result["status"]
        run.steps = result["steps"]
        run.token_usage = {}
        run.finished_at = run.created_at
        db.flush()
        return run

    steps_trace: list[dict] = []
    usage_total = {"input_tokens": 0, "output_tokens": 0}
    ctx = PluginContext(session_key=f"{agent.name}:{run.id}", user_input=input_text, agent_id=agent.id, run_id=run.id)

    try:
        wf_errors = WorkflowRunner.validate(agent.workflow or {})
        if wf_errors:
            raise ValueError("; ".join(wf_errors))

        plan = build_execution_plan(db, agent)
        for item in plan:
            stage, key = item["stage"], item.get("key")
            t0 = time.perf_counter()

            if stage == "intent" and key:
                result, builtin = _apply_plugin(db, key, ctx)
                steps_trace.append({"step": "intent", "plugin": key, "builtin": builtin,
                                    "result": result, "ms": _ms(t0)})

            elif stage == "rag":
                if use_rag or ctx.intent.get("intent") in ("search_knowledge",):
                    try:
                        result = rag_service.search(query=input_text)
                        ctx.retrieved_chunks = result["hits"]
                        steps_trace.append({
                            "step": "rag", "hits": len(result["hits"]), "reranked": result["reranked"],
                            "titles": [h["title"] for h in result["hits"]][:4], "ms": _ms(t0),
                        })
                    except Exception as exc:
                        steps_trace.append({"step": "rag", "error": str(exc), "ms": _ms(t0)})

            elif stage == "compress" and key:
                result, builtin = _apply_plugin(db, key, ctx)
                steps_trace.append({"step": "compress", "plugin": key, "builtin": builtin,
                                    "result": result, "ms": _ms(t0)})

            elif stage == "memory" and key:
                result, builtin = _apply_plugin(db, key, ctx)
                steps_trace.append({"step": "memory", "plugin": key, "builtin": builtin,
                                    "result": result, "ms": _ms(t0)})

            elif stage == "workflow_node" and key:
                result, builtin = _apply_plugin(db, key, ctx)
                steps_trace.append({"step": "plugin", "plugin": key, "slot": item.get("slot", ""),
                                    "builtin": builtin, "result": result, "ms": _ms(t0)})

            elif stage == "llm":
                system = _build_system(db, agent, ctx)
                gw = ModelGateway(db)
                reply, usage, used = gw.complete(
                    system, [{"role": "user", "content": input_text}],
                    model=agent.model, budget_tokens=agent.plugin_config.get("_budget_tokens"),
                )
                usage_total["input_tokens"] += usage.get("input_tokens", 0)
                usage_total["output_tokens"] += usage.get("output_tokens", 0)
                steps_trace.append({"step": "llm", "model": used, "ms": _ms(t0)})
                run.output_text = reply

        run.status = "succeeded"
    except Exception as exc:
        run.status = "failed"
        run.error = f"{type(exc).__name__}: {exc}"

    run.steps = steps_trace
    run.token_usage = usage_total
    if run.status != "running":
        run.finished_at = run.created_at
    record_usage(db, f"agent:{agent.name}@{agent.version}", "run", input_tokens=usage_total["input_tokens"],
                 output_tokens=usage_total["output_tokens"])
    db.flush()
    return run


def _build_system(db: Session, agent: Agent, ctx: PluginContext) -> str:
    from .artifact_store import load_skill_bundle

    parts = [agent.system_prompt] if agent.system_prompt else []
    injected = getattr(ctx, "injected_system", "")
    if injected:
        parts.append(injected)
    skill_instructions = []
    for key in agent.skill_keys:
        skill = db.query(Skill).filter(Skill.name == key, Skill.status == "published").first()
        if not skill:
            continue
        # 制品加载：哈希校验（审核内容=运行内容），失败降级到数据库文本缓存
        bundle = load_skill_bundle(skill.name, skill.artifact_sha256) if skill.artifact_sha256 else None
        if bundle:
            body = bundle["instructions"]
            refs = ""
            for rel, text in bundle["references"].items():
                refs += f"\n\n#### references/{rel}\n{text}"
            scripts_note = f"\n（包内脚本 {bundle['scripts']} 需沙箱执行，当前版本仅列出）" if bundle["scripts"] else ""
            skill_instructions.append(
                f"## Skill: {skill.name}\n{skill.description}\n\n{body}{scripts_note}{refs}"
            )
        else:
            skill_instructions.append(f"## Skill: {skill.name}\n{skill.description}\n\n{skill.instructions}")
    if skill_instructions:
        parts.append("### 已加载 Skill\n" + "\n\n".join(skill_instructions))
    if ctx.retrieved_chunks:
        rag_block = "\n---\n".join(f"[{h['title']}] {h['text']}" for h in ctx.retrieved_chunks)
        parts.append("### 知识库检索结果（仅供参考，引用需核实）\n" + rag_block)
    if ctx.memory:
        parts.append("### 会话记忆\n" + "\n".join(ctx.memory))
    return "\n\n".join(p for p in parts if p.strip()) or "你是一个有用的助手。"


def _ms(t0: float) -> int:
    return int((time.perf_counter() - t0) * 1000)
