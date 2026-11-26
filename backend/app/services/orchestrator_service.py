"""主 Agent 编排服务（对话式多 Agent 协作）。

流程：用户消息 → 主 Agent 意图判断（路由决策）→ 命中专业 Agent 则经 A2A 内部任务分发
→ 收集子 Agent 结果 → 主 Agent 综合处理 → 回复用户（带委托轨迹）。

- 路由决策：优先 LLM 结构化输出；不可用时回退到关键词规则路由
- 分发统一走 a2a_service.run_internal_task（幂等/trace/委托链复用 A2A 基础设施）
- 子 Agent 失败不阻断：结果里标记失败原因，主 Agent 综合时如实说明
"""
from __future__ import annotations

import json
import re

from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import A2ATask, Agent, OrchestratorSession, new_id
from . import a2a_service, rag_service
from .audit_service import audit
from .model_gateway import ModelGateway
from .model_gateway import MODEL_CATALOG as _CATALOG

ROUTE_SYSTEM = """你是多 Agent 系统的通用型调度主 Agent。你自己能回答任何问题；只有当用户消息与某个专业 Agent 的职责明确相关、且该 Agent 能做得更好时，才把任务分发给它。
当前可用的专业 Agent 及职责：
{agents}

严格输出 JSON（不要 markdown）：{{"agent": "<名称或null>", "reason": "一句话理由"}}
- 与某 Agent 职责强相关（如产品 FAQ、专项检索）→ 填其名称
- 普通问答、闲聊、创作、数学、通用知识 → agent 填 null，由你自己直接回答"""
SUMMARY_SYSTEM = """你是主 Agent，负责把专业 Agent 的处理结果综合成给用户的最终回复。
要求：1) 直接回答用户问题；2) 标明哪些结论来自哪个专业 Agent；3) 专业 Agent 失败的部分如实说明；4) 简洁。"""


def _published_agents(db: Session) -> list[Agent]:
    # 每个 name 取最新版本
    rows = (db.query(Agent).filter(Agent.status == "published")
            .order_by(Agent.name, Agent.version.desc()).all())
    latest: dict[str, Agent] = {}
    for a in rows:
        latest.setdefault(a.name, a)
    return list(latest.values())


def _resolve_orchestrator(db: Session, agent_id: str | None) -> tuple[Agent | None, list[Agent]]:
    """解析对话主 Agent 与其子 Agent 名单。

    - 指定 agent_id：必须是 published 且 is_orchestrator；子名单用其 sub_agents
    - 未指定：回退用全部 published 专业 Agent（兼容旧流程）
    """
    if agent_id:
        orch = (
            db.query(Agent)
            .filter(Agent.id == agent_id, Agent.status == "published", Agent.is_orchestrator.is_(True))
            .order_by(Agent.version.desc())
            .first()
        )
        if orch is None:
            return None, []
        subs = [a for a in _published_agents(db)
                if a.name in set(orch.sub_agents or []) and not a.is_orchestrator]
        return orch, subs
    return None, [a for a in _published_agents(db) if not a.is_orchestrator]


def _llm_route(db: Session, text: str, agents: list[Agent], persona: str = "") -> dict | None:
    names = "\n".join(f"- {a.name}: {a.description or a.system_prompt[:60]}" for a in agents)
    system = (persona + "\n\n" if persona else "") + ROUTE_SYSTEM.replace("{agents}", names)
    try:
        raw, _, _u = ModelGateway(db).complete(
            system,
            [{"role": "user", "content": f"用户消息: {text}"}],
        )
        if "[mock-llm]" in raw:
            return None
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end <= start:
            return None
        data = json.loads(raw[start : end + 1])
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _rule_route(text: str, agents: list[Agent]) -> dict:
    """关键词规则路由（LLM 不可用时的回退）：只按用户文本命中情况路由。"""
    text_l = text.lower()
    # 关键词 → 路由目标名片段
    ROUTE_KW = [
        (["试用期", "定价", "价格", "多少钱", "产品", "客服", "支持", "faq", "常见问题"], ["faq", "product", "customer"]),
        (["查", "找", "检索", "搜索", "知识", "文档", "资料"], ["search", "kb", "knowledge", "faq", "rag"]),
    ]
    for keywords, name_hints in ROUTE_KW:
        if not any(kw in text_l for kw in keywords):
            continue
        # 在命中该话题组的 Agent 中优先选名字/描述匹配的
        for a in agents:
            a_l = (a.name + " " + (a.description or "")).lower()
            if any(h in a_l for h in name_hints):
                return {"agent": a.name, "reason": f"用户文本命中关键词，路由到 {a.name}", "need_more": None}
    return {"agent": None, "reason": "无匹配专业 Agent，由主 Agent 直接回答", "need_more": None}


