"""全量 API 路由：Skill（三入口+审核）、A2A、市场（评测/Fork/Playground）、Peers、计量审计。"""
from __future__ import annotations

import json
import os
import re

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..models import A2APeer, A2ATask, Agent, AgentRun, EvalRun, MCPServer, Plugin, Skill, SkillDialog, SkillReview, new_id
from ..schemas import AgentCreate, AgentOut, RunCreate, RunOut, SkillOut
from ..services import a2a_service, agent_service, market_service, run_service, skill_pipeline
from ..services.audit_service import audit, get_usage
from ..services.skill_dialog_service import reply as dialog_reply, start_dialog, submit as dialog_submit

router = APIRouter()


# ══ Skill：ZIP 上传 + 对话创作 + 审核记录 ══

@router.post("/skills/upload")
async def upload_skill_zip(file: UploadFile = File(...), db: Session = Depends(get_db)):
    if not file.filename or not file.filename.lower().endswith(".zip"):
        raise HTTPException(422, {"rule": "zip-type", "message": "只接受 .zip 文件", "suggestion": "重命名或重新打包"})
    data = await file.read()
    try:
        skill, warnings = skill_pipeline.publish_skill_zip(db, data)
    except skill_pipeline.SkillValidationError as exc:
        raise HTTPException(422, {"rule": exc.rule, "message": exc.message, "suggestion": exc.suggestion})
    db.commit()
    from ..services import asset_sync
    asset_sync.mark_synced("skills", db)
    return {"id": skill.id, "name": skill.name, "warnings": warnings}


class TemplateSkillIn(BaseModel):
    name: str
    description: str
    instructions: str


@router.post("/skills/template")
def create_skill_template(body: TemplateSkillIn, db: Session = Depends(get_db)):
    """入口2：模板表单创建（走与 ZIP 相同的审核流水线）。"""
    try:
        skill, warnings = skill_pipeline.publish_skill_manual(
            db, name=body.name, description=body.description,
            instructions=body.instructions, source="template",
        )
    except skill_pipeline.SkillValidationError as exc:
        raise HTTPException(422, {"rule": exc.rule, "message": exc.message, "suggestion": exc.suggestion})
    db.commit()
    from ..services import asset_sync
    asset_sync.mark_synced("skills", db)
    return {"id": skill.id, "name": skill.name, "warnings": warnings}


@router.get("/skills/categories")
def skill_categories(db: Session = Depends(get_db)):
    """分类导航：按分类聚合已发布 Skill（分类由 description 自动归类）。"""
    from ..services import asset_sync
    from ..services.skill_catalog import CATEGORY_META, CATEGORY_RULES

    asset_sync.sync_skills(db)
    rows = db.query(Skill).filter(Skill.status == "published").all()
    buckets: dict[str, list] = {}
    for s in rows:
        buckets.setdefault(s.category or "其他", []).append({
            "name": s.name, "description": s.description, "summary": s.summary,
            "source": s.source, "files_count": len(s.files or []),
        })
    out = []
    order = [c for c, _ in CATEGORY_RULES]
    for cat in order:
        if cat in buckets:
            out.append({"name": cat, "color": CATEGORY_META.get(cat, "灰"), "count": len(buckets[cat]),
                        "skills": sorted(buckets.pop(cat), key=lambda x: x["name"])})
    for cat, items in sorted(buckets.items()):
        out.append({"name": cat, "color": CATEGORY_META.get(cat, "灰"), "count": len(items),
                    "skills": sorted(items, key=lambda x: x["name"])})
    return out


@router.get("/skills/{name}/detail")
def skill_detail(name: str, db: Session = Depends(get_db)):
    """Skill 详情：文件结构树 + 元信息 + SKILL.md 预览。"""
    from ..services import asset_sync
    from ..services.artifact_store import load_skill_bundle
    from ..services.skill_catalog import file_tree, strip_frontmatter

    asset_sync.sync_skills(db)
    s = db.query(Skill).filter(Skill.name == name).first()
    if not s:
        raise HTTPException(404, "Skill 不存在")
    fm, body = strip_frontmatter(s.instructions or "")
    bundle = None
    if s.artifact_sha256:
        bundle = load_skill_bundle(s.name, s.artifact_sha256)
    return {
        "name": s.name, "description": s.description, "summary": s.summary,
        "category": s.category, "source": s.source, "status": s.status,
        "sha256": s.artifact_sha256,
        "tree": file_tree(s.files or []),
        "frontmatter": fm, "instructions_preview": body[:2000],
        "references": sorted("references/" + k for k in (bundle or {}).get("references", {}).keys()),
        "scripts": ["scripts/" + p for p in (bundle or {}).get("scripts", [])],
        "assets": ["assets/" + p for p in (bundle or {}).get("assets", [])],
        "created_at": str(s.created_at),
    }


@router.get("/skills/{name}/file")
def skill_file(name: str, path: str, db: Session = Depends(get_db)):
    """读取 Skill 目录中单个文件（用 ?path=xxx 指定）。"""
    from ..services.artifact_store import read_skill_file, skill_dir

    s = db.query(Skill).filter(Skill.name == name).first()
    if not s or not skill_dir(name).exists():
        raise HTTPException(404, "Skill 不存在")
    content = read_skill_file(name, path)
    if content is None:
        raise HTTPException(404, f"文件不存在: {path}")
    return {"name": name, "path": path, "content": content}


class SkillFileEditIn(BaseModel):
    changes: dict[str, str] = Field(default_factory=dict, description="{文件路径: 新内容；空字符串表示删除}")
    description: str = ""


@router.put("/skills/{name}/files")
def skill_edit_files(name: str, body: SkillFileEditIn, db: Session = Depends(get_db)):
    """修改 Skill 目录内文件（文件是真相源，DB 索引同步更新）。"""
    from ..services.artifact_store import (
        delete_skill_file, list_skill_files, read_skill_file,
        skill_digest, skill_dir, write_skill_file,
    )
    from ..services.skill_catalog import make_summary, skill_category, split_frontmatter

    s = db.query(Skill).filter(Skill.name == name).first()
    if not s or not skill_dir(name).exists():
        raise HTTPException(404, "Skill 不存在")
    for rel, content in body.changes.items():
        if content == "":
            delete_skill_file(name, rel)
        else:
            write_skill_file(name, rel, content)

    raw = read_skill_file(name, "SKILL.md")
    if raw:
        fm, md_body = split_frontmatter(raw)
        import re as _re

        dm = _re.search(r"^description:\s*(.+)$", fm, _re.M)
        if body.description:
            s.description = body.description
        elif dm:
            s.description = dm.group(1).strip().strip("\"'") or s.description
        s.instructions = raw.strip()
        s.category = skill_category(s.name, s.description, md_body)
        s.summary = make_summary(s.description, md_body)
    artifact = {"path": f"skills/{name}", "sha256": skill_digest(name), "files": list_skill_files(name)}
    s.artifact_path = artifact["path"]
    s.artifact_sha256 = artifact["sha256"]
    s.files = artifact["files"]
    audit(db, "skill.files.edit", target=f"skill:{name}",
          detail={"changed": list(body.changes.keys()), "sha256": artifact["sha256"][:16]})
    db.commit()
    from ..services import asset_sync
    asset_sync.mark_synced("skills", db)
    return {"name": name, "sha256": artifact["sha256"],
            "files": [f["path"] for f in artifact["files"]], "category": s.category}


@router.delete("/skills/{name}")
def skill_delete(name: str, db: Session = Depends(get_db)):
    """删除 Skill：同步清理数据库元数据、审核记录与本地 skills/<name>/ 目录。"""
    from ..services import asset_sync
    from ..services.artifact_store import delete_skill_dir

    s = db.query(Skill).filter(Skill.name == name).first()
    if not s:
        raise HTTPException(404, "Skill 不存在")
    db.query(SkillReview).filter(SkillReview.skill_name == name).delete()
    db.delete(s)
    delete_skill_dir(name)
    audit(db, "skill.delete", target=f"skill:{name}")
    db.commit()
    asset_sync.mark_synced("skills", db)
    return {"ok": True, "name": name}


