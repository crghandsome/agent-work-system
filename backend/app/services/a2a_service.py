"""A2A 通信（设计文档 5）：Agent Card / 内部任务 / 外部 Peer / 幂等 / trace / 委托链。

- 内部：A2A Task → 目标 Agent 的 AgentRun；委托深度限制 + 循环检测
- 外部入站：Peer 调用本系统 Agent（受 enabled Peer 白名单约束，结果经出站脱敏）
- 外部出站：调用登记 Peer 的 /a2a/tasks（若 Peer 未实现则任务标记 failed 并记录）
- 幂等键唯一约束：同键重复请求返回既有任务
"""
from __future__ import annotations

import uuid

import httpx
from sqlalchemy.orm import Session

from sqlalchemy.exc import IntegrityError

from ..config import get_settings
from ..models import A2APeer, A2ATask, Agent, AgentRun, new_id
from . import run_service
from .audit_service import audit, record_usage

MAX_DEPTH = 5
SENSITIVE_KEY_PATTERNS = ("sk-", "Bearer ", "password", "token", "secret")


def new_trace_id() -> str:
    return uuid.uuid4().hex


def get_or_create_task(db: Session, *, idempotency_key: str, direction: str, agent_name: str = "",
                       peer_name: str = "", parent_task_id: str = "", depth: int = 0,
                       trace_id: str = "", message: str = "") -> tuple[A2ATask, bool]:
    """幂等创建：同键返回既有任务 (task, False)。"""
    existing = db.query(A2ATask).filter(A2ATask.idempotency_key == idempotency_key).first()
    if existing:
        return existing, False
    task = A2ATask(
        id=new_id("a2a"),
        idempotency_key=idempotency_key,
        direction=direction,
        agent_name=agent_name,
        peer_name=peer_name,
        parent_task_id=parent_task_id,
        depth=depth,
        trace_id=trace_id or new_trace_id(),
        root_task_id=parent_task_id.split(":")[0] if parent_task_id else "",
        message=message,
        state="submitted",
    )
    db.add(task)
    try:
        db.flush()
    except IntegrityError:
        # 并发同键：回滚本次插入，返回既有任务（幂等语义）
        db.rollback()
        existing = (
            db.query(A2ATask).filter(A2ATask.idempotency_key == idempotency_key).first()
        )
        if existing is None:  # rollback 后重查
            db.expire_all()
            existing = db.query(A2ATask).filter(A2ATask.idempotency_key == idempotency_key).first()
        if existing is None:
            raise
        return existing, False
    return task, True


def _check_delegation_chain(db: Session, task: A2ATask) -> None:
    """委托深度限制 + 循环检测（5.2）。"""
    if task.depth > MAX_DEPTH:
        task.state = "failed"
        task.error = f"委托深度超过上限 {MAX_DEPTH}"
        return
    seen = set()
    cur = task
    while cur and cur.parent_task_id:
        parent_id = cur.parent_task_id.split(":")[0]
        if parent_id in seen:
            task.state = "failed"
            task.error = "检测到循环委托"
            return
        seen.add(parent_id)
        cur = db.query(A2ATask).filter(A2ATask.id == parent_id).first()


def _sanitize_outbound(text: str, policy: dict) -> str:
    """出站脱敏（5.3）：剥离密钥形态字符串与策略声明的敏感词。"""
    import re

    cleaned = text
    for pat in SENSITIVE_KEY_PATTERNS:
        cleaned = cleaned.replace(pat, "[REDACTED]")
    cleaned = re.sub(r"sk-[A-Za-z0-9]{16,}", "[REDACTED-KEY]", cleaned)
    for key in policy.get("strip_words", []):
        cleaned = cleaned.replace(str(key), "[REDACTED]")
    return cleaned