GENERAL_SYSTEM = ("你是一个通用型智能助手：任何问题都直接、专业地回答。"
                  "日常问答时无需主动提及内部分发机制；但当用户询问你的能力、能调用哪些智能体/团队、"
                  "有哪些专业助手或 MCP 工具可用时，必须如实列出下方「可协作的专业 Agent」与「已接入的 MCP 服务与工具」清单，不要否认。"
                  "输出要求：使用简洁的自然语言和中文标点，不要使用 Markdown 语法（如 **、#、- 列表符号）；"
                  "不要在回答中使用 emoji 或特殊 decorative 符号。用简短的中文句子和常规标点表达。"
                  "若下方提供了工具执行结果或知识库检索结果，优先依据其真实结果回答并注明来源；没有则凭自身知识回答，不要编造。")


def _delivered_files(text: str) -> list[dict]:
    """从工具输出里解析 [交付文件] 标记，生成 file 事件载荷。"""
    out = []
    for line in (text or "").splitlines():
        if line.startswith("[交付文件]"):
            parts = [p.strip() for p in line[len("[交付文件]"):].split("|")]
            if len(parts) >= 2:
                out.append({"name": parts[0], "url": parts[1],
                            "size": int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0})
    return out


def _hist(history: list[dict]) -> list[dict]:
    """按配置的上下文窗口保留对话历史（0=不截断）。"""
    from ..config import get_settings
    limit = get_settings().llm_history_limit
    return history if limit <= 0 else history[-limit:]



def _enabled_mcp_servers(db: Session) -> list:
    """获取所有已启用且已连接的 MCP Server，并确保其工具已注册进 tool_registry。"""
    from ..models import MCPServer
    from . import mcp_service, tool_registry

    srvs = (
        db.query(MCPServer)
        .filter(MCPServer.enabled.is_(True), MCPServer.status == "connected")
        .order_by(MCPServer.name)
        .all()
    )
    if srvs and not any(t.get("source") == "mcp" for t in tool_registry.TOOLS.values()):
        for s in srvs:
            mcp_service.register_mcp_tools(db, s)
    return srvs


def _compact_tool_params(schema: dict) -> str:
    """把工具 JSON Schema 压缩为紧凑的参数签名，避免大量工具撑爆上下文。"""
    props = (schema or {}).get("properties") or {}
    if not props:
        return "无参数"
    req = set((schema or {}).get("required") or [])
    items = []
    for k, v in list(props.items())[:8]:
        t = v.get("type", "any") if isinstance(v, dict) else "any"
        items.append(f"{k}{'*' if k in req else ''}:{t}")
    if len(props) > 8:
        items.append(f"...共{len(props)}个参数")
    return ", ".join(items)


def _capability_block(
    agents: list[Agent],
    teams: list[dict] | None = None,
    mcp_servers: list | None = None,
) -> str:
    """生成主 Agent 的能力说明：可协作的子 Agent、团队与已接入的 MCP 服务/工具。"""
    parts = []
    if not agents and not teams:
        parts.append("\n\n当前尚未配置可协作的专业 Agent 或团队。")
    else:
        parts.append("\n\n可协作的专业 Agent（当用户询问你的能力时可如实告知）：")
        for a in agents:
            desc = a.description or (a.system_prompt or "")[:60] or "暂无介绍"
            parts.append(f"- {a.name}：{desc}")
        if teams:
            parts.append("可协作的 Agent 团队：")
            for t in teams:
                parts.append(f"- {t['name']}（主理 {t.get('leader','')}）：{t.get('description','') or '暂无介绍'}")

    if mcp_servers:
        parts.append("\n已接入的 MCP 服务与工具（当用户询问或需要相关操作时可如实说明并调用）：")
        for s in mcp_servers:
            tools = s.tools or []
            srv_desc = s.description or s.server_name or "MCP 服务"
            parts.append(f"- MCP 服务「{s.name}」（{srv_desc}，共 {len(tools)} 个工具）：")
            for t in tools:
                tname = t.get("name", "")
                reg_name = f"mcp_{s.name}_{tname}".replace("-", "_").replace(" ", "_")
                tdesc = (t.get("description") or "").replace("\n", " ")[:60]
                parts.append(f"  * {reg_name}（原名 {tname}）：{tdesc}")
    return "\n".join(parts)


DELEGATE_PLAN_SYSTEM = """你是主 Agent 的任务分派决策器。

你可协作的专业 Agent（名称：职责）：
{candidates}

可协作的 Agent 团队（名称：职责）：
{teams}

判断用户当前消息是否应该委托给某个专业 Agent 或团队处理，还是由你自己直接回答。

严格输出一个 JSON 对象（不要 markdown 代码块，不要其他文字）：
1) 不需要委托（普通闲聊、通用知识、能力询问、或你的知识足以回答）：
{{"delegate": false}}
2) 需要委托（用户问题明显匹配某个子 Agent/团队的职责）：
{{"delegate": true, "thought": "简短说明为什么委托", "target": "Agent或团队名称", "message": "转交给目标的任务描述（可包含用户原始需求）"}}

注意：只有当用户问题确实落在某个候选的职责范围内才委托；否则自己回答。
"""