class SkillCategoryIn(BaseModel):
    category: str


@router.patch("/skills/{name}/category")
def set_skill_category(name: str, body: SkillCategoryIn, db: Session = Depends(get_db)):
    """人工覆盖分类。"""
    from ..services import asset_sync

    s = db.query(Skill).filter(Skill.name == name).first()
    if not s:
        raise HTTPException(404, "Skill 不存在")
    s.category = (body.category or "其他").strip()[:48] or "其他"
    db.commit()
    asset_sync.mark_synced("skills", db)
    return {"name": s.name, "category": s.category}


@router.post("/skills/upload-md")
async def upload_skill_md(file: UploadFile = File(...), db: Session = Depends(get_db)):
    """入口：上传单个 .md 文件作为 Skill（自动解析 frontmatter 或用文件名补全）。"""
    if not file.filename or not file.filename.lower().endswith(".md"):
        raise HTTPException(422, {"rule": "file-type", "message": "只接受 .md 文件", "suggestion": "上传 Markdown 文件"})
    raw = (await file.read()).decode("utf-8", errors="replace")
    import re
    from ..services.skill_catalog import strip_frontmatter

    fm, body = strip_frontmatter(raw)
    name_m = re.search(r"^name:\s*(.+)$", fm, re.M)
    desc_m = re.search(r"^description:\s*(.+)$", fm, re.M)
    base = file.filename.rsplit(".", 1)[0]
    name = (name_m.group(1).strip().strip("\"'") if name_m else base)
    description = (desc_m.group(1).strip().strip("\"'") if desc_m else "")
    if not description:
        description = f"由 {base}.md 导入的 Skill"
    try:
        # 保存完整原文（含 frontmatter），便于详情页展示与再编辑
        skill, warnings = skill_pipeline.publish_skill_manual(
            db, name=name, description=description, instructions=raw.strip(), source="upload_md")
    except skill_pipeline.SkillValidationError as exc:
        raise HTTPException(422, {"rule": exc.rule, "message": exc.message, "suggestion": exc.suggestion})
    db.commit()
    from ..services import asset_sync
    asset_sync.mark_synced("skills", db)
    return {"id": skill.id, "name": skill.name, "category": skill.category, "warnings": warnings}


class DialogStartOut(BaseModel):
    dialog_id: str
    question: str
    state: str


class DialogReplyIn(BaseModel):
    text: str


class DialogOut(BaseModel):
    dialog_id: str
    state: str
    draft_name: str
    draft_description: str
    draft_instructions: str
    history: list


@router.post("/skills/dialog", response_model=DialogStartOut)
def create_dialog(db: Session = Depends(get_db)):
    d = start_dialog(db)
    db.commit()  # 显式提交：避免 yield 依赖 teardown 时序竞态
    return DialogStartOut(dialog_id=d.id, question=d.history[-1]["content"], state=d.state)


@router.post("/skills/dialog/{dialog_id}/reply", response_model=DialogOut)
def reply_dialog(dialog_id: str, body: DialogReplyIn, db: Session = Depends(get_db)):
    d = db.query(SkillDialog).filter(SkillDialog.id == dialog_id).first()
    if not d:
        raise HTTPException(404, "对话不存在")
    d = dialog_reply(db, d, body.text)
    db.commit()
    return DialogOut(dialog_id=d.id, state=d.state, draft_name=d.draft_name,
                     draft_description=d.draft_description, draft_instructions=d.draft_instructions,
                     history=d.history)


@router.post("/skills/dialog/{dialog_id}/submit")
def submit_dialog(dialog_id: str, db: Session = Depends(get_db)):
    d = db.query(SkillDialog).filter(SkillDialog.id == dialog_id).first()
    if not d:
        raise HTTPException(404, "对话不存在")
    try:
        skill, warnings = dialog_submit(db, d)
    except skill_pipeline.SkillValidationError as exc:
        raise HTTPException(422, {"rule": exc.rule, "message": exc.message, "suggestion": exc.suggestion})
    db.commit()
    from ..services import asset_sync
    asset_sync.mark_synced("skills", db)
    return {"skill": skill.name, "warnings": warnings}


@router.get("/skills/{name}/reviews")
def skill_reviews(name: str, db: Session = Depends(get_db)):
    return skill_pipeline.list_reviews(db, name)


@router.get("/skills/{name}/artifact")
def skill_artifact(name: str, db: Session = Depends(get_db)):
    """制品元信息：哈希、文件清单（含 references/scripts/assets）。"""
    from ..services.artifact_store import skill_digest, skill_dir

    skill = db.query(Skill).filter(Skill.name == name).first()
    if not skill:
        raise HTTPException(404, "Skill 不存在")
    verified = skill_dir(name).exists() and skill_digest(name) == skill.artifact_sha256
    return {
        "name": name,
        "sha256": skill.artifact_sha256,
        "integrity_ok": verified,
        "path": skill.artifact_path,
        "files": skill.files,
    }


# ══ DSH 导出：把插件/Skill 导出为 DeepSeek Harness 可用形态 ══

@router.post("/dsh/export/skills/{name}")
def dsh_export_skill(name: str):
    from ..services import dsh_export_service

    try:
        return dsh_export_service.export_skill_to_dsh(name)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc))


@router.post("/dsh/export/plugins/{key}")
def dsh_export_plugin(key: str, db: Session = Depends(get_db)):
    from ..services import dsh_export_service

    try:
        return dsh_export_service.export_plugin_to_dsh(db, key)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc))


@router.post("/dsh/export/all")
def dsh_export_all(db: Session = Depends(get_db)):
    from ..services import dsh_export_service

    return dsh_export_service.export_all_to_dsh(db)


@router.get("/dsh/home")
def dsh_home_info():
    from ..services.dsh_export_service import dsh_home

    return {"dsh_home": str(dsh_home()), "exists": dsh_home().exists()}


# ══ 插件市场：安装/启停/修改/卸载（对齐 DSH 插件思想）══

class PluginInstallIn(BaseModel):
    key: str
    name: str = ""
    description: str = ""
    extension_point: str = "WorkflowNode"
    slot_semantics: str = "single"
    config_schema: dict = Field(default_factory=dict)
    source: str = "market"           # market|github|local
    source_url: str = ""
    author: str = ""


@router.post("/plugins/install")
def plugin_install(body: PluginInstallIn, db: Session = Depends(get_db)):
    """从市场/GitHub 安装插件（按清单字段）。"""
    from ..services.plugin_service import install_plugin, to_dict

    try:
        p = install_plugin(
            db, key=body.key, name=body.name, description=body.description,
            extension_point=body.extension_point, slot_semantics=body.slot_semantics,
            config_schema=body.config_schema, source=body.source, source_url=body.source_url,
            author=body.author,
        )
    except ValueError as exc:
        raise HTTPException(409 if "已安装" in str(exc) else 422, str(exc))
    from ..services import asset_sync
    asset_sync.mark_synced("plugins", db)
    return to_dict(p)


class PluginLocalIn(BaseModel):
    directory: str  # 本地插件目录（含 package.json 或 plugin.json）


@router.post("/plugins/install-local")
def plugin_install_local(body: PluginLocalIn, db: Session = Depends(get_db)):
    """从本地目录安装插件（解析 bundle 清单）。"""
    from ..services import asset_sync
    from ..services.plugin_service import install_from_manifest, parse_bundle_manifest, to_dict

    try:
        manifest = parse_bundle_manifest(body.directory)
        p = install_from_manifest(db, manifest, source="local", source_url=body.directory)
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        raise HTTPException(422, str(exc))
    asset_sync.mark_synced("plugins", db)
    return to_dict(p)


@router.get("/plugins/managed")
def plugins_managed(db: Session = Depends(get_db)):
    """插件管理列表（含简介、来源、启停、可编辑标记）。"""
    from ..services import asset_sync
    from ..services.plugin_service import to_dict

    asset_sync.sync_plugins(db)
    return [to_dict(p) for p in db.query(Plugin).order_by(Plugin.builtin.desc(), Plugin.key).all()]


class PluginToggleIn(BaseModel):
    enabled: bool