def run_internal_task(db: Session, task: A2ATask) -> A2ATask:
    """内部 A2A：Task → 目标 Agent 的 Run。"""
    task.state = "working"
    db.flush()
    _check_delegation_chain(db, task)
    if task.state == "failed":
        return task

    agent = (
        db.query(Agent)
        .filter(Agent.name == task.agent_name, Agent.status == "published")
        .order_by(Agent.version.desc())
        .first()
    )
    if agent is None:
        task.state = "failed"
        task.error = f"目标 Agent '{task.agent_name}' 不存在或未发布"
        return task

    # 内置设计子 Agent：走专用设计流程（产出 Skill / 插件制品），而非通用 LLM 运行
    if task.agent_name in ("skill-designer", "plugin-designer"):
        from . import plugin_designer_service, skill_designer_service

        if task.agent_name == "plugin-designer":
            out = plugin_designer_service.design(db, task.message)
        else:
            out = skill_designer_service.design(db, task.message)
        task.state = "completed" if out.get("ok") else "failed"
        task.result = out.get("reply", "")
        if not out.get("ok"):
            task.error = out.get("reply", "")
        record_usage(db, f"agent:{agent.name}@{agent.version}", "a2a_call")
        audit(db, "a2a.task", target=f"a2a:{task.id}",
              detail={"direction": task.direction, "state": task.state, "special": task.agent_name})
        db.commit()
        return task

    run = run_service.run_agent(db, agent, task.message, use_rag=True)
    task.run_id = run.id
    if run.status == "succeeded":
        task.state = "completed"
        task.result = run.output_text
    else:
        task.state = "failed"
        task.error = run.error
    record_usage(db, f"agent:{agent.name}@{agent.version}", "a2a_call")
    audit(db, "a2a.task", target=f"a2a:{task.id}", detail={"direction": task.direction, "state": task.state})
    return task


def run_inbound_task(db: Session, task: A2ATask) -> A2ATask:
    """外部入站：Peer 调用本系统 Agent；结果经出站脱敏。"""
    task = run_internal_task(db, task)
    if task.state == "completed":
        peer = db.query(A2APeer).filter(A2APeer.name == task.peer_name).first()
        policy = peer.data_policy if peer else {}
        task.result = _sanitize_outbound(task.result, policy)
        audit(db, "a2a.outbound_sanitize", target=f"a2a:{task.id}", detail={"peer": task.peer_name})
    return task


def run_outbound_task(db: Session, task: A2ATask, peer: A2APeer) -> A2ATask:
    """外部出站：调用登记 Peer 的 A2A 端点（出口白名单——只允许 peer.base_url）。"""
    task.state = "working"
    db.flush()
    try:
        headers = {"content-type": "application/json"}
        if peer.auth_type == "bearer" and peer.auth_token:
            headers["Authorization"] = f"Bearer {peer.auth_token}"
        payload = {
            "idempotency_key": task.idempotency_key,
            "message": task.message,
            "trace_id": task.trace_id,
            "parent_task_id": task.parent_task_id,
            "depth": task.depth,
        }
        with httpx.Client(timeout=60) as client:
            resp = client.post(f"{peer.base_url.rstrip('/')}/a2a/tasks", headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
        task.state = data.get("state", "completed")
        task.result = data.get("result", "")
        task.error = data.get("error", "")
    except Exception as exc:  # noqa: BLE001
        task.state = "failed"
        task.error = f"Peer 调用失败: {exc}"
    record_usage(db, f"peer:{peer.name}", "a2a_call")
    audit(db, "a2a.task", target=f"a2a:{task.id}", detail={"peer": peer.name, "state": task.state})
    return task


def agent_card(agent: Agent, base_url: str, local_url: str = "") -> dict:
    """A2A Agent Card（发现模型）。

    URL 只对外暴露：用 base_url（AWS_A2A_PUBLIC_BASE_URL）生成。
    内部 Agent 不需要网络地址——内部 A2A 走进程内任务调度，不依赖 URL。
    """
    url = base_url.rstrip("/") + "/a2a/tasks" if base_url else ""
    is_main = bool(getattr(agent, "is_orchestrator", False))
    return {
        "name": agent.name,
        "version": str(agent.version),
        "description": agent.description,
        "protocol": "a2a/1.0",
        "exposure": "public" if is_main else "internal",
        "url": url,
        "url_hint": (
            None if url else
            "未配置 AWS_A2A_PUBLIC_BASE_URL；内部 Agent 的 A2A 不依赖 URL，配置外部地址后才会生成对外端点。"
        ),
        "skills": list(agent.skill_keys),
        "extension_points": sorted(agent.plugin_keys.keys()),
        "capabilities": {"streaming": False, "pushNotifications": False},
    }


def cancel_task(db: Session, task_id: str) -> A2ATask:
    task = db.query(A2ATask).filter(A2ATask.id == task_id).first()
    if task and task.state in ("submitted", "working"):
        task.state = "canceled"
        audit(db, "a2a.cancel", target=f"a2a:{task.id}")
    return task
