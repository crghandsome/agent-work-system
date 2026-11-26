"""三向实时同步服务：本地目录（skills/ plugins/ agents/）↔ 数据库（MySQL）↔ 前端。

工作原理（双快照差量对账）：
- 维护每个域（skills / plugins / agents）上次同步后的磁盘指纹（last_disk）与数据库指纹（last_db）。
- 当本地文件夹发生增/改/删：
  - 新增目录 / 文件内容变更 → 自动写入/更新 DB 索引；
  - 目录被删除（上次存在、本次消失）→ 自动从 DB 删除对应记录。
- 当数据库被直接增/改/删（例如外部 SQL 操作）：
  - DB 新增行 / 字段内容变更（且磁盘未动）→ 自动回写本地目录文件；
  - DB 删除行（上次存在、本次消失，且磁盘未动）→ 自动删除本地对应目录。
- 当通过 Web API 正常增/改/删时：
  - API 同时更新 DB 与本地目录，并调用 mark_synced() 刷新双快照，确保两端立即可见且不冲突。
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from pathlib import Path

from sqlalchemy.orm import Session

from ..models import Agent, Plugin, Skill, SkillReview, new_id

_LOCK = threading.RLock()
_STATE: dict[str, dict] = {
    "skills": {"init": False, "disk": {}, "db": {}, "ts": 0.0},
    "plugins": {"init": False, "disk": {}, "db": {}, "ts": 0.0},
    "agents": {"init": False, "disk": {}, "db": {}, "ts": 0.0},
}
_MIN_INTERVAL = 1.0  # 秒：高频读取节流，避免重复 stat


def _hash_obj(obj) -> str:
    raw = json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _dir_stat_sig(d: Path) -> str:
    """快速目录内容签名（按相对路径 + 大小 + mtime_ns），毫秒级检测文件增删改。"""
    parts = []
    for f in sorted(d.rglob("*")):
        if f.is_file():
            st = f.stat()
            parts.append(f"{f.relative_to(d).as_posix()}:{st.st_size}:{st.st_mtime_ns}")
    return _hash_obj(parts)


# ═══════════════════════════════════════════════════════
# 1. Skills 双向同步
# ═══════════════════════════════════════════════════════

def _find_skill_md(d: Path) -> Path | None:
    if (d / "SKILL.md").is_file():
        return d / "SKILL.md"
    for sub in sorted(d.iterdir()):
        if sub.is_dir() and (sub / "SKILL.md").is_file():
            return sub / "SKILL.md"
    return None


def _scan_skills_sig() -> dict[str, str]:
    from .artifact_store import skills_root

    out: dict[str, str] = {}
    root = skills_root()
    if not root.exists():
        return out
    for d in sorted(root.iterdir()):
        if d.is_dir() and _find_skill_md(d):
            out[d.name] = _dir_stat_sig(d)
    return out


def _read_skill_disk_info(dir_name: str) -> dict | None:
    from .artifact_store import list_skill_files, skill_digest, skill_dir

    d = skill_dir(dir_name)
    if not d.is_dir():
        return None
    md = _find_skill_md(d)
    if not md:
        return None
    raw = md.read_text(encoding="utf-8", errors="replace")
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", raw, re.S)
    fm = m.group(1) if m else ""
    dm = re.search(r"^description:\s*(.+)$", fm, re.M)
    desc = dm.group(1).strip().strip("\"'") if dm else ""
    return {
        "name": dir_name,
        "description": desc,
        "instructions": raw.strip(),
        "sha256": skill_digest(dir_name),
        "files": list_skill_files(dir_name),
    }


def _db_skills_sig(db: Session) -> dict[str, str]:
    rows = db.query(Skill).all()
    return {
        s.name: _hash_obj([s.description, s.instructions, s.category, s.status, s.artifact_sha256])
        for s in rows
    }


def _upsert_skill_from_disk(db: Session, dir_name: str) -> bool:
    from .skill_catalog import make_summary, skill_category

    info = _read_skill_disk_info(dir_name)
    if not info:
        return False
    s = db.query(Skill).filter(Skill.name == dir_name).first()
    if s is None:
        s = Skill(id=new_id("skill"), name=dir_name, source="local_dir")
        db.add(s)
    s.description = info["description"] or s.description or ""
    s.instructions = info["instructions"] or s.instructions or ""
    s.artifact_path = f"skills/{dir_name}"
    s.artifact_sha256 = info["sha256"]
    s.files = info["files"]
    s.status = "published"
    s.category = skill_category(dir_name, s.description, s.instructions)
    s.summary = make_summary(s.description, s.instructions)
    return True


def sync_skills(db: Session, force: bool = False) -> bool:
    """双向同步 skills/ 目录与 Skill 表。返回是否发生变更。"""
    from .artifact_store import build_skill_dir, delete_skill_dir

    with _LOCK:
        st = _STATE["skills"]
        now = time.time()
        if not force and st["init"] and (now - st["ts"]) < _MIN_INTERVAL:
            return False

        cur_disk = _scan_skills_sig()
        cur_db = _db_skills_sig(db)
        if not st["init"]:
            # 首次同步：以磁盘已有目录为准补齐 DB，同时记录双快照基线
            changed = False
            for name in cur_disk:
                if name not in cur_db:
                    _upsert_skill_from_disk(db, name)
                    changed = True
            if changed:
                db.commit()
                cur_db = _db_skills_sig(db)
            st.update(init=True, disk=cur_disk, db=cur_db, ts=time.time())
            return changed

        last_disk, last_db = st["disk"], st["db"]
        if cur_disk == last_disk and cur_db == last_db:
            st["ts"] = now
            return False

        changed = False
        all_names = set(cur_disk) | set(cur_db) | set(last_disk) | set(last_db)
        for name in all_names:
            in_disk, was_disk = name in cur_disk, name in last_disk
            in_db, was_db = name in cur_db, name in last_db
            disk_mod = in_disk and (cur_disk[name] != last_disk.get(name))
            db_mod = in_db and (cur_db[name] != last_db.get(name))

            if in_disk and (not was_disk or disk_mod):
                # 磁盘新增或修改 → 同步进 DB
                _upsert_skill_from_disk(db, name)
                changed = True
            elif was_disk and not in_disk and in_db:
                # 磁盘被删除 → 同步从 DB 删除
                db.query(SkillReview).filter(SkillReview.skill_name == name).delete()
                db.query(Skill).filter(Skill.name == name).delete()
                changed = True
            elif was_db and not in_db and in_disk:
                # DB 被直接删除（且磁盘未动）→ 同步删除本地目录
                delete_skill_dir(name)
                changed = True
            elif in_db and (not was_db or db_mod) and not disk_mod:
                # DB 被直接新增或修改（且磁盘未动）→ 同步写回本地目录
                s = db.query(Skill).filter(Skill.name == name).first()
                if s:
                    art = build_skill_dir(s.name, s.description or "", s.instructions or "")
                    s.artifact_path = art["path"]
                    s.artifact_sha256 = art["sha256"]
                    s.files = art["files"]
                    changed = True

        if changed:
            db.commit()
        st.update(init=True, disk=_scan_skills_sig(), db=_db_skills_sig(db), ts=time.time())
        return changed


# ═══════════════════════════════════════════════════════
# 2. Plugins 双向同步
# ═══════════════════════════════════════════════════════

def _scan_plugins_sig() -> dict[str, str]:
    from .artifact_store import plugins_root

    out: dict[str, str] = {}
    root = plugins_root()
    if not root.exists():
        return out
    for d in sorted(root.iterdir()):
        if d.is_dir() and (d / "plugin.json").is_file():
            out[d.name] = _dir_stat_sig(d)
    return out


def _db_plugins_sig(db: Session) -> dict[str, str]:
    from .plugin_service import manifest_of

    return {p.key: _hash_obj(manifest_of(p)) for p in db.query(Plugin).all()}


def _upsert_plugin_from_disk(db: Session, dir_name: str) -> bool:
    from .artifact_store import read_plugin_manifest

    mf = read_plugin_manifest(dir_name)
    if not mf:
        return False
    key = mf.get("key") or dir_name
    p = db.query(Plugin).filter(Plugin.key == key).first()
    if p is None:
        p = Plugin(id=new_id("plugin"), key=key)
        db.add(p)
    p.name = mf.get("name") or key
    p.description = mf.get("description") or ""
    p.mechanism = mf.get("mechanism") or ""
    p.extension_point = mf.get("extension_point") or "WorkflowNode"
    p.slot_semantics = mf.get("slot_semantics") or "single"
    p.version_str = mf.get("version") or "1.0.0"
    p.author = mf.get("author") or ""
    p.source = mf.get("source") or "local"
    p.source_url = mf.get("source_url") or ""
    p.config = mf.get("config") or {}
    p.config_schema = mf.get("config_schema") or {}
    p.builtin = bool(mf.get("builtin", False))
    p.enabled = bool(mf.get("enabled", True))
    p.install_path = f"plugins/{key}"
    return True


def sync_plugins(db: Session, force: bool = False) -> bool:
    """双向同步 plugins/ 目录与 Plugin 表。"""
    from .artifact_store import delete_plugin_dir
    from .plugin_service import sync_plugin_to_disk

    with _LOCK:
        st = _STATE["plugins"]
        now = time.time()
        if not force and st["init"] and (now - st["ts"]) < _MIN_INTERVAL:
            return False

        cur_disk = _scan_plugins_sig()
        cur_db = _db_plugins_sig(db)
        if not st["init"]:
            changed = False
            for key in cur_disk:
                if key not in cur_db:
                    _upsert_plugin_from_disk(db, key)
                    changed = True
            if changed:
                db.commit()
                cur_db = _db_plugins_sig(db)
            st.update(init=True, disk=cur_disk, db=cur_db, ts=time.time())
            return changed

        last_disk, last_db = st["disk"], st["db"]
        if cur_disk == last_disk and cur_db == last_db:
            st["ts"] = now
            return False

        changed = False
        all_keys = set(cur_disk) | set(cur_db) | set(last_disk) | set(last_db)
        for key in all_keys:
            in_disk, was_disk = key in cur_disk, key in last_disk
            in_db, was_db = key in cur_db, key in last_db
            disk_mod = in_disk and (cur_disk[key] != last_disk.get(key))
            db_mod = in_db and (cur_db[key] != last_db.get(key))

            if in_disk and (not was_disk or disk_mod):
                _upsert_plugin_from_disk(db, key)
                changed = True
            elif was_disk and not in_disk and in_db:
                db.query(Plugin).filter(Plugin.key == key).delete()
                changed = True
            elif was_db and not in_db and in_disk:
                delete_plugin_dir(key)
                changed = True
            elif in_db and (not was_db or db_mod) and not disk_mod:
                p = db.query(Plugin).filter(Plugin.key == key).first()
                if p:
                    sync_plugin_to_disk(db, p)
                    changed = True

        if changed:
            db.commit()
        st.update(init=True, disk=_scan_plugins_sig(), db=_db_plugins_sig(db), ts=time.time())
        return changed


# ═══════════════════════════════════════════════════════
# 3. Agents 双向同步
# ═══════════════════════════════════════════════════════

def _scan_agents_sig() -> dict[str, str]:
    from .agent_store import agents_root

    out: dict[str, str] = {}
    root = agents_root()
    if not root.exists():
        return out
    for d in sorted(root.iterdir()):
        if d.name == "teams" or not d.is_dir():
            continue
        if (d / "agent.json").is_file():
            out[d.name] = _dir_stat_sig(d)
    return out


def _db_agents_sig(db: Session) -> dict[str, str]:
    from .agent_store import manifest_of

    # 同名多版本取最新版本代表该目录
    latest: dict[str, Agent] = {}
    for a in db.query(Agent).order_by(Agent.version.desc()).all():
        latest.setdefault(a.name, a)
    return {name: _hash_obj(manifest_of(a)) for name, a in latest.items()}


def _upsert_agent_from_disk(db: Session, dir_name: str) -> bool:
    from .agent_store import read_agent_manifest

    mf = read_agent_manifest(dir_name)
    if not mf:
        return False
    name = mf.get("name") or dir_name
    ver = mf.get("version", 1)
    a = db.query(Agent).filter(Agent.name == name, Agent.version == ver).first()
    if a is None:
        a = db.query(Agent).filter(Agent.name == name).order_by(Agent.version.desc()).first()
    if a is None:
        a = Agent(id=mf.get("id") or new_id("agent"), name=name, version=ver)
        db.add(a)
    for k in ("description", "system_prompt", "model", "skill_keys", "plugin_keys",
              "plugin_config", "workflow", "eval_set", "forked_from",
              "is_orchestrator", "sub_agents"):
        if k in mf:
            setattr(a, k, mf[k])
    a.status = "published"
    return True


def sync_agents(db: Session, force: bool = False) -> bool:
    """双向同步 agents/ 目录与 Agent 表。"""
    from .agent_store import delete_agent_dir, save_agent_dir

    with _LOCK:
        st = _STATE["agents"]
        now = time.time()
        if not force and st["init"] and (now - st["ts"]) < _MIN_INTERVAL:
            return False

        cur_disk = _scan_agents_sig()
        cur_db = _db_agents_sig(db)
        if not st["init"]:
            changed = False
            for name in cur_disk:
                if name not in cur_db:
                    _upsert_agent_from_disk(db, name)
                    changed = True
            if changed:
                db.commit()
                cur_db = _db_agents_sig(db)
            st.update(init=True, disk=cur_disk, db=cur_db, ts=time.time())
            return changed

        last_disk, last_db = st["disk"], st["db"]
        if cur_disk == last_disk and cur_db == last_db:
            st["ts"] = now
            return False

        changed = False
        all_names = set(cur_disk) | set(cur_db) | set(last_disk) | set(last_db)
        for name in all_names:
            in_disk, was_disk = name in cur_disk, name in last_disk
            in_db, was_db = name in cur_db, name in last_db
            disk_mod = in_disk and (cur_disk[name] != last_disk.get(name))
            db_mod = in_db and (cur_db[name] != last_db.get(name))

            if in_disk and (not was_disk or disk_mod):
                _upsert_agent_from_disk(db, name)
                changed = True
            elif was_disk and not in_disk and in_db:
                db.query(Agent).filter(Agent.name == name).delete()
                changed = True
            elif was_db and not in_db and in_disk:
                delete_agent_dir(name)
                changed = True
            elif in_db and (not was_db or db_mod) and not disk_mod:
                a = db.query(Agent).filter(Agent.name == name).order_by(Agent.version.desc()).first()
                if a:
                    save_agent_dir(a)
                    changed = True

        if changed:
            db.commit()
        st.update(init=True, disk=_scan_agents_sig(), db=_db_agents_sig(db), ts=time.time())
        return changed


def mark_synced(domain: str, db: Session) -> None:
    """API 写入（增/改/删）完成后调用，刷新双快照基线，防止下一次同步误判冲突。"""
    with _LOCK:
        st = _STATE.get(domain)
        if not st:
            return
        if domain == "skills":
            st.update(init=True, disk=_scan_skills_sig(), db=_db_skills_sig(db), ts=time.time())
        elif domain == "plugins":
            st.update(init=True, disk=_scan_plugins_sig(), db=_db_plugins_sig(db), ts=time.time())
        elif domain == "agents":
            st.update(init=True, disk=_scan_agents_sig(), db=_db_agents_sig(db), ts=time.time())


def init_all_baselines(db: Session) -> None:
    """启动完成所有初始化后记录基线快照。"""
    mark_synced("skills", db)
    mark_synced("plugins", db)
    mark_synced("agents", db)


def reset_state_for_tests() -> None:
    """仅供测试重置内存快照。"""
    with _LOCK:
        for k in _STATE:
            _STATE[k] = {"init": False, "disk": {}, "db": {}, "ts": 0.0}