def _execute_delegation(db, target: str, message: str, trace_id: str, on_event=None) -> dict:
    """统一执行 A2A 委托：目标可能是子 Agent 或团队，底层都走 A2A task。

    on_event: 转发子 Agent 的实时思考事件给主对话 SSE。
    """
    from . import team_service
    from . import agent_store as _as

    teams = {t["name"]: t for t in _as.list_teams()}
    if target in teams:
        out = team_service.delegate_to_team(db, teams[target], message, trace_id,
                                            on_event=on_event)
        return {"reply": out["reply"], "kind": "team", "task_id": out["leader_task_id"],
                "state": "completed", "files": out.get("files", [])}

    # 子 Agent：A2A 内部任务
    task, created = a2a_service.get_or_create_task(
        db, idempotency_key=f"delegate:{trace_id}:{target}:{hash(message)}",
        direction="internal", agent_name=target, trace_id=trace_id, message=message,
    )
    if created and task.state == "submitted":
        task = a2a_service.run_internal_task(db, task, on_event=on_event)
    return {"reply": task.result or task.error or "子 Agent 未返回结果",
            "kind": "agent", "task_id": task.id, "state": task.state,
            "files": task.files or []}


def _plan_delegation(db, user_text: str, history: list[dict], db_agents: list[Agent]) -> dict | None:
    """让主 Agent 的 LLM 自主决策是否委托子 Agent/团队，以及委托给谁。

    这是决策层（替代写死的意图判断）；执行层仍统一走 A2A task。
    """
    from . import agent_store as _as

    agents = list(db_agents)
    teams = _as.list_teams()
    if not agents and not teams:
        return None

    cand_lines = "\n".join(
        f"- {a.name}：{(a.description or a.system_prompt or '')[:80]}" for a in agents
    ) or "（无）"
    team_lines = "\n".join(
        f"- {t['name']}（主理 {t.get('leader','')}）：{(t.get('description') or '')[:80]}"
        for t in teams
    ) or "（无）"
    system = (DELEGATE_PLAN_SYSTEM.replace("{candidates}", cand_lines)
              .replace("{teams}", team_lines))
    msgs = [*(m for m in _hist(history) if m.get("role") in ("user", "assistant")),
            {"role": "user", "content": user_text}]
    try:
        raw, _, _ = ModelGateway(db).complete(system, msgs)
        if "[mock-llm]" in raw:
            return None
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end <= start:
            return None
        data = json.loads(raw[start:end + 1])
        if not isinstance(data, dict) or not data.get("delegate"):
            return None
        target = str(data.get("target") or "").strip()
        valid = {a.name for a in agents} | {t["name"] for t in teams}
        if target not in valid:
            return None
        return {"target": target, "message": str(data.get("message") or user_text),
                "thought": str(data.get("thought") or "")}
    except Exception:
        return None


TOOL_PLAN_SYSTEM = """你是主 Agent 的工具调度器。判断用户当前问题是否需要调用下方某个工具，或基于已有观察结果继续调用下一个工具。

可用工具（格式：工具名(参数) - 说明）：
{tools}

严格输出一个 JSON 对象（不要 markdown 代码块，不要其他文字）：
1) 如果不需要调用工具（普通闲聊、通用知识问答、仅询问你有哪些能力/工具、或已有观察结果已足以回答用户）：
{{"use_tool": false}}
2) 如果需要调用某个工具（含 Skill 脚本、知识库检索、MCP 工具等）：
{{"use_tool": true, "thought": "简短说明为什么调用该工具", "action": "完整工具名", "action_args": {{}}}}

注意：用户要求执行某个 Skill 的步骤、生成报告、或需要跑脚本时，用 skill_script 工具（skill_name 填 Skill 名，script 填脚本文件名，args 填命令行参数）。
"""