@router.put("/plugins/{key}/enabled")
def plugin_toggle(key: str, body: PluginToggleIn, db: Session = Depends(get_db)):
    """启用/停用插件。"""
    from ..services import asset_sync
    from ..services.plugin_service import set_enabled

    try:
        res = set_enabled(db, key, body.enabled)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    asset_sync.mark_synced("plugins", db)
    return res


class PluginUpdateIn(BaseModel):
    name: str = ""
    description: str = ""
    mechanism: str = ""
    config: dict | None = None
    config_schema: dict | None = None


@router.put("/plugins/{key}")
def plugin_update(key: str, body: PluginUpdateIn, db: Session = Depends(get_db)):
    """修改插件（所有插件均可修改）。"""
    from ..services import asset_sync
    from ..services.plugin_service import update_plugin

    try:
        res = update_plugin(db, key, name=body.name, description=body.description,
                            mechanism=body.mechanism, config=body.config, config_schema=body.config_schema)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    asset_sync.mark_synced("plugins", db)
    return res


@router.get("/plugins/behaviors")
def plugin_behaviors():
    """配置驱动插件的 behavior 指南（用户设计插件时参考）。"""
    from ..services.plugin_service import behavior_guide

    return behavior_guide()


@router.delete("/plugins/{key}")
def plugin_uninstall(key: str, db: Session = Depends(get_db)):
    """卸载插件（所有插件均可删除）。"""
    from ..services import asset_sync
    from ..services.plugin_service import uninstall_plugin

    ok = uninstall_plugin(db, key)
    if not ok:
        raise HTTPException(404, f"插件 '{key}' 不存在")
    asset_sync.mark_synced("plugins", db)
    return {"ok": True, "key": key}


# ══ Agent：工作流/评测集/Fork/Playground ══

class AgentCreateFull(AgentCreate):
    workflow: dict = Field(default_factory=dict)
    eval_set: list[dict] = Field(default_factory=list)


class AgentOutFull(AgentOut):
    # 历史行的 JSON 列可能为 NULL（加列时未回填），用 field_validator 归一
    workflow: dict = {}
    eval_set: list = []
    forked_from: str = ""
    is_orchestrator: bool = False
    sub_agents: list = []

    from pydantic import field_validator

    @field_validator("workflow", mode="before")
    @classmethod
    def _wf(cls, v): return v if isinstance(v, dict) else {}

    @field_validator("eval_set", "sub_agents", "skill_keys", mode="before")
    @classmethod
    def _lst(cls, v): return v if isinstance(v, list) else []

    @field_validator("plugin_keys", "plugin_config", mode="before")
    @classmethod
    def _dct(cls, v): return v if isinstance(v, dict) else {}


@router.post("/agents", response_model=AgentOutFull)
def create_agent_full(body: AgentCreateFull, db: Session = Depends(get_db)):
    try:
        agent, warnings = agent_service.create_agent(
            db, name=body.name, description=body.description, system_prompt=body.system_prompt,
            model=body.model, skill_keys=body.skill_keys, plugin_keys=body.plugin_keys,
            plugin_config=body.plugin_config, workflow=body.workflow, eval_set=body.eval_set,
            is_orchestrator=body.is_orchestrator, sub_agents=body.sub_agents,
        )
    except agent_service.AssemblyValidationError as exc:
        raise HTTPException(422, {"rule": exc.rule, "message": exc.message})
    from ..services import asset_sync
    from ..services.agent_store import save_agent_dir

    save_agent_dir(agent)  # 文件优先：落本地 agents/ 目录
    agent._warnings = warnings  # noqa: SLF001
    db.commit()
    asset_sync.mark_synced("agents", db)
    return agent


@router.put("/agents/{agent_id}")
def update_agent_full(agent_id: str, body: AgentCreateFull, db: Session = Depends(get_db)):
    """重新组装 Agent：更新定义并同步到 agents/ 目录。"""
    from ..services import asset_sync
    from ..services.agent_store import delete_agent_dir, save_agent_dir

    a = db.query(Agent).filter(Agent.id == agent_id).first()
    if not a:
        raise HTTPException(404, "Agent 不存在")
    old_name = a.name
    # 逐字段更新（版本不变，保留历史 id）
    for k in ("description", "system_prompt", "model", "skill_keys", "plugin_keys",
              "plugin_config", "workflow", "eval_set", "is_orchestrator", "sub_agents"):
        v = getattr(body, k)
        if v is not None:
            setattr(a, k, v)
    if old_name != body.name:
        delete_agent_dir(old_name)
        a.name = body.name
    save_agent_dir(a)
    audit(db, "agent.update", target=f"agent:{a.name}", detail={"fields": list(body.model_fields)})
    db.commit()
    asset_sync.mark_synced("agents", db)
    return a


@router.delete("/agents/{agent_id}")
def delete_agent_full(agent_id: str, db: Session = Depends(get_db)):
    """删除 Agent：DB 索引 + agents/ 目录。"""
    from ..services import asset_sync
    from ..services.agent_store import delete_agent_dir

    a = db.query(Agent).filter(Agent.id == agent_id).first()
    if not a:
        raise HTTPException(404, "Agent 不存在")
    name = a.name
    db.delete(a)
    delete_agent_dir(name)
    audit(db, "agent.delete", target=f"agent:{name}")
    db.commit()
    asset_sync.mark_synced("agents", db)
    return {"ok": True, "name": name}


# ── 智能推荐：根据描述推荐 Skill / 插件 ──

class RecommendIn(BaseModel):
    description: str = ""
    limit: int = 5


@router.post("/agents/recommend/skills")
def recommend_skills(body: RecommendIn, db: Session = Depends(get_db)):
    """按用户填写的描述推荐可能想用的 Skill（关键词匹配 description/category）。"""
    from ..services.agent_store import _score_match  # 见下方定义

    rows = db.query(Skill).filter(Skill.status == "published").all()
    scored = [(s, _score_match(body.description, f"{s.name} {s.description} {s.category}"))
              for s in rows]
    picked = [s for s, sc in sorted(scored, key=lambda x: -x[1]) if sc > 0][:body.limit]
    return [{"name": s.name, "description": s.description, "category": s.category, "summary": s.summary}
            for s in picked]


@router.post("/agents/recommend/plugins")
def recommend_plugins(body: RecommendIn, db: Session = Depends(get_db)):
    """按描述推荐可能想用的插件（关键词匹配 name/description/extension_point）。"""
    from ..services.agent_store import _score_match

    rows = db.query(Plugin).filter(Plugin.enabled.is_(True)).all()
    scored = [(p, _score_match(body.description, f"{p.key} {p.name} {p.description} {p.extension_point}"))
              for p in rows]
    picked = [p for p, sc in sorted(scored, key=lambda x: -x[1]) if sc > 0][:body.limit]
    return [{"key": p.key, "name": p.name, "description": p.description,
             "extension_point": p.extension_point} for p in picked]


# ── Agent 团队 ──

class TeamIn(BaseModel):
    name: str
    description: str = ""
    leader: str                       # 主理 Agent 名称
    members: list[str] = Field(default_factory=list)


@router.post("/agents/teams")
def create_team(body: TeamIn, db: Session = Depends(get_db)):
    """创建 Agent 团队：系统自动创建主理 Agent（若同名不存在），它负责协调成员。"""
    from ..services.agent_store import read_team, save_team, save_agent_dir
    from ..services.audit_service import audit as _audit

    if not re.match(r"^[a-z0-9][a-z0-9_-]{1,63}$", body.name):
        raise HTTPException(422, "团队名称只能用小写字母、数字、连字符、下划线")
    if read_team(body.name):
        raise HTTPException(409, f"团队 '{body.name}' 已存在")

    leader_name = (body.leader or "").strip() or f"{body.name}-leader"
    leader = db.query(Agent).filter(Agent.name == leader_name).first()
    auto_created = False
    if leader is None:
        # 自动创建主理 Agent：它的职责是接收主 Agent 任务、协调成员、汇总结果
        leader = Agent(
            id=new_id("agent"), name=leader_name, version=1,
            description=f"团队「{body.name}」的主理 Agent，负责协调成员工作",
            system_prompt=(f"你是团队「{body.name}」的主理 Agent。团队描述：{body.description or '无'}。"
                           "你接收主 Agent 的任务，分配给成员并汇总结果后回复。回答简洁自然。"),
            skill_keys=[], plugin_keys={}, plugin_config={}, workflow={},
            eval_set=[], is_orchestrator=False, sub_agents=[], status="published",
        )
        db.add(leader)
        db.flush()
        save_agent_dir(leader)
        auto_created = True
        from ..services import asset_sync
        asset_sync.mark_synced("agents", db)

    for m in body.members:
        if not db.query(Agent).filter(Agent.name == m).first():
            raise HTTPException(422, f"成员 Agent '{m}' 不存在")

    team = {"name": body.name, "description": body.description,
            "leader": leader_name, "members": body.members,
            "auto_created_leader": auto_created}
    path = save_team(team)
    audit(db, "agent.team.create", target=f"team:{body.name}",
          detail={"leader": leader_name, "members": body.members, "auto_created_leader": auto_created})
    db.commit()
    return {**team, "path": path}


