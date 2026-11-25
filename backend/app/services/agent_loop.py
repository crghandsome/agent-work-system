"""Agent 工作循环（ReAct）：让 Agent 真正"干事"。

循环：
    用户目标 → [LLM 决策：思考 + 选工具/给最终答案]
             → 若调工具：执行工具 → 观察结果 → 回到 LLM 决策
             → 若给答案：结束

LLM 通过结构化 JSON 决策：
    {"thought": "...", "action": "knowledge_search", "action_args": {...}}
    或
    {"thought": "...", "action": "final", "answer": "..."}

带步数上限、失败降级、轨迹记录。
"""
from __future__ import annotations

import json

from sqlalchemy.orm import Session

from . import tool_registry
from .model_gateway import ModelGateway

LOOP_SYSTEM = """你是一个能自主使用工具的 Agent。你可以调用工具来完成用户目标。

可用工具：
{tools}

工作方式：
1. 先思考（thought）：分析用户目标，决定下一步。
2. 若需要工具，输出：
   {{"thought":"...", "action":"工具名", "action_args":{{...}}}}
3. 若已完成，输出最终答案：
   {{"thought":"...", "action":"final", "answer":"给用户的最终回答"}}

严格输出一个 JSON 对象，不要 markdown 代码块，不要其它文字。
用户目标：{goal}
"""


def _decide(db, system: str, messages: list[dict], model: str = "") -> dict | None:
    """让 LLM 输出一步决策 JSON。"""
    try:
        raw, _, _ = ModelGateway(db).complete(system, messages, model=model)
        if "[mock-llm]" in raw:
            return None
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end <= start:
            return None
        data = json.loads(raw[start:end + 1])
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _prepare_plugin_context(db, agent, goal: str) -> tuple[str, list[dict]]:
    """执行 Agent 配置的插件，产出注入 ReAct 的基础能力上下文。

    插件提供记忆/意图/压缩/inject_system 等基础能力，与 ReAct 自主调工具互补，不冲突。
    返回 (注入到 system 的上下文文本, 插件执行轨迹)。
    """
    from ..plugins.builtin import PluginContext, PLUGIN_REGISTRY
    from . import plugin_runner, rag_service

    ctx = PluginContext(session_key=f"{agent.name}:loop", user_input=goal,
                        agent_id=agent.id, run_id="")
    traces = []

    def apply(key: str, stage: str):
        if key in PLUGIN_REGISTRY:
            return PLUGIN_REGISTRY[key].apply(ctx, {}), True
        p = plugin_runner.resolve(db, key)
        if p:
            return plugin_runner.execute(p, ctx, {}), False
        return {"error": f"插件 {key} 不可用"}, False

    # 按扩展点自动执行（与 run_service 的 build_execution_plan 同语义）
    from .run_service import build_execution_plan

    for item in build_execution_plan(db, agent):
        stage, key = item["stage"], item.get("key")
        if stage == "rag" and not key:
            # RAG 检索阶段（无插件 key）：执行检索供 LLM 参考
            try:
                r = rag_service.search(query=goal)
                ctx.retrieved_chunks = r["hits"]
                traces.append({"stage": "rag", "plugin": "",
                               "result": {"hits": len(r["hits"]), "reranked": r["reranked"]}})
            except Exception as exc:
                traces.append({"stage": "rag", "plugin": "", "error": str(exc)})
            continue
        if not key:
            continue
        try:
            result, _ = apply(key, stage)
            traces.append({"stage": stage, "plugin": key, "result": result})
        except Exception as exc:
            traces.append({"stage": stage, "plugin": key, "error": str(exc)})

    # 组装注入文本：意图 + 记忆 + 检索（已有则注入）+ 手动注入的 system
    parts = []
    if getattr(ctx, "intent", None):
        parts.append(f"意图识别：{ctx.intent}")
    if getattr(ctx, "memory", None):
        parts.append("会话记忆：\n" + "\n".join(ctx.memory))
    if getattr(ctx, "retrieved_chunks", None):
        parts.append("知识库片段：\n" + "\n---\n".join(
            f"[{c.get('title','')}] {c.get('text','')[:400]}" for c in ctx.retrieved_chunks[:5]))
    injected = getattr(ctx, "injected_system", "")
    if injected:
        parts.append("插件注入指令：\n" + injected)
    return ("\n\n".join(parts) if parts else ""), traces