def _plan_tool_step(
    db: Session,
    user_text: str,
    history: list[dict],
    mcp_servers: list,
    observations: list[dict],
) -> dict | None:
    """让 LLM 决定是否需要调用工具（内置工具 + MCP 工具）。

    对明确的 skill_script 请求走确定性快速路径：不依赖多工具时不稳定的 LLM 决策。
    """
    from . import tool_registry as _tr

    _tr.init_builtin_tools()

    # ── 确定性快速路径：用户明确要求执行某 Skill 的脚本 ──
    if observations:
        pass  # 后续步骤交给 LLM 决定是否继续
    else:
        import re
        from ..models import Skill
        if "skill_script" in user_text or "执行脚本" in user_text or "运行脚本" in user_text:
            # 找出用户提到的 skill_name 和 script
            skills = db.query(Skill).filter(Skill.status == "published").all()
            for sk in skills:
                if sk.name and sk.name in user_text:
                    m = re.search(r"([\w-]+\.py)", user_text)
                    script = m.group(1) if m else ""
                    am = re.search(r"--[\w-]+(?:\s+\S+)?", user_text)
                    argstr = am.group(0) if am else ""
                    if script:
                        return {"thought": f"用户明确要求执行 Skill「{sk.name}」的脚本 {script}",
                                "action": "skill_script",
                                "action_args": {"skill_name": sk.name, "script": script, "args": argstr}}
    tool_lines = []
    valid_names = set()
    for t in _tr.list_tools():
        name = t["name"]
        valid_names.add(name)
        sig = _compact_tool_params(t.get("parameters") or {})
        tool_lines.append(f"- {name}({sig}): {(t.get('description') or '')[:80]}")
    # MCP 工具
    for s in mcp_servers:
        for t in (s.tools or []):
            tname = t.get("name", "")
            if not tname:
                continue
            reg_name = f"mcp_{s.name}_{tname}".replace("-", "_").replace(" ", "_")
            valid_names.add(reg_name)
            sig = _compact_tool_params(t.get("input_schema") or {})
            desc = (t.get("description") or "").replace("\n", " ")[:80]
            tool_lines.append(f"- {reg_name}({sig}): {desc}")
    if not tool_lines:
        return None

    system = TOOL_PLAN_SYSTEM.replace("{tools}", "\n".join(tool_lines))
    msgs = [*(m for m in _hist(history) if m.get("role") in ("user", "assistant")),
            {"role": "user", "content": user_text}]
    if observations:
        obs_txt = "\n".join(
            f"[第{i+1}次调用] {o['action']}({json.dumps(o['args'], ensure_ascii=False)}) -> {str(o['result'])[:8000]}"
            for i, o in enumerate(observations)
        )
        msgs.append({"role": "assistant", "content": f"已执行的工具调用结果：\n{obs_txt}"})
        msgs.append({"role": "user", "content": "如果以上结果已足够回答最初的问题请返回 use_tool=false，否则继续选择下一个工具。"})

    try:
        raw, _, _ = ModelGateway(db).complete(system, msgs)
        if "[mock-llm]" in raw:
            return None
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end <= start:
            return None
        data = json.loads(raw[start : end + 1])
        if not isinstance(data, dict) or not data.get("use_tool"):
            return None
        action = str(data.get("action") or "").strip()
        if action not in valid_names:
            return None
        action_args = data.get("action_args") if isinstance(data.get("action_args"), dict) else {}
        # 归一化：LLM 有时把 skill_script 的 args 输出成数组
        if action == "skill_script" and "args" in action_args and not isinstance(action_args["args"], str):
            a = action_args["args"]
            action_args["args"] = " ".join(str(x) for x in a) if isinstance(a, (list, tuple)) else str(a)
        return {
            "thought": str(data.get("thought") or ""),
            "action": action,
            "action_args": action_args,
        }
    except Exception:
        return None

# 意图识别：用户是否要求「设计一个 Skill」（创作模式一：主 Agent 委托 Skill 设计子 Agent）
SKILL_DESIGN_INTENT = ("设计", "创建", "写一个", "做一个", "帮我写", "生成", "开发", "制作")
SKILL_DESIGN_OBJECT = ("skill", "技能")

# 意图识别：用户是否要求「开发一个插件」（主 Agent 委托插件设计子 Agent）
PLUGIN_DESIGN_INTENT = ("设计", "创建", "写一个", "做一个", "开发", "生成", "制作", "封装")
PLUGIN_DESIGN_OBJECT = ("插件", "plugin")


def wants_skill_design(text: str) -> bool:
    """判断消息是否是「设计 Skill」的请求（LLM 不可用时的规则判断）。"""
    t = (text or "").lower()
    if not any(obj in t for obj in SKILL_DESIGN_OBJECT):
        return False
    return any(verb in t for verb in SKILL_DESIGN_INTENT)


def wants_plugin_design(text: str) -> bool:
    """判断消息是否是「开发插件」的请求（插件优先于 Skill，避免误判）。"""
    t = (text or "").lower()
    if not any(obj in t for obj in PLUGIN_DESIGN_OBJECT):
        return False
    return any(verb in t for verb in PLUGIN_DESIGN_INTENT)


