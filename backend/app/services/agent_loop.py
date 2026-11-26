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


def _load_skill_context(db, agent) -> str:
    """加载 Agent 装配的 Skill 指令（含 references）注入 ReAct 上下文。

    这是 Agent 的"作业指导书"——报告生成等 Skill 的详细步骤在这里，
    没有它 LLM 只能靠 description 猜，必然做不好。
    """
    from .artifact_store import load_skill_bundle
    from ..models import Skill

    parts = []
    for key in (agent.skill_keys or []):
        skill = db.query(Skill).filter(Skill.name == key, Skill.status == "published").first()
        if not skill:
            continue
        bundle = load_skill_bundle(skill.name, skill.artifact_sha256) if skill.artifact_sha256 else None
        if bundle and bundle.get("instructions"):
            body = bundle["instructions"]
            refs = ""
            for rel, text in bundle["references"].items():
                refs += f"\n\n#### references/{rel}\n{text}"
            scripts_note = f"\n（包内脚本 {bundle['scripts']} 可用 skill_script 工具执行）" if bundle["scripts"] else ""
            parts.append(f"## Skill: {skill.name}\n{skill.description}\n\n{body}{scripts_note}{refs}")
        else:
            # bundle 损坏/结构异常时降级用 DB 里的完整指令（真相源兜底）
            parts.append(f"## Skill: {skill.name}\n{skill.description}\n\n{skill.instructions}")
    return ("### 你装配的 Skill（必须严格按其中的步骤完成任务）\n" + "\n\n".join(parts)) if parts else ""


def _decide(db, system: str, messages: list[dict], model: str = "", emit=None) -> dict | None:
    """让 LLM 输出一步决策 JSON；同时把模型真实思考链（reasoning_content）流式输出。"""
    try:
        raw, usage, _ = ModelGateway(db).complete(system, messages, model=model)
        if "[mock-llm]" in raw:
            return None
        # 真实思考链：模型返回的 reasoning_content（MiniMax 等）
        real_reasoning = ""
        if isinstance(usage, dict):
            real_reasoning = (usage.get("reasoning_content") or "").strip()
        if real_reasoning and emit:
            emit({"type": "reasoning", "text": real_reasoning})
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end <= start:
            return None
        data = json.loads(raw[start:end + 1])
        if isinstance(data, dict):
            data["__reasoning__"] = real_reasoning
            return data
        return None
    except Exception:
        return None


def _prepare_plugin_context(db, agent, goal: str) -> tuple[str, list[dict], object]:
    """执行 Agent 配置的插件，产出注入 ReAct 的基础能力上下文。

    插件提供持久化记忆/意图路由/相关性压缩/inject_system 等真实能力，与 ReAct 自主调工具互补。
    返回 (注入到 system 的上下文文本, 插件执行轨迹, PluginContext)。
    """
    from ..plugins.builtin import PluginContext
    from . import rag_service
    from .run_service import _apply_plugin, build_execution_plan

    ctx = PluginContext(session_key=f"{agent.name}:loop", user_input=goal,
                        agent_id=agent.id, run_id="")
    traces = []

    for item in build_execution_plan(db, agent):
        stage, key = item["stage"], item.get("key")
        if stage == "rag" and not key:
            # 若意图插件判定闲聊且开启 skip_rag，真正跳过无效向量检索
            if getattr(ctx, "skip_rag", False):
                traces.append({"stage": "rag", "plugin": "",
                               "result": {"skipped": True, "reason": f"intent={ctx.intent.get('intent', '')}"}})
                continue
            try:
                search_kw = {"query": goal}
                if getattr(ctx, "rag_top_k", None):
                    search_kw["top_k"] = ctx.rag_top_k
                r = rag_service.search(**search_kw)
                ctx.retrieved_chunks = r["hits"]
                traces.append({"stage": "rag", "plugin": "",
                               "result": {"hits": len(r["hits"]), "reranked": r["reranked"],
                                          "top_k": search_kw.get("top_k", 5)}})
            except Exception as exc:
                traces.append({"stage": "rag", "plugin": "", "error": str(exc)})
            continue
        if not key:
            continue
        try:
            result, _ = _apply_plugin(db, key, ctx, agent=agent, slot=item.get("slot"), hook="before_llm")
            if not result.get("skipped"):
                traces.append({"stage": stage, "plugin": key, "slot": item.get("slot", ""), "result": result})
            if result.get("block"):
                ctx._blocked = True
                ctx._block_message = str(result.get("block_message") or "请求已被安全插件拦截")
                break
        except Exception as exc:
            traces.append({"stage": stage, "plugin": key, "slot": item.get("slot", ""), "error": str(exc)})

    # 组装注入文本：意图 + 持久化记忆 + 检索（若已压缩则完整保留压缩结果）+ 插件策略指引
    parts = []
    if getattr(ctx, "intent", None):
        parts.append(f"意图识别：{ctx.intent}")
    if getattr(ctx, "memory", None):
        parts.append("历史记忆（跨轮召回）：\n" + "\n".join(ctx.memory))
    if getattr(ctx, "retrieved_chunks", None):
        # 经过 compress_truncate 压缩后不再二次硬截断，未压缩时用 800 字兜底
        chunk_lim = 1200 if getattr(ctx, "compressed", False) else 800
        parts.append("知识库片段：\n" + "\n---\n".join(
            f"[{c.get('title','')}] {c.get('text','')[:chunk_lim]}" for c in ctx.retrieved_chunks[:8]))
    injected = getattr(ctx, "injected_system", "")
    if injected:
        parts.append("插件策略指引：\n" + injected)
    return ("\n\n".join(parts) if parts else ""), traces, ctx


