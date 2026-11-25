"""FastAPI 入口：启动时建表、注册内置插件、建 Qdrant collection。"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from .config import get_settings
from .db import get_session_factory, init_db
from .models import Plugin, Agent
from .plugins.builtin import ChunkingPlugin, ContextCompressorPlugin, IntentPlugin, MemoryPlugin
from .services import rag_service, skill_designer_service, plugin_service, plugin_designer_service, agent_store
from .routers.api import router as api_router
from .routers.api_full import router as full_router
from .version import VERSION


def _rebuild_skills_from_disk(session) -> None:
    """从 skills 目录重建 Skill 索引（DB 丢失/目录新增时自动恢复）。"""
    from .models import Skill, new_id
    from .services.artifact_store import scan_skill_dirs
    from .services.skill_catalog import make_summary, skill_category

    for info in scan_skill_dirs():
        name = info["name"]
        s = session.query(Skill).filter(Skill.name == name).first()
        if s is None:
            s = Skill(id=new_id("skill"), name=name, source="local_dir")
            session.add(s)
        s.description = info.get("description") or s.description
        s.instructions = info.get("instructions") or s.instructions
        s.artifact_path = f"skills/{name}"
        s.artifact_sha256 = info.get("sha256", "")
        s.files = info.get("files", [])
        s.status = "published"
        s.category = skill_category(name, s.description, s.instructions)
        s.summary = make_summary(s.description, s.instructions)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # 注册内置插件（幂等）
    session = get_session_factory()()
    try:
        for p in (MemoryPlugin(), ChunkingPlugin(), IntentPlugin(), ContextCompressorPlugin()):
            exists = session.query(Plugin).filter(Plugin.key == p.key).first()
            if not exists:
                session.add(
                    Plugin(
                        id=f"plugin_{p.key}",
                        key=p.key,
                        name=p.key,
                        extension_point=p.extension_point,
                        slot_semantics=p.slot_semantics,
                        config_schema=p.config_schema,
                        builtin=True,
                    )
                )
        session.commit()
    finally:
        session.close()
    # 注册内置 Skill 设计子 Agent（创作模式一，供主 Agent 经 A2A 调用）
    session = get_session_factory()()
    try:
        plugin_service.ensure_descriptions(session)  # 回填内置插件功能简介
        plugin_service.sync_all_plugins_to_disk(session)  # DB 中已有插件写回目录（迁移）
        plugin_service.rebuild_index_from_disk(session)   # 目录中有、DB 没有的插件补建索引
        _rebuild_skills_from_disk(session)
        agent_store.rebuild_agents_from_disk(session)
        # MCP 文件优先：先从 mcp/ 目录重建 DB 索引，再恢复工具注册
        from .services import mcp_service
        mcp_service.rebuild_index_from_disk(session)
        mcp_service.register_all_enabled(session)
        # 内置设计 Agent 必须在磁盘重建之后 ensure（否则旧 agent.json 会覆盖 prompt 迁移）
        skill_designer_service.ensure_designer(session)
        plugin_designer_service.ensure_designer(session)
        # DB 中已有的 Agent 写回目录（文件优先：agents/ 目录与 DB 保持一致）
        for _a in session.query(Agent).all():
            agent_store.save_agent_dir(_a)
        session.commit()
        # 从 DB 重建向量索引（维度变更/服务重启后；DB 文档是真相源）
        try:
            rag_service.ensure_collection()
            n = rag_service.reindex_all_documents(session)
            if n:
                print(f"[startup] reindexed {n} vectors from documents")
        except Exception as exc:
            # 模型服务不可用时降级：不阻断启动
            print(f"[startup] vector reindex skipped: {exc}")
    finally:
        session.close()
    yield


app = FastAPI(title="Agent Work System", version=VERSION, lifespan=lifespan)
# 先注册 full_router：其静态路由（如 /skills/categories）需优先于 api.py 的 /skills/{name}
app.include_router(full_router)
app.include_router(api_router)


@app.get("/console")
def console():
    """Web 控制台（静态 SPA）。"""
    from pathlib import Path
    html = Path(__file__).resolve().parent.parent / "web" / "index.html"
    from fastapi.responses import HTMLResponse
    return HTMLResponse(html.read_text(encoding="utf-8"))


@app.get("/health")
def health():
    s = get_settings()
    return {
        "status": "ok",
        "version": VERSION,
        "llm": "anthropic-compat" if s.anthropic_auth_token else "mock",
        "embedding": s.embedding_provider,
        "rerank": s.rerank_provider,
    }