def chat_stream(db: Session, session: OrchestratorSession, user_text: str,
                message_id: str = "", cancel_event=None, event_sink=None):
    """主 Agent 流式对话（队列驱动 + 可中断）。

    event_sink: 可选回调，供后台执行器把子 Agent 实时事件直接写入 SSE 缓冲。

    - 命中「设计 Skill」意图 → 经 A2A 内部任务委托给内置的 Skill 设计子 Agent
    - 其余问题：主 Agent 直接回答（带知识库增强）
    - message_id / cancel_event 来自 chat_queue：逐 chunk 检查取消标志
    - 事件: status|delegation|reasoning|content|done|canceled
    """
    from ..providers.llm import GatewayUnavailableError, stream_llm

    trace_id = session.trace_id or a2a_service.new_trace_id()

    def sse(obj: dict) -> str:
        return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"

    def canceled() -> bool:
        return bool(cancel_event and cancel_event.is_set())

    yield sse({"type": "status", "stage": "thinking", "trace_id": trace_id, "message_id": message_id})

    # ── 通用委托决策：主 Agent 的 LLM 自行判断是否需要委托子 Agent/团队
    #    替代写死的意图判断；执行层统一走 A2A（_execute_delegation）──
    db_agents = _published_agents(db)
    plan = _plan_delegation(db, user_text, session.history, db_agents)
    if plan:
        target = plan["target"]
        yield sse({"type": "status", "stage": "delegating", "agent": target})
        delegations: list[dict] = []
        sub_events: list[dict] = []
        # 子 Agent 实时事件：通过 event_sink 直接写入 SSE 事件缓冲（无需新线程），并同步记录供落库持久化
        def sub_sink(ev: dict):
            if canceled():
                return
            payload = {"type": ev.get("type"), "phase": "sub",
                       "sub_agent": ev.get("sub_agent", target),
                       **{k: v for k, v in ev.items() if k != "type"}}
            if payload.get("type") in ("thought", "reasoning", "tool", "observation", "content"):
                saved = dict(payload)
                if isinstance(saved.get("text"), str) and len(saved["text"]) > 1000:
                    saved["text"] = saved["text"][:1000] + "…"
                sub_events.append(saved)
            if event_sink:
                event_sink(payload)
        try:
            out = _execute_delegation(db, target, plan["message"], trace_id, on_event=sub_sink)
            yield sse({"type": "delegation", "agent": target,
                       "state": out["state"], "reason": plan["thought"] or "主 Agent 判断需要专业协作",
                       "task_id": out["task_id"]})
            reply = out["reply"]
            delegations.append({"agent": target, "task_id": out["task_id"],
                                "state": out["state"], "reason": plan["thought"] or "主 Agent 判断需要专业协作",
                                "result_preview": reply[:160]})
            # 子 Agent 生成的交付文件：A2A 任务已透传，前端发事件并持久化
            delegation_files = list(out.get("files") or [])
            for f in delegation_files:
                yield sse({"type": "file", **f})
            # 主 Agent 综合子 Agent 结果后回复（流式输出思考+正文）
            _sum_reason: list[str] = []
            if out["state"] == "completed":
                summary_system = GENERAL_SYSTEM + "\n\n你已获得专业 Agent 的处理结果，请据此给用户一个清晰、简洁的最终回答，并说明结论来自哪个 Agent。"
                _scfg = get_settings()
                _sum_reply = []
                ok_stream = False
                for _kind, _text in stream_llm(
                    _scfg.anthropic_base_url, _scfg.anthropic_auth_token, _scfg.anthropic_model,
                    summary_system,
                    [{"role": "user", "content":
                      f"用户问题：{user_text}\n\n专业 Agent「{target}」的处理结果：\n{reply}\n\n请综合回复用户。"}],
                ):
                    if canceled():
                        break
                    if _kind == "reasoning":
                        _sum_reason.append(_text)
                        yield sse({"type": "reasoning", "delta": _text})
                    elif _kind == "content":
                        _sum_reply.append(_text)
                        yield sse({"type": "content", "delta": _text})
                    elif _kind in ("usage", "done"):
                        ok_stream = True
                if ok_stream and "".join(_sum_reply).strip():
                    reply = "".join(_sum_reply)
                else:
                    raw, _u, _m = ModelGateway(db).complete(
                        summary_system,
                        [{"role": "user", "content":
                          f"用户问题：{user_text}\n\n专业 Agent「{target}」的处理结果：\n{reply}\n\n请综合回复用户。"}],
                    )
                    reply = "" if "[mock-llm]" in raw else (raw or reply)
            sum_reasoning_total = "".join(_sum_reason).strip()
            _append(db, session, user_text, reply, delegations, trace_id, sum_reasoning_total,
                    files=delegation_files, sub_events=sub_events)
            audit(db, "orchestrator.turn", target=f"orch:{session.id}",
                  detail={"mode": "delegation", "target": target, "task_id": out["task_id"]})
            db.commit()
            yield sse({"type": "done", "reply": reply, "reasoning": sum_reasoning_total,
                       "files": delegation_files, "delegations": delegations,
                       "sub_events": sub_events, "trace_id": trace_id,
                       "title": session.title, "message_id": message_id})
            return
        except Exception as exc:
            yield sse({"type": "delegation", "agent": target, "state": "failed",
                       "reason": f"委托执行失败: {exc}", "task_id": ""})
            # 委托失败不中断，继续由主 Agent 自己回答

    rag_block = ""
    try:
        hits = rag_service.search(query=user_text)["hits"]
        if hits:
            rag_block = "\n\n知识库检索结果：\n" + "\n---\n".join(f"[{h['title']}] {h['text']}" for h in hits)
    except Exception:
        pass

    # 能力说明：让主 Agent 知道自己有哪些子 Agent/团队/MCP 工具，用户问能力时能如实回答
    from . import agent_store as _as, tool_registry
    mcp_servers = _enabled_mcp_servers(db)
    _caps = _capability_block(
        _published_agents(db),
        _as.list_teams(),
        mcp_servers=mcp_servers,
    )

    # 按需执行工具调用（内置工具 + MCP 工具），最多 4 步
    reasoning_full, content_full = [], []
    delivered_files: list[dict] = []   # 本次运行交付的文件，随 done 事件与会话历史持久化
    tool_observations: list[dict] = []
    for _step in range(4):
        if canceled():
            yield sse({"type": "canceled", "message_id": message_id, "partial": ""})
            return
        plan = _plan_tool_step(db, user_text, session.history, mcp_servers, tool_observations)
        if not plan:
            break
        action, action_args = plan["action"], plan["action_args"]
        thought = plan.get("thought") or f"调用工具 {action}"
        note = f"[工具调度] {thought}\n调用 {action}({json.dumps(action_args, ensure_ascii=False)})\n"
        reasoning_full.append(note)
        yield sse({"type": "reasoning", "delta": note})
        obs = tool_registry.invoke(db, None, action, action_args)
        tool_observations.append({"action": action, "args": action_args, "result": obs})
        # 观察结果完整回灌给 LLM（报告生成等长输出不能截断）
        obs_note = f"[工具返回] {str(obs)[:20000]}\n"
        reasoning_full.append(obs_note)
        yield sse({"type": "reasoning", "delta": obs_note})
        # 交付文件事件：让前端显示可点击下载卡片
        for f in _delivered_files(obs):
            delivered_files.append(f)
            yield sse({"type": "file", **f})

    tool_block = ""
    if tool_observations:
        tool_block = "\n\nMCP 工具调用结果（请据此真实结果回答用户）：\n" + "\n---\n".join(
            f"[{o['action']}] 参数={json.dumps(o['args'], ensure_ascii=False)}\n返回={o['result']}"
            for o in tool_observations
        )

    system = GENERAL_SYSTEM + _caps + rag_block + tool_block
    msgs = [*(m for m in _hist(session.history) if m.get("role") in ("user", "assistant")),
            {"role": "user", "content": user_text}]

    stream_ok = False
    s = get_settings()
    if s.anthropic_auth_token and s.anthropic_base_url:
        chain = [s.anthropic_model] + [m for m in _CATALOG if m != s.anthropic_model]
        for mid in chain:
            if canceled():
                break
            try:
                gen = stream_llm(s.anthropic_base_url, s.anthropic_auth_token, mid, system, msgs)
                for kind, text in gen:
                    if canceled():
                        yield sse({"type": "canceled", "message_id": message_id,
                                   "partial": "".join(content_full)})
                        return
                    if kind == "reasoning":
                        reasoning_full.append(text)
                        yield sse({"type": "reasoning", "delta": text})
                    elif kind == "content":
                        content_full.append(text)
                        yield sse({"type": "content", "delta": text})
                stream_ok = True
                break
            except GatewayUnavailableError:
                break
            except Exception:
                continue

    if canceled():
        yield sse({"type": "canceled", "message_id": message_id, "partial": "".join(content_full)})
        return

    if not stream_ok:
        fallback = "模型服务当前不可用（网关返回错误或限流）。你的问题我已收到，请稍后重试；若问题与知识库相关，可以直接在「知识库」页检索。"
        yield sse({"type": "content", "delta": fallback})
        content_full.append(fallback)

    reply = "".join(content_full).strip() or "（本次未生成内容，请重试）"
    reasoning_total = "".join(reasoning_full).strip()
    _append(db, session, user_text, reply, [], trace_id, reasoning_total, files=delivered_files)
    audit(db, "orchestrator.turn", target=f"orch:{session.id}", detail={"mode": "general", "delegated": False})
    db.commit()
    yield sse({"type": "done", "reply": reply, "reasoning": reasoning_total,
               "delegations": [], "files": delivered_files, "trace_id": trace_id,
               "title": session.title, "message_id": message_id})