@router.get("/agents/teams")
def list_teams():
    from ..services.agent_store import list_teams as _list

    return _list()


@router.patch("/agents/teams/{name}")
def update_team(name: str, body: TeamIn, db: Session = Depends(get_db)):
    """编辑已建立的团队：修改描述、主理 Agent、增删成员（全量替换 members）。"""
    from ..services.agent_store import read_team, save_team

    team = read_team(name)
    if not team:
        raise HTTPException(404, "团队不存在")
    leader_name = (body.leader or "").strip() or team.get("leader") or f"{name}-leader"
    if not db.query(Agent).filter(Agent.name == leader_name).first():
        # 主理不存在则自动创建（与创建团队一致的规则）
        from ..models import new_id
        from ..services.agent_store import save_agent_dir

        leader = Agent(
            id=new_id("agent"), name=leader_name, version=1,
            description=f"团队「{name}」的主理 Agent",
            system_prompt=(f"你是团队「{name}」的主理 Agent，负责协调成员并汇总结果。"
                           f"团队描述：{body.description or '无'}。回答简洁自然。"),
            status="published",
        )
        db.add(leader)
        db.flush()
        save_agent_dir(leader)
    for m in body.members:
        if not db.query(Agent).filter(Agent.name == m).first():
            raise HTTPException(422, f"成员 Agent '{m}' 不存在")
    team["description"] = body.description
    team["leader"] = leader_name
    team["members"] = body.members
    save_team(team)
    audit(db, "agent.team.update", target=f"team:{name}",
          detail={"leader": leader_name, "members": body.members})
    db.commit()
    return team


@router.delete("/agents/teams/{name}")
def remove_team(name: str, db: Session = Depends(get_db)):
    from ..services.agent_store import delete_team

    if not delete_team(name):
        raise HTTPException(404, "团队不存在")
    audit(db, "agent.team.delete", target=f"team:{name}")
    db.commit()
    return {"ok": True, "name": name}


@router.get("/agents", response_model=list[AgentOutFull])
def list_agents_full(db: Session = Depends(get_db)):
    from ..services import asset_sync
    asset_sync.sync_agents(db)
    return db.query(Agent).order_by(Agent.created_at.desc()).all()


@router.get("/agents/{agent_id}", response_model=AgentOutFull)
def get_agent_full(agent_id: str, db: Session = Depends(get_db)):
    from ..services import asset_sync
    asset_sync.sync_agents(db)
    agent = db.query(Agent).filter(Agent.id == agent_id).order_by(Agent.version.desc()).first()
    if not agent:
        raise HTTPException(404, "Agent 不存在")
    return agent


class ForkIn(BaseModel):
    new_name: str


@router.post("/agents/{agent_id}/fork", response_model=AgentOutFull)
def fork_agent(agent_id: str, body: ForkIn, db: Session = Depends(get_db)):
    from ..services import asset_sync
    from ..services.agent_store import save_agent_dir

    source = db.query(Agent).filter(Agent.id == agent_id).order_by(Agent.version.desc()).first()
    if not source:
        raise HTTPException(404, "Agent 不存在")
    if db.query(Agent).filter(Agent.name == body.new_name).first():
        raise HTTPException(409, f"名称 '{body.new_name}' 已存在")
    forked = market_service.fork_agent(db, source, body.new_name)
    save_agent_dir(forked)
    db.commit()
    asset_sync.mark_synced("agents", db)
    return forked


@router.post("/agents/{agent_id}/eval")
def eval_agent(agent_id: str, db: Session = Depends(get_db)):
    agent = db.query(Agent).filter(Agent.id == agent_id).order_by(Agent.version.desc()).first()
    if not agent:
        raise HTTPException(404, "Agent 不存在")
    eval_set = agent.eval_set or [{"input": "你好", "contains_any": []}]
    er = market_service.run_eval(db, agent, eval_set)
    db.commit()
    return {"id": er.id, "total": er.total, "passed": er.passed, "results": er.results}


@router.get("/agents/{agent_id}/evals")
def list_evals(agent_id: str, db: Session = Depends(get_db)):
    rows = (db.query(EvalRun).filter(EvalRun.agent_id == agent_id)
            .order_by(EvalRun.created_at.desc()).limit(20).all())
    return [{"id": r.id, "agent_version": r.agent_version, "total": r.total, "passed": r.passed,
             "created_at": str(r.created_at)} for r in rows]


@router.post("/agents/{agent_id}/playground", response_model=RunOut)
def playground(agent_id: str, body: RunCreate, db: Session = Depends(get_db)):
    agent = db.query(Agent).filter(Agent.id == agent_id).order_by(Agent.version.desc()).first()
    if not agent:
        raise HTTPException(404, "Agent 不存在")
    run = market_service.run_playground(db, agent, body.input, history=body.history)
    db.commit()
    return run