def run_agent_loop(
    db: Session, agent, goal: str, *, max_steps: int = 8, on_event=None,
    history: list[dict] | None = None,
) -> dict:
    """执行 ReAct 循环，返回 {answer, steps, status}。

    steps: [{step, thought, action, action_args, observation, ms}, ...]
    插件上下文作为前置基础能力注入，不削弱自主调工具能力。
    on_event: 可选回调 on_event(dict)，实时产出 thought/tool/observation/content 事件，用于 SSE 流式展示。
    """

    def emit(ev: dict):
        if on_event:
            try:
                on_event(ev)
            except Exception:
                pass
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
    # 装配的 Skill 指令（作业指导书）——必须注入，否则 LLM 不知道该怎么做
    skill_context = _load_skill_context(db, agent)
    if skill_context:
        identity += "\n\n" + skill_context
    # 插件基础能力上下文（持久化记忆/意图路由/压缩/策略注入）——与自主工具调用叠加，不冲突
    plugin_context, plugin_traces, plugin_ctx = _prepare_plugin_context(db, agent, goal)
    if plugin_context:
        identity += "\n\n### 插件提供的基础能力上下文（请据此回答）\n" + plugin_context
    full_system = identity + "\n\n---\n\n" + system

    # 每个插件作为独立的前置步骤（step=阶段名，兼容原轨迹格式；index=序号）
    steps = [{"step": t["stage"], "index": i, "thought": "", "action": t["stage"],
              "action_args": {}, "observation": str(t.get("result") or t.get("error") or "")[:500],
              "plugin": t.get("plugin", "")}
             for i, t in enumerate(plugin_traces, 1)]
    # 若 before_llm 插件直接拦截请求，不调用 LLM，立刻返回安全提示
    if getattr(plugin_ctx, "_blocked", False):
        msg_block = getattr(plugin_ctx, "_block_message", "请求已被安全插件拦截")
        emit({"type": "content", "text": msg_block})
        return {"answer": msg_block, "steps": steps, "status": "succeeded", "files": []}
    # 多轮上下文：按配置保留历史（金鱼记忆的根因就是这里以前只发当前一句）
    from ..config import get_settings
    _limit = get_settings().llm_history_limit
    _hist = [m for m in (history or []) if m.get("role") in ("user", "assistant") and m.get("content")]
    if _limit > 0:
        _hist = _hist[-_limit:]
    messages = [*_hist, {"role": "user", "content": goal}]
    answer, status = "", "failed"
    delivered_files: list[dict] = []   # 本次运行交付的文件
    call_counts: dict[str, int] = {}   # 工具调用次数（检测循环）

    for i in range(max_steps):
        step_no = len(plugin_traces) + i + 1
        decision = _decide(db, full_system, messages, model=agent.model or "", emit=emit)
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
        # 实时输出：每一步的思考
        if thought:
            emit({"type": "thought", "index": step_no, "text": thought})

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
            if answer:
                emit({"type": "content", "text": answer})
            break

        args = decision.get("action_args") or {}
        from .run_service import run_lifecycle_hook
        # ── before_tool 钩子：工具权限拦截或参数改写 ──
        bt_state, bt_traces = run_lifecycle_hook(db, agent, plugin_ctx, "before_tool",
                                                 {"tool": action, "args": args})
        for tr in bt_traces:
            steps.append({"step": "before_tool", "index": len(steps) + 1, "thought": "",
                          "action": "before_tool", "action_args": {"tool": action},
                          "observation": str(tr.get("result") or tr.get("error") or "")[:500],
                          "plugin": tr.get("plugin", "")})
        args = bt_state.get("args", args)
        step["action_args"] = args
        # 实时输出：即将调用的工具
        emit({"type": "tool", "index": step_no, "action": action, "args": args})
        # 循环检测：同一工具调用超过 2 次，强制提示 LLM 换策略或收尾
        call_counts[action] = call_counts.get(action, 0) + 1
        if bt_state.get("block"):
            observation = f"[插件拦截] {bt_state.get('block_message') or '禁止调用该工具'}"
        else:
            observation = tool_registry.invoke(db, agent, action, args)
            # ── after_tool 钩子：工具结果脱敏/过滤/审计 ──
            at_state, at_traces = run_lifecycle_hook(db, agent, plugin_ctx, "after_tool",
                                                     {"tool": action, "args": args, "observation": str(observation)})
            for tr in at_traces:
                steps.append({"step": "after_tool", "index": len(steps) + 1, "thought": "",
                              "action": "after_tool", "action_args": {"tool": action},
                              "observation": str(tr.get("result") or tr.get("error") or "")[:500],
                              "plugin": tr.get("plugin", "")})
            if at_state.get("block"):
                observation = f"[插件拦截] {at_state.get('block_message') or '工具结果已被拦截'}"
            elif isinstance(at_state.get("observation"), str):
                observation = at_state["observation"]
        step["observation"] = str(observation)[:2000]
        steps.append(step)
        # 实时输出：工具执行结果
        emit({"type": "observation", "index": step_no, "action": action,
              "text": str(observation)[:1500]})
        # 交付文件：收集到结果（供 A2A/委托链透传）+ 实时事件
        for line in str(observation).splitlines():
            if line.startswith("[交付文件]"):
                parts = [p.strip() for p in line[len("[交付文件]"):].split("|")]
                if len(parts) >= 2:
                    f = {"name": parts[0], "url": parts[1],
                         "size": int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0}
                    if f not in delivered_files:
                        delivered_files.append(f)
                    emit({"type": "file", **f})
        # 把观察回灌给 LLM 继续决策
        messages.append({"role": "assistant", "content": json.dumps(decision, ensure_ascii=False)})
        hint = ""
        if call_counts[action] >= 2:
            hint = (f"\n\n⚠️ 工具「{action}」已连续调用 {call_counts[action]} 次。"
                    "不要再用相同参数重复调用：要么换不同工具/参数，要么基于已有信息直接给出 final 答案。")
        messages.append({"role": "user", "content":
                         f"工具「{action}」的结果：\n{observation}\n\n继续你的决策。{hint}"})

    def _clean(a: str) -> str:
        """降级直答若又吐出 ReAct JSON，提取其中的 answer/thought 或重写，绝不把 JSON 原文给用户。"""
        a = (a or "").strip()
        if not (a.startswith("{") and a.endswith("}")):
            return a
        try:
            d = json.loads(a)
            if isinstance(d, dict):
                return str(d.get("answer") or d.get("thought") or "").strip()
        except Exception:
            pass
        return ""

    if status == "failed" or not answer or (answer.startswith("{") and answer.endswith("}")):
        # 步数耗尽/答案仍是决策 JSON：用已收集的工具结果做一次干净总结
        observations = [s.get("observation", "") for s in steps if s.get("observation")]
        if observations:
            summary = "\n---\n".join(observations[-5:])
            file_note = ""
            if delivered_files:
                file_note = "\n已生成文件：" + "、".join(f["name"] for f in delivered_files) + "（见下方下载卡片）"
            try:
                raw, _, _ = ModelGateway(db).complete(
                    identity,
                    [{"role": "user", "content":
                      f"用户目标：{goal}\n\n已执行步骤获取的信息：\n{summary}{file_note}\n\n"
                      "请直接用自然语言向用户总结结果（不要输出任何JSON或代码块）；"
                      "若信息不足，如实说明缺什么。"}],
                    model=agent.model or "",
                )
                answer = _clean("" if "[mock-llm]" in raw else raw)
                status = "succeeded" if answer else status
            except Exception:
                pass
        if not answer:
            answer = _clean(answer) or "任务未在限定步数内完成，且网关未返回可用结果。请稍后重试或简化需求。"
            status = "succeeded" if answer and "网关未返回" not in answer else "failed"
    else:
        answer = _clean(answer) or answer
    # ── after_llm 钩子：输出合规审核/脱敏 + 长期记忆持久化 ──
    from .run_service import run_lifecycle_hook
    al_state, al_traces = run_lifecycle_hook(db, agent, plugin_ctx, "after_llm", {"answer": answer})
    if al_state.get("block"):
        answer = str(al_state.get("block_message") or "回答触发合规策略，已拦截")
    elif isinstance(al_state.get("answer"), str):
        answer = al_state["answer"]
    for ah in al_traces:
        steps.append({"step": ah["stage"], "index": len(steps) + 1, "thought": "",
                      "action": ah["stage"], "action_args": {},
                      "observation": str(ah.get("result") or ah.get("error") or "")[:300],
                      "plugin": ah.get("plugin", "")})
    return {"answer": answer, "steps": steps, "status": status, "files": delivered_files}