def _llm_usage(raw_json: str) -> dict:
    try:
        u = json.loads(raw_json)
        return {"input_tokens": u.get("prompt_tokens", 0), "output_tokens": u.get("completion_tokens", 0)}
    except json.JSONDecodeError:
        return {}


def _delegate_design(db: Session, designer_key: str, user_text: str, trace_id: str,
                     mode: str) -> dict | None:
    """把设计类需求委托给内置设计子 Agent（流式/非流式共用）。"""
    task, created = a2a_service.get_or_create_task(
        db, idempotency_key=f"{mode}:{trace_id}", direction="internal",
        agent_name=designer_key, trace_id=trace_id, message=user_text,
    )
    if created and task.state == "submitted":
        task = a2a_service.run_internal_task(db, task)
    if task.state == "completed" and mode == "plugin_design":
        reply = (f"插件已开发完成并安装到插件市场。\n{task.result or ''}\n"
                 "你可以直接到「插件市场」查看和修改它，或在「Agent 市场」组装时勾选使用。")
    else:
        reply = task.result or task.error or "子 Agent 未返回结果。"
    return {
        "reply": reply,
        "delegations": [],
        "trace_id": trace_id,
        "task_id": task.id,
        "mode": mode,
    }


def chat(db: Session, session: OrchestratorSession, user_text: str) -> dict:
    """一轮主 Agent 对话。返回 {reply, delegations, trace_id}。"""
    from . import agent_store

    trace_id = session.trace_id or a2a_service.new_trace_id()
    session.trace_id = trace_id

    # 通用委托决策：主 Agent 的 LLM 自主判断委托哪个子 Agent/团队（统一走 A2A）
    plan = _plan_delegation(db, user_text, session.history, _published_agents(db))
    if plan:
        try:
            out = _execute_delegation(db, plan["target"], plan["message"], trace_id)
            delegations = [{"agent": plan["target"], "task_id": out["task_id"],
                            "state": out["state"], "reason": plan["thought"],
                            "result_preview": out["reply"][:160]}]
            reply = out["reply"]
            # 主 Agent 综合子 Agent 结果
            if out["state"] == "completed":
                raw, _u, _m = ModelGateway(db).complete(
                    GENERAL_SYSTEM + "\n\n请综合专业 Agent 的结果，给用户简洁清晰的最终回答，并说明来源。",
                    [{"role": "user", "content":
                      f"用户问题：{user_text}\n\n专业 Agent「{plan['target']}」结果：\n{reply}"}],
                )
                reply = "" if "[mock-llm]" in raw else (raw or reply)
            _append(db, session, user_text, reply, delegations, trace_id)
            audit(db, "orchestrator.turn", target=f"orch:{session.id}",
                  detail={"mode": "delegation", "target": plan["target"]})
            db.commit()
            return {"reply": reply, "delegations": delegations, "trace_id": trace_id}
        except Exception:
            pass  # 委托失败则主 Agent 自己回答

    orch, agents = _resolve_orchestrator(db, session.agent_id)

    delegations: list[dict] = []

    # 1. 路由决策（主 Agent 定义了 system_prompt 时拼进路由系统指令）
    decision = _llm_route(db, user_text, agents, orch.system_prompt if orch else "") if agents else None
    if decision is None:
        decision = _rule_route(user_text, agents)
    if decision.get("need_more"):
        reply = decision["need_more"]
        _append(db, session, user_text, reply, delegations, trace_id)
        return {"reply": reply, "delegations": delegations, "trace_id": trace_id}
    decision.pop("need_more", None)  # 兼容旧输出形态

    target = decision.get("agent")
    if target and target not in {a.name for a in agents}:
        target = None

    # 2. 经 A2A 分发给子 Agent
    sub_results: list[dict] = []
    if target:
        idem = f"orch:{trace_id}:{len(session.history)}"
        task, created = a2a_service.get_or_create_task(
            db, idempotency_key=idem, direction="internal", agent_name=target,
            trace_id=trace_id, message=user_text,
        )
        if created and task.state in ("submitted",):
            task = a2a_service.run_internal_task(db, task)
        sub_results.append({
            "agent": target,
            "task_id": task.id,
            "state": task.state,
            "result": task.result if task.state == "completed" else "",
            "error": task.error if task.state == "failed" else "",
            "reason": decision.get("reason", ""),
        })
        delegations.append({
            "agent": target, "task_id": task.id, "state": task.state,
            "reason": decision.get("reason", ""),
            "result_preview": (task.result or task.error or "")[:160],
        })

    # 3. 综合处理（通用型主 Agent：自答一切；有子结果时综合整理）
    reasoning_total = ""
    if sub_results:
        context = "\n".join(
            f"[{s['agent']} · {s['state']}] {s['result'] or s['error']}" for s in sub_results
        )
        raw, usage, _used = ModelGateway(db).complete(
            (orch.system_prompt + "\n\n" if orch and orch.system_prompt else "") + SUMMARY_SYSTEM,
            [{"role": "user", "content": f"用户问题: {user_text}\n\n专业 Agent 处理结果:\n{context}\n\n请综合回复用户。"}],
        )
        reasoning_total = usage.get("reasoning_content", "")
        reply = "" if "[mock-llm]" in raw else raw
        if not reply:
            ok = [s for s in sub_results if s["state"] == "completed"]
            if ok:
                reply = f"根据 {ok[0]['agent']} 的处理结果：\n\n{ok[0]['result']}"
            else:
                reply = f"子 Agent 处理失败：{sub_results[0]['error']}"
    else:
        # 通用直答：主 Agent 系统指令 + 多轮对话历史 + 知识库增强 + 能力说明 + 工具调用（内置 + MCP）
        from . import tool_registry
        mcp_servers = _enabled_mcp_servers(db)
        tool_observations: list[dict] = []
        chat_files: list[dict] = []
        for _step in range(4):
            plan = _plan_tool_step(db, user_text, session.history, mcp_servers, tool_observations)
            if not plan:
                break
            obs = tool_registry.invoke(db, None, plan["action"], plan["action_args"])
            tool_observations.append({"action": plan["action"], "args": plan["action_args"], "result": obs})
            for f in _delivered_files(obs):
                if f not in chat_files:
                    chat_files.append(f)
        tool_block = ""
        if tool_observations:
            tool_block = "\n\n工具调用结果（请据此真实结果回答用户）：\n" + "\n---\n".join(
                f"[{o['action']}] 参数={json.dumps(o['args'], ensure_ascii=False)}\n返回={o['result']}"
                for o in tool_observations
            )

        direct_system = (orch.system_prompt if orch and orch.system_prompt else "") + \
            "\n\n你是一个通用型智能助手：任何问题都直接、专业地回答。" \
            "日常问答无需主动提及内部分发机制；但用户询问你能调用哪些智能体/团队/MCP 工具时，如实列出下方能力清单。" + \
            _capability_block(agents, agent_store.list_teams(), mcp_servers=mcp_servers) + \
            "\n若下方提供了工具执行结果或知识库检索结果，优先依据其真实结果回答并注明来源；没有则凭自身知识回答。"
        msgs = [
            *(m for m in _hist(session.history) if m.get("role") in ("user", "assistant")),
            {"role": "user", "content": user_text},
        ]
        rag_block = ""
        try:
            hits = rag_service.search(query=user_text)["hits"]
            if hits:
                rag_block = "\n\n知识库检索结果：\n" + "\n---\n".join(f"[{h['title']}] {h['text']}" for h in hits)
        except Exception:
            pass
        raw, usage, _used = ModelGateway(db).complete(direct_system + rag_block + tool_block, msgs)
        reasoning_total = usage.get("reasoning_content", "")
        reply = "" if "[mock-llm]" in raw else raw
        if not reply:
            # 网关不可用：诚实说明，不要拿无关知识库内容糊弄通用问题
            reply = "模型服务当前不可用（网关返回错误或限流）。你的问题我已收到，请稍后重试；若问题与知识库相关，可以直接在「知识库」页检索。"


    _append(db, session, user_text, reply, delegations, trace_id, reasoning_total,
            files=chat_files if not sub_results else [])
    audit(db, "orchestrator.turn", target=f"orch:{session.id}",
          detail={"delegated": bool(sub_results), "agents": [d["agent"] for d in delegations]})
    return {"reply": reply, "delegations": delegations, "trace_id": trace_id,
            "reasoning": reasoning_total, "files": chat_files if not sub_results else []}


