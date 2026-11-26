"""Agent 团队协调服务。

设计（对应用户需求 2）：
- 用户可自由组建 Agent 团队：leader（主理 Agent）+ members（成员）
- 主 Agent 需要团队协作时，只与 leader 通信（一次 A2A 调用）
- leader 作为主理者，内部协调 members：按团队定义顺序调用各成员，收集结果，
  最终由 leader 汇总成一份结果返回给主 Agent
- 主 Agent 从不直接联系 members —— 它们对主 Agent 不可见
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from . import a2a_service
from .agent_store import read_team
from .audit_service import audit


def find_team_for_request(db: Session, text: str, explicit_team: str = "") -> dict | None:
    """根据请求中的团队名或描述匹配团队。简单实现：显式名优先，否则从描述中找团队名。"""
    from .agent_store import list_teams

    teams = list_teams()
    if explicit_team:
        return next((t for t in teams if t["name"] == explicit_team), None)
    t = (text or "").lower()
    for team in teams:
        if team["name"] in t or (team.get("description") and team["description"][:8] in t):
            return team
    return None


def delegate_to_team(db: Session, team: dict, user_text: str, trace_id: str, on_event=None) -> dict:
    """主 Agent → 主理 Agent 单次 A2A 调用；主理内部协调成员并汇总。

    on_event: 转发主理与各成员的实时思考事件（带 sub_agent 标识）。
    """
    leader = team["leader"]
    members = team.get("members") or []
    all_files: list[dict] = []

    # 1. 主 Agent 只向 leader 发起一个 A2A 任务
    task, created = a2a_service.get_or_create_task(
        db, idempotency_key=f"team:{team['name']}:{trace_id}", direction="internal",
        agent_name=leader, trace_id=trace_id,
        message=f"[团队主理任务] 团队「{team['name']}」成员：{', '.join(members)}。\n用户请求：{user_text}",
    )
    if created and task.state == "submitted":
        task = a2a_service.run_internal_task(db, task, on_event=on_event)
    if task.files:
        all_files.extend(task.files)

    # 2. 主理 Agent 协调成员：顺序调用每个 member（各自一次 A2A 任务），收集结果
    member_results = []
    for m in members:
        mt, mc = a2a_service.get_or_create_task(
            db, idempotency_key=f"team:{team['name']}:{trace_id}:member:{m}",
            direction="internal", agent_name=m, trace_id=trace_id,
            parent_task_id=task.id, message=user_text,
        )
        if mc and mt.state == "submitted":
            mt = a2a_service.run_internal_task(db, mt, on_event=on_event)
        if mt.files:
            all_files.extend(mt.files)
        member_results.append({"agent": m, "state": mt.state,
                               "result": mt.result or mt.error or ""})

    # 3. 汇总：leader 结果 + 各成员结果（主 Agent 只拿到这一份）
    parts = [f"团队「{team['name']}」由主理 Agent「{leader}」协调完成。"]
    if task.result:
        parts.append(f"主理结论：{task.result}")
    if member_results:
        parts.append("成员输出：")
        for r in member_results:
            parts.append(f"- {r['agent']}：{r['result'][:300]}")
    reply = "\n".join(parts)
    audit(db, "agent.team.delegate", target=f"team:{team['name']}",
          detail={"leader": leader, "members": members, "task_id": task.id})
    db.commit()
    return {
        "reply": reply,
        "delegations": [],
        "trace_id": trace_id,
        "team": team["name"], "leader": leader, "leader_task_id": task.id,
        "member_results": member_results, "files": all_files,
    }
