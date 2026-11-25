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
                  "有哪些专业助手可用时，必须如实列出下方「可协作的专业 Agent」清单，不要否认。"
                  "输出要求：使用简洁的自然语言和中文标点，不要使用 Markdown 语法（如 **、#、- 列表符号）；"
                  "不要在回答中使用 emoji 或特殊 decorative 符号。用简短的中文句子和常规标点表达。"
                  "若下方提供了知识库检索结果，优先依据其回答并注明来源；没有则凭自身知识回答。")


def _capability_block(agents: list[Agent], teams: list[dict] | None = None) -> str:
    """生成主 Agent 的能力说明：可协作的子 Agent 与团队，注入直答 prompt。
    这样用户问「你能调用哪些智能体」时，主 Agent 能如实回答。"""
    if not agents and not teams:
        return "\n\n当前尚未配置可协作的专业 Agent 或团队。"
    parts = ["\n\n可协作的专业 Agent（当用户询问你的能力时可如实告知）："]
    for a in agents:
        desc = a.description or (a.system_prompt or "")[:60] or "暂无介绍"
        parts.append(f"- {a.name}：{desc}")
    if teams:
        parts.append("可协作的 Agent 团队：")
        for t in teams:
            parts.append(f"- {t['name']}（主理 {t.get('leader','')}）：{t.get('description','') or '暂无介绍'}")
    return "\n".join(parts)

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
                message_id: str = "", cancel_event=None):
    """主 Agent 流式对话（队列驱动 + 可中断 + Skill 设计委托）。

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

    # ── 意图识别：是否需要插件设计子 Agent（优先于 Skill，避免"插件"误判）──
    if wants_plugin_design(user_text):
        from . import plugin_designer_service

        yield sse({"type": "status", "stage": "delegating", "agent": plugin_designer_service.DESIGNER_KEY})
        idem = f"plugin-design:{trace_id}:{len(session.history)}"
        task, created = a2a_service.get_or_create_task(
            db, idempotency_key=idem, direction="internal",
            agent_name=plugin_designer_service.DESIGNER_KEY,
            trace_id=trace_id, message=user_text,
        )
        if created and task.state == "submitted":
            task = a2a_service.run_internal_task(db, task)
        yield sse({"type": "delegation", "agent": plugin_designer_service.DESIGNER_KEY,
                   "state": task.state, "reason": "识别到开发插件的需求，委托专业子 Agent 处理",
                   "task_id": task.id})
        reply = task.result or task.error or "子 Agent 未返回结果。"
        if task.state == "completed":
            reply = (f"插件已开发完成并安装到插件市场。\n{reply}\n"
                     "你可以直接到「插件市场」查看和修改它，或在「Agent 市场」组装时勾选使用。")
        _append(db, session, user_text, reply, [], trace_id, "")
        audit(db, "orchestrator.turn", target=f"orch:{session.id}",
              detail={"mode": "plugin_design", "task_id": task.id})
        db.commit()
        yield sse({"type": "done", "reply": reply, "reasoning": "", "delegations": [],
                   "trace_id": trace_id, "title": session.title, "message_id": message_id})
        return

    # ── 意图识别：是否需要 Skill 设计子 Agent ──
    if wants_skill_design(user_text):
        from . import skill_designer_service

        yield sse({"type": "status", "stage": "delegating", "agent": skill_designer_service.DESIGNER_KEY})
        idem = f"skill-design:{trace_id}:{len(session.history)}"
        task, created = a2a_service.get_or_create_task(
            db, idempotency_key=idem, direction="internal",
            agent_name=skill_designer_service.DESIGNER_KEY,
            trace_id=trace_id, message=user_text,
        )
        if created and task.state == "submitted":
            task = a2a_service.run_internal_task(db, task)
        yield sse({"type": "delegation", "agent": skill_designer_service.DESIGNER_KEY,
                   "state": task.state, "reason": "识别到设计 Skill 的需求，委托专业子 Agent 处理",
                   "task_id": task.id})
        reply = task.result or task.error or "子 Agent 未返回结果。"
        _append(db, session, user_text, reply, [], trace_id, "")
        audit(db, "orchestrator.turn", target=f"orch:{session.id}",
              detail={"mode": "skill_design", "task_id": task.id})
        db.commit()
        yield sse({"type": "done", "reply": reply, "reasoning": "", "delegations": [],
                   "trace_id": trace_id, "title": session.title, "message_id": message_id})
        return

    # ── 意图识别：是否需要 Agent 团队协作（主 Agent 只和主理 Agent 通信）──
    from . import team_service

    explicit_team = ""  # 可由上层 message metadata 传入；此处按描述匹配
    team = team_service.find_team_for_request(db, user_text, explicit_team)
    if team:
        yield sse({"type": "status", "stage": "delegating", "agent": team["leader"]})
        out = team_service.delegate_to_team(db, team, user_text, trace_id)
        yield sse({"type": "delegation", "agent": team["leader"],
                   "state": "completed", "reason": f"团队「{team['name']}」主理协调",
                   "task_id": out["leader_task_id"]})
        _append(db, session, user_text, out["reply"], [], trace_id, "")
        audit(db, "orchestrator.turn", target=f"orch:{session.id}", detail={"mode": "team", "team": team["name"]})
        db.commit()
        yield sse({"type": "done", "reply": out["reply"], "reasoning": "", "delegations": [],
                   "trace_id": trace_id, "title": session.title, "message_id": message_id})
        return

    rag_block = ""
    try:
        hits = rag_service.search(query=user_text)["hits"]
        if hits:
            rag_block = "\n\n知识库检索结果：\n" + "\n---\n".join(f"[{h['title']}] {h['text']}" for h in hits)
    except Exception:
        pass

    # 能力说明：让主 Agent 知道自己有哪些子 Agent/团队，用户问能力时能如实回答
    from . import agent_store as _as
    _caps = _capability_block(
        _published_agents(db),
        _as.list_teams(),
    )
    system = GENERAL_SYSTEM + _caps + rag_block
    msgs = [*(m for m in session.history[-8:] if m.get("role") in ("user", "assistant")),
            {"role": "user", "content": user_text}]

    reasoning_full, content_full = [], []
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
    _append(db, session, user_text, reply, [], trace_id, reasoning_total)
    audit(db, "orchestrator.turn", target=f"orch:{session.id}", detail={"mode": "general", "delegated": False})
    db.commit()
    yield sse({"type": "done", "reply": reply, "reasoning": reasoning_total,
               "delegations": [], "trace_id": trace_id, "title": session.title,
               "message_id": message_id})



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
    trace_id = session.trace_id or a2a_service.new_trace_id()
    session.trace_id = trace_id

    # 设计类意图优先：插件 / Skill（非流式端点也支持）
    if wants_plugin_design(user_text):
        from . import plugin_designer_service

        out = _delegate_design(db, plugin_designer_service.DESIGNER_KEY,
                               user_text, trace_id, "plugin_design")
        _append(db, session, user_text, out["reply"], [], trace_id)
        audit(db, "orchestrator.turn", target=f"orch:{session.id}", detail={"mode": "plugin_design"})
        db.commit()
        return out
    if wants_skill_design(user_text):
        from . import skill_designer_service

        out = _delegate_design(db, skill_designer_service.DESIGNER_KEY,
                               user_text, trace_id, "skill_design")
        _append(db, session, user_text, out["reply"], [], trace_id)
        audit(db, "orchestrator.turn", target=f"orch:{session.id}", detail={"mode": "skill_design"})
        db.commit()
        return out

    # 团队路由（主 Agent 只和主理 Agent 通信）
    from . import team_service, agent_store

    team = team_service.find_team_for_request(db, user_text)
    if team:
        out = team_service.delegate_to_team(db, team, user_text, trace_id)
        _append(db, session, user_text, out["reply"], [], trace_id, "")
        audit(db, "orchestrator.turn", target=f"orch:{session.id}", detail={"mode": "team", "team": team["name"]})
        db.commit()
        return out

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
        # 通用直答：主 Agent 系统指令 + 多轮对话历史 + 知识库增强 + 能力说明
        direct_system = (orch.system_prompt if orch and orch.system_prompt else "") + \
            "\n\n你是一个通用型智能助手：任何问题都直接、专业地回答。" \
            "日常问答无需主动提及内部分发机制；但用户询问你能调用哪些智能体/团队时，如实列出下方能力清单。" + \
            _capability_block(agents, agent_store.list_teams()) + \
            "\n若下方提供了知识库检索结果，优先依据其回答并注明来源；没有则凭自身知识回答。"
        msgs = [
            *(m for m in session.history[-8:] if m.get("role") in ("user", "assistant")),
            {"role": "user", "content": user_text},
        ]
        rag_block = ""
        try:
            hits = rag_service.search(query=user_text)["hits"]
            if hits:
                rag_block = "\n\n知识库检索结果：\n" + "\n---\n".join(f"[{h['title']}] {h['text']}" for h in hits)
        except Exception:
            pass
        raw, usage, _used = ModelGateway(db).complete(direct_system + rag_block, msgs)
        reasoning_total = usage.get("reasoning_content", "")
        reply = "" if "[mock-llm]" in raw else raw
        if not reply:
            # 网关不可用：诚实说明，不要拿无关知识库内容糊弄通用问题
            reply = "模型服务当前不可用（网关返回错误或限流）。你的问题我已收到，请稍后重试；若问题与知识库相关，可以直接在「知识库」页检索。"


    _append(db, session, user_text, reply, delegations, trace_id, reasoning_total)
    audit(db, "orchestrator.turn", target=f"orch:{session.id}",
          detail={"delegated": bool(sub_results), "agents": [d["agent"] for d in delegations]})
    return {"reply": reply, "delegations": delegations, "trace_id": trace_id, "reasoning": reasoning_total}


def _append(db: Session, session: OrchestratorSession, user_text: str, reply: str,
            delegations: list[dict], trace_id: str, reasoning: str = "") -> None:
    session.history = [*session.history, {
        "role": "user", "content": user_text,
    }, {
        "role": "assistant", "content": reply, "delegations": delegations,
        "reasoning": reasoning,  # 思考链单独存档，不混入正文
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