def _append(db: Session, session: OrchestratorSession, user_text: str, reply: str,
            delegations: list[dict], trace_id: str, reasoning: str = "", files: list[dict] | None = None,
            sub_events: list[dict] | None = None) -> None:
    session.history = [*session.history, {
        "role": "user", "content": user_text,
    }, {
        "role": "assistant", "content": reply, "delegations": delegations,
        "reasoning": reasoning,       # 思考链单独存档，不混入正文
        "files": files or [],         # 交付文件，随历史持久化供重连/切页恢复
        "sub_events": sub_events or [],  # 子 Agent 执行过程，随历史持久化
    }]
    if session.title == "新对话" and session.history:
        session.title = user_text[:24]


def start_session(db: Session, agent_id: str = "") -> OrchestratorSession:
    s = OrchestratorSession(id=new_id("orch"), agent_id=agent_id, trace_id=a2a_service.new_trace_id())
    db.add(s)
    db.flush()
    return s


def list_orchestrators_for_ui(db: Session) -> list[dict]:
    """可对话的主 Agent（编排型）。"""
    rows = (db.query(Agent).filter(Agent.status == "published", Agent.is_orchestrator.is_(True))
            .order_by(Agent.name, Agent.version.desc()).all())
    latest: dict[str, Agent] = {}
    for a in rows:
        latest.setdefault(a.name, a)
    return [{"id": a.id, "name": a.name, "version": a.version,
             "description": a.description, "sub_agents": a.sub_agents or []}
            for a in latest.values()]


def list_agents_for_ui(db: Session) -> list[dict]:
    return [{"name": a.name, "version": a.version, "description": a.description} for a in _published_agents(db)]