@router.post("/agents/{agent_id}/playground/stream")
def playground_stream(agent_id: str, body: RunCreate, db: Session = Depends(get_db)):
    """Playground 流式执行：实时推送 ReAct 循环的思考/工具/结果事件。"""
    from fastapi.responses import StreamingResponse
    from ..db import get_session_factory
    from ..services.event_bridge import stream_callable

    agent = db.query(Agent).filter(Agent.id == agent_id).order_by(Agent.version.desc()).first()
    if not agent:
        raise HTTPException(404, "Agent 不存在")
    agent_snapshot = agent
    text = body.input
    history = body.history

    def frame(obj: dict) -> str:
        return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"

    def work(emit):
        bg = get_session_factory()()
        try:
            a = bg.query(Agent).filter(Agent.id == agent_id).order_by(Agent.version.desc()).first()
            run = market_service.run_playground(bg, a, text, on_event=emit, history=history)
            bg.commit()
            emit({"type": "done", "status": run.status, "output": run.output_text,
                  "steps": run.steps, "token_usage": run.token_usage})
        except Exception as exc:
            emit({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
        finally:
            bg.close()

    return StreamingResponse(
        (frame(ev) for ev in stream_callable(work)),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/agents/{agent_id}/runs", response_model=RunOut)
def create_run(agent_id: str, body: RunCreate, db: Session = Depends(get_db)):
    agent = db.query(Agent).filter(Agent.id == agent_id).order_by(Agent.version.desc()).first()
    if not agent or agent.status != "published":
        raise HTTPException(404, "Agent 不存在或未发布")
    run = run_service.run_agent(db, agent, body.input, use_rag=body.use_rag, history=body.history)
    db.commit()
    return run


# ══ A2A ══

class A2ATaskIn(BaseModel):
    message: str
    agent_name: str = ""  # 内部/入站目标 Agent
    idempotency_key: str | None = None
    parent_task_id: str = ""
    depth: int = 0
    trace_id: str = ""
    # 出站专用
    peer: str | None = None


class A2ATaskOut(BaseModel):
    id: str
    state: str
    direction: str
    agent_name: str
    peer_name: str
    trace_id: str
    depth: int
    result: str
    error: str
    run_id: str


def _task_out(t: A2ATask) -> A2ATaskOut:
    return A2ATaskOut(id=t.id, state=t.state, direction=t.direction, agent_name=t.agent_name,
                      peer_name=t.peer_name, trace_id=t.trace_id, depth=t.depth,
                      result=t.result, error=t.error, run_id=t.run_id)


@router.get("/a2a/public-url")
def a2a_public_url():
    """本平台对外暴露的 A2A 基础 URL（用于配置 Agent Card / 告知外部系统）。"""
    from ..config import get_settings

    s = get_settings()
    base = s.a2a_public_base_url.rstrip("/") if s.a2a_public_base_url else ""
    return {
        "base_url": base,
        "tasks_url": f"{base}/a2a/tasks" if base else "",
        "card_url": f"{base}/a2a/cards/{{agent_name}}" if base else "",
        "configured": bool(base),
        "hint": ("把 tasks_url 提供给外部系统作为本平台 Agent 的 A2A 端点；"
                 "未配置时在 .env 设置 AWS_A2A_PUBLIC_BASE_URL 为外部可访问地址（不要用 localhost）"),
    }


@router.get("/.well-known/agent-card.json")
def a2a_wellknown_card(db: Session = Depends(get_db)):
    """A2A 标准发现端点（新版路径）：返回本平台主 Agent 的 Card。"""
    return _build_wellknown_card(db, "")


def _build_wellknown_card(db: Session, agent_name: str) -> dict:
    from ..config import get_settings

    settings = get_settings()
    base = settings.a2a_public_base_url.rstrip("/") if settings.a2a_public_base_url else ""
    if agent_name:
        agent = (db.query(Agent).filter(Agent.name == agent_name, Agent.status == "published")
                 .order_by(Agent.version.desc()).first())
    else:
        agent = (db.query(Agent).filter(Agent.status == "published")
                 .order_by(Agent.is_orchestrator.desc(), Agent.created_at.asc()).first())
    if not agent:
        raise HTTPException(404, "无可发现的 Agent")
    local = f"http://localhost:{os.environ.get('API_PORT', '8900')}"
    card = a2a_service.agent_card(agent, base, local_url=local)
    card["url"] = f"{base}/a2a/tasks" if base else f"{local}/a2a/tasks"
    # 标准 A2A Card 的 skills 是对象数组
    skills = []
    for k in (agent.skill_keys or []):
        sk = db.query(Skill).filter(Skill.name == k).first()
        skills.append({"id": k, "name": k,
                       "description": (sk.description if sk else ""),
                       "tags": [sk.category] if sk else []})
    card["skills"] = skills
    return card


@router.get("/a2a/cards/{agent_name}")
def a2a_card(agent_name: str, db: Session = Depends(get_db)):
    from ..config import get_settings

    agent = (db.query(Agent).filter(Agent.name == agent_name, Agent.status == "published")
             .order_by(Agent.version.desc()).first())
    if not agent:
        raise HTTPException(404, "Agent 不存在或未发布")
    s = get_settings()
    base = s.a2a_public_base_url.rstrip("/") if s.a2a_public_base_url else ""
    local = f"http://localhost:{os.environ.get('API_PORT', '8900')}"
    return a2a_service.agent_card(agent, base, local_url=local)


@router.post("/a2a/tasks", response_model=A2ATaskOut)
def a2a_task(body: A2ATaskIn, request_source: str | None = None, db: Session = Depends(get_db)):
    """A2A 任务统一入口。

    - body.peer 指定 → 出站（调用外部 Peer）
    - 请求来自已登记 inbound Peer（request_source）→ 入站
    - 否则 → 内部任务
    """
    idem = body.idempotency_key or f"auto-{new_id('k')}"
    trace = body.trace_id or a2a_service.new_trace_id()

    if body.peer:
        peer = db.query(A2APeer).filter(A2APeer.name == body.peer, A2APeer.enabled.is_(True)).first()
        if not peer:
            raise HTTPException(404, f"Peer '{body.peer}' 不存在或未启用")
        task, _created = a2a_service.get_or_create_task(
            db, idempotency_key=f"out:{idem}", direction="outbound", peer_name=peer.name,
            parent_task_id=body.parent_task_id, depth=body.depth, trace_id=trace, message=body.message)
        if _created:
            task = a2a_service.run_outbound_task(db, task, peer)
        db.commit()
        return _task_out(task)

    direction = "inbound" if request_source else "internal"
    task, created = a2a_service.get_or_create_task(
        db, idempotency_key=f"{direction[:2]}:{idem}", direction=direction,
        agent_name=body.agent_name, peer_name=request_source or "",
        parent_task_id=body.parent_task_id, depth=body.depth, trace_id=trace, message=body.message)
    if not created:
        return _task_out(task)  # 幂等命中：返回既有任务

    if direction == "inbound":
        if not task.agent_name:
            task.state = "failed"
            task.error = "入站任务缺少目标 Agent（message 格式: 'agent_name|内容'）"
        else:
            task = a2a_service.run_inbound_task(db, task)
    else:
        if not task.agent_name:
            # 内部默认：message 头 'agent_name|内容' 或报错
            if "|" in body.message:
                task.agent_name, _, task.message = body.message.partition("|")
                db.flush()
            else:
                task.state = "failed"
                task.error = "内部任务缺少目标 Agent（message 格式: 'agent_name|内容'）"
                return _task_out(task)
        task = a2a_service.run_internal_task(db, task)
    db.commit()
    return _task_out(task)


@router.get("/a2a/trace/{trace_id}")
def a2a_trace(trace_id: str, db: Session = Depends(get_db)):
    """调用链查询：按 trace_id 返回一次请求的完整 A2A 任务链（父子关系、状态、耗时）。"""
    rows = (db.query(A2ATask).filter(A2ATask.trace_id == trace_id)
            .order_by(A2ATask.created_at.asc()).all())
    nodes = []
    for t in rows:
        dur_ms = None
        if t.created_at and t.updated_at:
            dur_ms = int((t.updated_at - t.created_at).total_seconds() * 1000)
        nodes.append({
            "id": t.id, "parent_id": t.parent_task_id or None,
            "depth": t.depth, "direction": t.direction,
            "agent": t.agent_name or None, "peer": t.peer_name or None,
            "state": t.state, "message": (t.message or "")[:120],
            "result": (t.result or t.error or "")[:160],
            "run_id": t.run_id or None, "duration_ms": dur_ms,
            "created_at": str(t.created_at),
        })
    # 按 parent 组树形结构（根节点 = parent_id 为空或父不在本链）
    by_id = {n["id"]: n for n in nodes}
    by_parent: dict[str | None, list[dict]] = {}
    for n in nodes:
        p = n["parent_id"] if n["parent_id"] in by_id else None
        by_parent.setdefault(p, []).append(n)
    def grow(p):
        return [{**n, "children": grow(n["id"])} for n in by_parent.get(p, [])]
    return {"trace_id": trace_id, "total": len(nodes), "tree": grow(None)}


@router.get("/a2a/traces")
def a2a_trace_list(limit: int = 20, db: Session = Depends(get_db)):
    """最近调用链列表（供用户选择要查的 trace）。"""
    rows = (db.query(A2ATask.trace_id)
            .filter(A2ATask.trace_id != "")
            .order_by(A2ATask.created_at.desc()).all())
    seen, out = set(), []
    for (tid,) in rows:
        if tid in seen:
            continue
        seen.add(tid)
        tasks = db.query(A2ATask).filter(A2ATask.trace_id == tid).all()
        out.append({
            "trace_id": tid, "task_count": len(tasks),
            "agents": sorted({t.agent_name for t in tasks if t.agent_name}),
            "states": sorted({t.state for t in tasks}),
            "latest_at": str(max((t.created_at for t in tasks), default="")),
        })
        if len(out) >= limit:
            break
    return out


@router.get("/a2a/tasks/{task_id}", response_model=A2ATaskOut)
def a2a_get(task_id: str, db: Session = Depends(get_db)):
    t = db.query(A2ATask).filter(A2ATask.id == task_id).first()
    if not t:
        raise HTTPException(404, "任务不存在")
    return _task_out(t)


@router.post("/a2a/tasks/{task_id}/cancel", response_model=A2ATaskOut)
def a2a_cancel(task_id: str, db: Session = Depends(get_db)):
    t = a2a_service.cancel_task(db, task_id)
    if not t:
        raise HTTPException(404, "任务不存在")
    return _task_out(t)


class PeerIn(BaseModel):
    name: str = ""              # 可留空：自动发现时从 Agent Card 取
    base_url: str
    description: str = ""      # 这个外部 Agent 能干什么（发现失败时手填）
    category: str = ""          # 分类，留空则按 description 自动归类
    auth_type: str = "bearer"
    auth_token: str = ""
    direction: str = "outbound"
    data_policy: dict = Field(default_factory=dict)
    rate_limit_rpm: int = 60
    auto_discover: bool = True  # 是否自动从 Agent Card 发现（默认开）


class PeerDiscoverIn(BaseModel):
    base_url: str
    auth_token: str = ""
    auth_type: str = "bearer"


@router.post("/a2a/peers/discover")
def discover_peer_card(body: PeerDiscoverIn):
    """预览发现：从外部 Agent Card 自动读取名称/简介/技能（不落库）。"""
    from ..services.a2a_card_client import discover_card

    headers = {"Authorization": f"Bearer {body.auth_token}"} if (
        body.auth_type == "bearer" and body.auth_token) else {}
    return discover_card(body.base_url, headers=headers)


@router.post("/a2a/peers")
def create_peer(body: PeerIn, db: Session = Depends(get_db)):
    from ..services.skill_catalog import classify
    from ..services.a2a_card_client import discover_card

    name, desc, cat, skills, card_url, discovered = (
        body.name, body.description, body.category, [], "", False)
    # 自动发现：从 Agent Card 拉取能力信息（用户只需填 base_url）
    if body.auto_discover:
        headers = {"Authorization": f"Bearer {body.auth_token}"} if (
            body.auth_type == "bearer" and body.auth_token) else {}
        card = discover_card(body.base_url, headers=headers)
        if card.get("ok"):
            discovered = True
            card_url = card.get("card_url", "")
            name = name or card.get("name") or ""
            desc = desc or card.get("description") or ""
            skills = card.get("skills", [])
            cat = cat or classify(f"{name} {desc} " + " ".join(s.get("name", "") for s in skills))
    if not name:
        raise HTTPException(422, "未能从 Agent Card 自动获取名称，请手动填写 name")
    if db.query(A2APeer).filter(A2APeer.name == name).first():
        raise HTTPException(409, f"Peer '{name}' 已存在")
    cat = (cat or "").strip() or classify(f"{name} {desc}")
    peer = A2APeer(
        id=new_id("peer"), name=name, base_url=body.base_url,
        description=desc, category=cat, skills=skills,
        card_url=card_url, discovered=discovered,
        auth_type=body.auth_type, auth_token=body.auth_token, direction=body.direction,
        data_policy=body.data_policy, rate_limit_rpm=body.rate_limit_rpm,
    )
    db.add(peer)
    audit(db, "peer.create", target=f"peer:{name}",
          detail={"base_url": body.base_url, "direction": body.direction,
                  "discovered": discovered, "skills": len(skills)})
    db.flush()
    db.commit()
    return {"id": peer.id, "name": peer.name, "category": peer.category,
            "discovered": discovered, "skill_count": len(skills)}


@router.get("/a2a/peers")
def list_peers(db: Session = Depends(get_db)):
    out = []
    for p in db.query(A2APeer).order_by(A2APeer.category, A2APeer.name).all():
        out.append({
            "id": p.id, "name": p.name, "base_url": p.base_url,
            "description": p.description, "category": p.category,
            "skills": p.skills or [], "card_url": p.card_url,
            "discovered": p.discovered,
            "direction": p.direction, "enabled": p.enabled,
        })
    return out


@router.post("/a2a/peers/{peer_id}/refresh-card")
def refresh_peer_card(peer_id: str, db: Session = Depends(get_db)):
    """重新发现外部 Peer 的 Agent Card，更新能力信息。"""
    from ..services.a2a_card_client import discover_card
    from ..services.skill_catalog import classify

    p = db.query(A2APeer).filter(A2APeer.id == peer_id).first()
    if not p:
        raise HTTPException(404, "Peer 不存在")
    headers = {"Authorization": f"Bearer {p.auth_token}"} if (
        p.auth_type == "bearer" and p.auth_token) else {}
    card = discover_card(p.base_url, headers=headers)
    if not card.get("ok"):
        raise HTTPException(422, f"发现失败: {card.get('error')}")
    p.description = card.get("description") or p.description
    p.skills = card.get("skills", [])
    p.card_url = card.get("card_url", "")
    p.discovered = True
    p.category = classify(f"{p.name} {p.description} " +
                          " ".join(s.get("name", "") for s in p.skills))
    db.commit()
    return {"ok": True, "name": p.name, "description": p.description,
            "skills": p.skills, "category": p.category}


@router.delete("/a2a/peers/{peer_id}")
def delete_peer(peer_id: str, db: Session = Depends(get_db)):
    p = db.query(A2APeer).filter(A2APeer.id == peer_id).first()
    if not p:
        raise HTTPException(404, "Peer 不存在")
    name = p.name
    db.delete(p)
    audit(db, "peer.delete", target=f"peer:{name}")
    db.commit()
    return {"ok": True, "name": name}


# ══ MCP 市场 ══

class MCPIn(BaseModel):
    name: str
    description: str = ""
    transport: str = "http"      # http | sse | stdio
    endpoint: str = ""
    command: str = ""
    env: dict = Field(default_factory=dict)
    headers: dict = Field(default_factory=dict)  # HTTP 自定义请求头（鉴权）
    enabled: bool = True


@router.get("/mcp/servers")
def mcp_list(db: Session = Depends(get_db)):
    from ..services import mcp_service
    return [mcp_service.to_dict(s) for s in
            db.query(MCPServer).order_by(MCPServer.created_at.desc()).all()]


@router.get("/mcp/categories")
def mcp_categories():
    return [
        {"key": "文件与系统", "label": "文件与系统"}, {"key": "数据与数据库", "label": "数据与数据库"},
        {"key": "网络与搜索", "label": "网络与搜索"}, {"key": "开发工具", "label": "开发工具"},
        {"key": "知识与文档", "label": "知识与文档"}, {"key": "通讯与协作", "label": "通讯与协作"},
        {"key": "其他", "label": "其他"},
    ]


def _create_mcp_server(db: Session, name: str, description: str, transport: str,
                       endpoint: str, command: str, env: dict, headers: dict | None = None,
                       enabled: bool = True) -> dict:
    """接入单个 MCP Server 的内部实现（供表单与 JSON 导入复用）。"""
    import re
    from ..services import mcp_service

    if not re.match(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{1,63}$", name):
        raise HTTPException(422, f"名称「{name}」只能用字母、数字、连字符、下划线（2-64 字符）")
    if db.query(MCPServer).filter(MCPServer.name == name).first():
        raise HTTPException(409, f"MCP Server '{name}' 已存在")
    if transport in ("http", "sse") and not endpoint:
        raise HTTPException(422, f"{name}: HTTP/SSE 传输必须提供 url")
    if transport == "stdio" and not command:
        raise HTTPException(422, f"{name}: stdio 传输必须提供 command")

    srv = MCPServer(
        id=new_id("mcp"), name=name, description=description,
        category=mcp_service.classify(description),
        transport=transport, endpoint=endpoint, command=command,
        env_json=env, headers_json=headers or {}, enabled=enabled,
    )
    db.add(srv)
    db.flush()
    result = mcp_service.connect_server(db, srv)
    audit(db, "mcp.create", target=f"mcp:{name}",
          detail={"transport": transport, "connected": result.get("ok")})
    db.commit()
    return {"id": srv.id, **mcp_service.to_dict(srv), "connect": result}


@router.post("/mcp/servers")
def mcp_create(body: MCPIn, db: Session = Depends(get_db)):
    """接入 MCP Server：先连接发现工具，再落库并注册工具。"""
    return _create_mcp_server(
        db, name=body.name, description=body.description, transport=body.transport,
        endpoint=body.endpoint, command=body.command, env=body.env,
        headers=body.headers, enabled=body.enabled,
    )


class MCPImportIn(BaseModel):
    config: str  # 粘贴的 JSON 文本（mcpServers 标准格式）


@router.post("/mcp/import")
def mcp_import(body: MCPImportIn, db: Session = Depends(get_db)):
    """从 MCP 标准 JSON 配置批量导入（Claude Desktop / Cursor 等格式）。

    支持：
    - {"mcpServers": {"name": {"command": "...", "args": [...]}}}
    - {"mcpServers": {"name": {"url": "http://..."}}}
    - 直接是 {"name": {...}} 也可以
    """
    import json as _json

    raw = (body.config or "").strip()
    if not raw:
        raise HTTPException(422, "配置内容为空")
    try:
        data = _json.loads(raw)
    except Exception as exc:
        raise HTTPException(422, f"JSON 解析失败: {exc}")
    # 取 mcpServers 节点（或直接当映射）
    servers = data.get("mcpServers", data) if isinstance(data, dict) else None
    if not isinstance(servers, dict) or not servers:
        raise HTTPException(422, "未找到 mcpServers 配置（应是一个 {名称: 配置} 的对象）")

    results = []
    for name, cfg in servers.items():
        try:
            if not isinstance(cfg, dict):
                results.append({"name": name, "ok": False, "error": "配置不是对象"})
                continue
            # 判定传输：有 url → http/sse，有 command → stdio
            url = cfg.get("url") or ""
            command = cfg.get("command") or ""
            args = cfg.get("args") or []
            env = cfg.get("env") or {}
            headers = cfg.get("headers") or {}
            if url:
                transport = "sse" if cfg.get("transport") == "sse" else "http"
                endpoint, cmd = url, ""
            elif command:
                transport, endpoint = "stdio", ""
                # command + args 拼成一条可执行的 shell 命令
                import shlex
                cmd = " ".join([command] + [shlex.quote(str(a)) for a in args])
            else:
                results.append({"name": name, "ok": False, "error": "缺少 url 或 command"})
                continue
            out = _create_mcp_server(
                db, name=name, description=cfg.get("description", ""),
                transport=transport, endpoint=endpoint, command=cmd,
                env=env, headers=headers, enabled=cfg.get("disabled") is not True,
            )
            results.append({"name": name, "ok": bool(out.get("connect", {}).get("ok")),
                            "tool_count": out.get("tool_count", 0),
                            "error": (out.get("connect", {}).get("error") or "")[:200]})
        except HTTPException as he:
            results.append({"name": name, "ok": False, "error": str(he.detail)})
        except Exception as exc:
            results.append({"name": name, "ok": False, "error": f"{type(exc).__name__}: {exc}"})
    return {"imported": len(servers), "results": results}


@router.post("/mcp/servers/{sid}/refresh")
def mcp_refresh(sid: str, db: Session = Depends(get_db)):
    """重新连接并刷新 MCP Server 的工具列表。"""
    from ..services import mcp_service

    srv = db.query(MCPServer).filter(MCPServer.id == sid).first()
    if not srv:
        raise HTTPException(404, "MCP Server 不存在")
    result = mcp_service.connect_server(db, srv, refresh=True)
    return {"ok": result.get("ok"), **mcp_service.to_dict(srv)}


@router.put("/mcp/servers/{sid}/enabled")
def mcp_set_enabled(sid: str, body: dict, db: Session = Depends(get_db)):
    """单独启停 MCP Server（不影响其它配置），并同步工具注册。"""
    from ..services import mcp_service, tool_registry

    srv = db.query(MCPServer).filter(MCPServer.id == sid).first()
    if not srv:
        raise HTTPException(404, "MCP Server 不存在")
    srv.enabled = bool(body.get("enabled", srv.enabled))
    # 同步工具注册：停用时注销，启用时重新注册（需已连接）
    if srv.enabled:
        mcp_service.register_mcp_tools(db, srv)
    else:
        for key in [k for k, t in tool_registry.TOOLS.items()
                    if t.get("source") == "mcp" and k.startswith(f"mcp_{srv.name}_")]:
            tool_registry.TOOLS.pop(key, None)
    mcp_service.save_config(srv)   # 文件优先：启停状态落盘
    db.commit()
    return mcp_service.to_dict(srv)


@router.put("/mcp/servers/{sid}")
def mcp_update(sid: str, body: MCPIn, db: Session = Depends(get_db)):
    """修改 MCP Server 配置（连接信息变更时需 refresh）。"""
    from ..services import mcp_service

    srv = db.query(MCPServer).filter(MCPServer.id == sid).first()
    if not srv:
        raise HTTPException(404, "MCP Server 不存在")
    for k, v in (("description", body.description), ("transport", body.transport),
                 ("endpoint", body.endpoint), ("command", body.command),
                 ("env_json", body.env), ("headers_json", body.headers),
                 ("enabled", body.enabled)):
        setattr(srv, k, v)
    srv.category = mcp_service.classify(body.description)
    mcp_service.save_config(srv)   # 文件优先
    db.commit()
    return mcp_service.to_dict(srv)


@router.delete("/mcp/servers/{sid}")
def mcp_delete(sid: str, db: Session = Depends(get_db)):
    from ..services import tool_registry, artifact_store

    srv = db.query(MCPServer).filter(MCPServer.id == sid).first()
    if not srv:
        raise HTTPException(404, "MCP Server 不存在")
    name = srv.name
    # 注销该 Server 的工具
    for key in [k for k, t in tool_registry.TOOLS.items() if t.get("source") == "mcp"
                and k.startswith(f"mcp_{name}_")]:
        tool_registry.TOOLS.pop(key, None)
    db.delete(srv)
    artifact_store.delete_mcp_config(name)   # 删除项目 mcp/ 目录配置
    audit(db, "mcp.delete", target=f"mcp:{name}")
    db.commit()
    return {"ok": True, "name": name}


# ══ 主 Agent 编排对话 ══

class OrchOut(BaseModel):
    session_id: str
    reply: str
    delegations: list
    trace_id: str
    title: str
    history: list
    reasoning: str = ""  # 思考链（默认收起，用户点开才看）


@router.post("/orchestrator/sessions")
def orch_start(db: Session = Depends(get_db)):
    """创建通用主 Agent 会话（智能对话统一入口，无需选 Agent）。"""
    from ..services import orchestrator_service

    s = orchestrator_service.start_session(db)
    db.commit()
    return {"session_id": s.id, "title": s.title, "trace_id": s.trace_id}


@router.get("/orchestrator/sessions")
def orch_list(db: Session = Depends(get_db)):
    """历史会话列表（按最近更新倒序）。"""
    from ..models import OrchestratorSession

    rows = (db.query(OrchestratorSession)
            .order_by(OrchestratorSession.updated_at.desc())
            .limit(100).all())
    return [{
        "session_id": s.id, "title": s.title,
        "updated_at": str(s.updated_at), "trace_id": s.trace_id,
        "message_count": len(s.history or []),
        "preview": next((m.get("content", "")[:60] for m in reversed(s.history or [])
                         if m.get("role") == "assistant"), ""),
    } for s in rows]


@router.delete("/orchestrator/sessions/{sid}")
def orch_delete(sid: str, db: Session = Depends(get_db)):
    from ..models import OrchestratorSession
    from ..services.chat_queue import queue

    s = db.query(OrchestratorSession).filter(OrchestratorSession.id == sid).first()
    if not s:
        raise HTTPException(404, "会话不存在")
    queue.cancel_session(sid)
    db.delete(s)
    db.commit()
    return {"ok": True}


@router.post("/orchestrator/sessions/{sid}/chat", response_model=OrchOut)
def orch_chat(sid: str, body: DialogReplyIn, db: Session = Depends(get_db)):
    """非流式对话（向后兼容）。"""
    from ..models import OrchestratorSession
    from ..services import orchestrator_service

    s = db.query(OrchestratorSession).filter(OrchestratorSession.id == sid).first()
    if not s:
        raise HTTPException(404, "会话不存在")
    result = orchestrator_service.chat(db, s, body.text)
    db.commit()
    return OrchOut(session_id=sid, reply=result["reply"], delegations=result["delegations"],
                   trace_id=result["trace_id"], title=s.title, history=s.history,
                   reasoning=result.get("reasoning", ""))


@router.post("/orchestrator/sessions/{sid}/chat/stream")
def orch_chat_stream(sid: str, body: DialogReplyIn, db: Session = Depends(get_db)):
    """后台任务式流式对话：任务在后台线程执行，SSE 只转发事件。

    前端切换会话/页面断开 SSE 不会中断后台任务；重连可从事件缓冲续读。
    """
    import threading
    from fastapi.responses import StreamingResponse
    from ..models import OrchestratorSession
    from ..services import orchestrator_service
    from ..services.chat_queue import queue
    from ..db import get_session_factory
    from ..services.audit_service import audit

    s = db.query(OrchestratorSession).filter(OrchestratorSession.id == sid).first()
    if not s:
        raise HTTPException(404, "会话不存在")
    msg = queue.submit(sid, body.text)

    def frame(obj: dict) -> str:
        return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"

    def background_run():
        """后台线程：用独立 db session 执行任务，事件写入队列缓冲。"""
        bg_db = get_session_factory()()
        try:
            bg_s = bg_db.query(OrchestratorSession).filter(OrchestratorSession.id == sid).first()
            if not bg_s:
                queue.finish(sid, msg.id, "failed", "会话不存在")
                return
            queue.append_event(msg.id, {"type": "queued", "message_id": msg.id,
                                        "pending": len(queue.session_pending(sid))})
            if not queue.wait_and_acquire(sid, msg.id, timeout=180):
                state = queue.status(msg.id)
                queue.append_event(msg.id, {
                    "type": "canceled" if state and state["canceled"] else "error",
                    "message_id": msg.id,
                    "message": "已取消" if state and state["canceled"] else "排队超时"})
                queue.finish(sid, msg.id, "canceled" if state and state["canceled"] else "failed")
                return
            try:
                # event_sink：子 Agent 的实时事件直接写入 SSE 缓冲（委托在同步调用里产生）
                def _sink(payload: dict):
                    if payload.get("phase") == "sub":
                        queue.append_event(msg.id, payload)
                for ev in orchestrator_service.chat_stream(
                    bg_db, bg_s, msg.text, msg.id, msg.cancel, event_sink=_sink
                ):
                    # chat_stream yield 的是 SSE 帧字符串（data: ...），转成 dict 存入缓冲
                    try:
                        payload = json.loads(ev.replace("data: ", "", 1).strip())
                        queue.append_event(msg.id, payload)
                    except Exception:
                        queue.append_event(msg.id, {"type": "raw", "data": ev})
                queue.finish(sid, msg.id, "done")
                audit(bg_db, "orchestrator.turn", target=f"orch:{sid}", detail={"async": True, "msg": msg.id})
                bg_db.commit()
            except Exception as exc:
                queue.append_event(msg.id, {"type": "error", "message": str(exc), "message_id": msg.id})
                queue.finish(sid, msg.id, "failed", str(exc))
        finally:
            bg_db.close()

    # 启动后台线程（daemon：进程退出时不阻塞；任务状态仍落库）
    threading.Thread(target=background_run, daemon=True, name=f"orch-{sid}-{msg.id}").start()

    def gen():
        # SSE 消费者：从事件缓冲转发；前端断开只结束消费，后台任务不受影响
        for ev in queue.iter_events(msg.id, start_index=0):
            yield frame(ev)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/orchestrator/messages/{mid}/stream")
def orch_message_stream(mid: str, from_index: int = 0):
    """重连某个后台任务的 SSE 事件流（前端切回会话时续读进度）。"""
    from fastapi.responses import StreamingResponse
    from ..services.chat_queue import queue

    snap = queue.message_snapshot(mid)
    if not snap:
        raise HTTPException(404, "消息不存在")

    def frame(obj: dict) -> str:
        return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"

    def gen():
        for ev in queue.iter_events(mid, start_index=from_index):
            yield frame(ev)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/orchestrator/running")
def orch_running_sessions():
    """返回正在执行任务的会话列表（供前端会话列表显示转圈/完成状态）。"""
    from ..services.chat_queue import queue

    return queue.running_sessions()


@router.get("/orchestrator/messages/{mid}")
def orch_message_status(mid: str):
    """消息队列状态查询。"""
    from ..services.chat_queue import queue

    status = queue.status(mid)
    if not status:
        raise HTTPException(404, "消息不存在")
    return status


@router.get("/orchestrator/messages/{mid}/snapshot")
def orch_message_snapshot(mid: str):
    """返回单个后台任务的最新事件快照（供前端切回会话时无竞态重建进度）。"""
    from ..services.chat_queue import queue

    snap = queue.message_snapshot(mid)
    if not snap:
        raise HTTPException(404, "消息不存在")
    return snap


@router.post("/orchestrator/messages/{mid}/cancel")
def orch_cancel_message(mid: str):
    """中断单条消息（排队中直接取消；执行中在下一个流式 chunk 停止）。"""
    from ..services.chat_queue import queue

    ok = queue.cancel(mid)
    return {"ok": ok, "status": queue.status(mid)}


@router.post("/orchestrator/sessions/{sid}/cancel")
def orch_cancel_session(sid: str):
    """中断会话内所有排队/执行中的消息。"""
    from ..services.chat_queue import queue

    return {"cancelled": queue.cancel_session(sid)}


@router.get("/orchestrator/sessions/{sid}/pending")
def orch_pending(sid: str):
    from ..services.chat_queue import queue

    return {"pending": queue.session_pending(sid)}


@router.get("/orchestrator/sessions/{sid}")
def orch_get(sid: str, db: Session = Depends(get_db)):
    from ..models import OrchestratorSession

    s = db.query(OrchestratorSession).filter(OrchestratorSession.id == sid).first()
    if not s:
        raise HTTPException(404, "会话不存在")
    return {"session_id": s.id, "title": s.title, "trace_id": s.trace_id, "history": s.history}



@router.get("/orchestrator/agents")
def orch_agents(db: Session = Depends(get_db)):
    from ..services import asset_sync, orchestrator_service

    asset_sync.sync_agents(db)
    return orchestrator_service.list_orchestrators_for_ui(db)


class AgentCreateFull(AgentCreate):
    workflow: dict = Field(default_factory=dict)
    eval_set: list[dict] = Field(default_factory=list)
    is_orchestrator: bool = False
    sub_agents: list[str] = Field(default_factory=list, description="编排型的子 Agent 名单")


# ══ 计量与审计 ══

@router.get("/usage")
def usage(subject: str | None = None, db: Session = Depends(get_db)):
    return get_usage(db, subject)


@router.get("/audit")
def audit_events(limit: int = 50, db: Session = Depends(get_db)):
    from ..models import AuditEvent
    rows = db.query(AuditEvent).order_by(AuditEvent.created_at.desc()).limit(min(limit, 200)).all()
    return [{"id": r.id, "action": r.action, "actor": r.actor, "target": r.target,
             "detail": r.detail, "created_at": str(r.created_at)} for r in rows]


# ══ 交付文件：Skill 脚本生成的报告等产物 ══

@router.get("/files/{run_key}")
def list_files(run_key: str):
    """列出某次运行生成的交付文件。"""
    from ..services.artifact_files import list_run_files
    return list_run_files(run_key)


@router.get("/files/{run_key}/{filename}")
def download_file(run_key: str, filename: str):
    """下载交付文件（防路径穿越：仅允许 outputs/<run_key>/ 下的普通文件）。"""
    from fastapi.responses import FileResponse
    from ..services.artifact_files import run_output_dir

    d = run_output_dir(run_key)
    target = (d / filename).resolve()
    if not str(target).startswith(str(d.resolve())) or not target.is_file():
        raise HTTPException(404, "文件不存在")
    suffix = target.suffix.lower()
    media = {
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".pdf": "application/pdf",
        ".json": "application/json",
        ".txt": "text/plain",
    }.get(suffix, "application/octet-stream")
    return FileResponse(str(target), filename=target.name, media_type=media)
