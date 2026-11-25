"""Agent 存储：文件优先，落本地 agents/ 目录。

每个 Agent 一个目录：agents/<name>/agent.json，包含全部定义字段。
DB 只存可重建索引；启动时从目录扫描重建。
团队（AgentTeam）同样落 agents/teams/<name>/team.json。
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from sqlalchemy.orm import Session

from ..models import Agent, new_id


def _score_match(query: str, target: str) -> int:
    """简单关键词匹配打分：按描述里出现的词/短语给推荐排序。"""
    import re

    q = (query or "").lower()
    t = (target or "").lower()
    if not q:
        return 0
    score = 0
    # 中文按 2-3 字片段切，英文按单词切
    terms = set(re.findall(r"[a-z0-9]{2,}", q)) | {
        q[i:i+2] for i in range(len(q) - 1) if "\u4e00" <= q[i] <= "\u9fff"
    }
    for term in terms:
        if term and term in t:
            score += 1
    return score


def agents_root() -> Path:
    root = Path(os.environ.get("AWS_AGENTS_DIR", "/srv/data/artifacts/agents"))
    root.mkdir(parents=True, exist_ok=True)
    return root


def agent_dir(name: str) -> Path:
    return agents_root() / name


def manifest_of(a: Agent) -> dict:
    return {
        "id": a.id, "name": a.name, "version": a.version,
        "description": a.description, "system_prompt": a.system_prompt,
        "model": a.model, "skill_keys": a.skill_keys or [],
        "plugin_keys": a.plugin_keys or {}, "plugin_config": a.plugin_config or {},
        "workflow": a.workflow or {}, "eval_set": a.eval_set or [],
        "forked_from": a.forked_from or "", "is_orchestrator": bool(a.is_orchestrator),
        "sub_agents": a.sub_agents or [],
    }


def save_agent_dir(a: Agent) -> str:
    d = agent_dir(a.name)
    d.mkdir(parents=True, exist_ok=True)
    (d / "agent.json").write_text(
        json.dumps(manifest_of(a), ensure_ascii=False, indent=2), encoding="utf-8")
    return f"agents/{a.name}"


def read_agent_manifest(name: str) -> dict | None:
    p = agent_dir(name) / "agent.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def delete_agent_dir(name: str) -> bool:
    # 防御：空名或非法名会指向 agents/ 根目录（挂载点），严禁删除
    if not name or name in (".", "..") or "/" in name or "\\" in name:
        return False
    d = agent_dir(name)
    if not d.exists() or not d.is_dir():
        return False
    shutil.rmtree(d)
    return True


def scan_agent_dirs() -> list[dict]:
    out = []
    for d in sorted(agents_root().iterdir()):
        if d.name == "teams" or not d.is_dir():
            continue
        mf = read_agent_manifest(d.name)
        if mf:
            out.append(mf)
    return out


def rebuild_agents_from_disk(db: Session) -> int:
    created = 0
    for mf in scan_agent_dirs():
        name = mf.get("name")
        if not name:
            continue
        a = db.query(Agent).filter(Agent.name == name, Agent.version == mf.get("version", 1)).first()
        if a is None:
            a = Agent(id=mf.get("id") or new_id("agent"), name=name, version=mf.get("version", 1))
            db.add(a)
            created += 1
        for k in ("description", "system_prompt", "model", "skill_keys", "plugin_keys",
                  "plugin_config", "workflow", "eval_set", "forked_from",
                  "is_orchestrator", "sub_agents"):
            if k in mf:
                setattr(a, k, mf[k])
        a.status = "published"
    db.commit()
    return created


# ── Agent 团队 ──

def teams_root() -> Path:
    d = agents_root() / "teams"
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_team(team: dict) -> str:
    d = teams_root() / team["name"]
    d.mkdir(parents=True, exist_ok=True)
    (d / "team.json").write_text(json.dumps(team, ensure_ascii=False, indent=2), encoding="utf-8")
    return f"agents/teams/{team['name']}"


def read_team(name: str) -> dict | None:
    p = teams_root() / name / "team.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def list_teams() -> list[dict]:
    out = []
    for d in sorted(teams_root().iterdir()):
        if not d.is_dir():
            continue
        t = read_team(d.name)
        if t:
            out.append(t)
    return out


def delete_team(name: str) -> bool:
    d = teams_root() / name
    if not d.exists():
        return False
    shutil.rmtree(d)
    return True