def run_agent_loop(
    db: Session, agent, goal: str, *, max_steps: int = 8,
) -> dict:
    """执行 ReAct 循环，返回 {answer, steps, status}。

    steps: [{step, thought, action, action_args, observation, ms}, ...]
    插件上下文作为前置基础能力注入，不削弱自主调工具能力。
    """
    tool_registry.init_builtin_tools()
    tools_desc = "\n".join(
        f"- {t['name']}: {t['description']} 参数: {json.dumps(t['parameters'], ensure_ascii=False)}"
        for t in tool_registry.list_tools()
    )
    system = LOOP_SYSTEM.replace("{tools}", tools_desc).replace("{goal}", goal)

    # 身份强化 + 用户系统指令
    identity = (
        f"你是「{agent.name}」。职责：{agent.description or '见系统指令'}。\n"
        "只处理职责范围内的事；范围外的请求礼貌说明并拒绝，不要退化成通用助手。"
    )
    if agent.system_prompt:
        identity += "\n\n" + agent.system_prompt
    # 插件基础能力上下文（记忆/意图/检索/注入）——与自主工具调用叠加，不冲突
    plugin_context, plugin_traces = _prepare_plugin_context(db, agent, goal)
    if plugin_context:
        identity += "\n\n### 插件提供的基础能力上下文（请据此回答）\n" + plugin_context
    full_system = identity + "\n\n---\n\n" + system

    # 每个插件作为独立的前置步骤（step=阶段名，兼容原轨迹格式；index=序号）
    steps = [{"step": t["stage"], "index": i, "thought": "", "action": t["stage"],
              "action_args": {}, "observation": str(t.get("result") or t.get("error") or "")[:500],
              "plugin": t.get("plugin", "")}
             for i, t in enumerate(plugin_traces, 1)]
    messages = [{"role": "user", "content": goal}]
    answer, status = "", "failed"

    for i in range(max_steps):
        step_no = len(plugin_traces) + i + 1
        decision = _decide(db, full_system, messages, model=agent.model or "")
        if not decision:
            # LLM 决策失败：降级用一次普通回答
            raw, _, _ = ModelGateway(db).complete(identity, messages, model=agent.model or "")
            answer = "" if "[mock-llm]" in raw else raw
            status = "succeeded" if answer else "failed"
            steps.append({"step": "fallback", "index": step_no, "thought": "",
                          "action": "fallback", "action_args": {},
                          "observation": "LLM未返回结构化决策，降级直答"})
            break

        thought = str(decision.get("thought", ""))
        action = str(decision.get("action", ""))
        step = {"step": action or "final", "index": step_no, "thought": thought,
                "action": action, "action_args": decision.get("action_args", {})}

        if action == "final" or not action:
            answer = str(decision.get("answer", ""))
            if not answer:
                # LLM 给了 decision 但没填 answer（网关异常时常见）：降级直答
                raw, _, _ = ModelGateway(db).complete(identity, messages, model=agent.model or "")
                answer = "" if "[mock-llm]" in raw else raw
                step["action"] = "fallback"
                step["observation"] = "LLM未给出答案，降级直答"
            status = "succeeded" if answer else "failed"
            steps.append(step)
            break

        args = decision.get("action_args") or {}
        observation = tool_registry.invoke(db, agent, action, args)
        step["observation"] = str(observation)[:2000]
        steps.append(step)
        # 把观察回灌给 LLM 继续决策
        messages.append({"role": "assistant", "content": json.dumps(decision, ensure_ascii=False)})
        messages.append({"role": "user", "content": f"工具「{action}」的结果：\n{observation}\n\n继续你的决策。"})

    if status == "failed" and not answer:
        answer = "我没有能在限定步数内完成任务，请补充更具体的要求。"
    return {"answer": answer, "steps": steps, "status": status}
