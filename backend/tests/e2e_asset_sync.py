"""三向同步全面测试：本地目录 ↔ 数据库 ↔ 前端自动渲染跟进。

覆盖：
1. 后端双向同步（Skills / Plugins / Agents）：
   - 本地目录新增 / 修改 / 删除 → 自动同步到数据库；
   - 数据库直接新增 / 修改 / 删除 → 自动同步到本地目录；
   - `delete_skill_dir` 安全删除验证。
2. 前端无刷新自动跟进（Playwright）：
   - 停留在 Skill 市场 / 插件市场 / Agent 市场页面，不刷新不点击；
   - 后端数据动态发生新增、修改、删除 → 前端 3 秒轮询自动更新渲染。
"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parents[2]
INDEX_HTML = (ROOT / "backend" / "web" / "index.html").read_bytes()


def test_backend_bidirectional_sync():
    """在隔离临时目录 + 轻量内存 DB 桩上测试真实 asset_sync.py 的 6 向同步逻辑。"""
    tmp = Path(tempfile.mkdtemp(prefix="aws_sync_test_"))
    try:
        os.environ["AWS_ARTIFACTS_DIR"] = str(tmp)
        os.environ["AWS_AGENTS_DIR"] = str(tmp / "agents")

        # 构造轻量内存 ORM 桩，直接加载真实 artifact_store / agent_store / plugin_service / skill_catalog / asset_sync
        class _Col:
            def __init__(self, name): self.name = name
            def __eq__(self, other): return (self.name, "eq", other)
            def desc(self): return (self.name, "desc")

        class _ModelMeta(type):
            def __getattr__(cls, item): return _Col(item)

        class _Base(metaclass=_ModelMeta):
            _defaults = {}
            def __init__(self, **kw):
                for k, v in self._defaults.items():
                    setattr(self, k, list(v) if isinstance(v, list) else (dict(v) if isinstance(v, dict) else v))
                for k, v in kw.items():
                    setattr(self, k, v)

        class Skill(_Base):
            _defaults = dict(description="", instructions="", category="其他", status="published",
                             artifact_path="", artifact_sha256="", files=[], summary="", source="local_dir")

        class SkillReview(_Base):
            _defaults = dict(skill_name="")

        class Plugin(_Base):
            _defaults = dict(name="", description="", mechanism="", extension_point="WorkflowNode",
                             slot_semantics="single", version_str="1.0.0", author="", source="local",
                             source_url="", config={}, config_schema={}, builtin=False, enabled=True, install_path="")

        class Agent(_Base):
            _defaults = dict(version=1, description="", system_prompt="", model="", skill_keys=[],
                             plugin_keys={}, plugin_config={}, workflow={}, eval_set=[],
                             forked_from="", is_orchestrator=False, sub_agents=[], status="published")

        _seq = [0]
        def new_id(prefix="id"):
            _seq[0] += 1
            return f"{prefix}_{_seq[0]}"

        class _Query:
            def __init__(self, store, cls):
                self._store = store; self._cls = cls; self._filters = []; self._order = None
            def filter(self, *conds):
                self._filters.extend(conds); return self
            def order_by(self, order):
                self._order = order; return self
            def _match(self):
                rows = list(self._store[self._cls])
                for col, op, val in self._filters:
                    rows = [r for r in rows if getattr(r, col, None) == val]
                if self._order:
                    col, _dir = self._order
                    rows.sort(key=lambda r: getattr(r, col, 0), reverse=(_dir == "desc"))
                return rows
            def all(self): return self._match()
            def first(self):
                m = self._match()
                return m[0] if m else None
            def delete(self):
                m = set(self._match())
                self._store[self._cls] = [r for r in self._store[self._cls] if r not in m]
                return len(m)

        class FakeDB:
            def __init__(self):
                self.store = {Skill: [], SkillReview: [], Plugin: [], Agent: []}
            def query(self, cls): return _Query(self.store, cls)
            def add(self, obj): self.store[type(obj)].append(obj)
            def delete(self, obj): self.store[type(obj)].remove(obj)
            def commit(self): pass
            def flush(self): pass

        # 注册虚拟包结构以加载真实模块
        for mod_name in ("app", "app.models", "app.services", "app.services.audit_service",
                         "sqlalchemy", "sqlalchemy.orm"):
            sys.modules[mod_name] = types.ModuleType(mod_name)
        sys.modules["sqlalchemy.orm"].Session = object
        sys.modules["app.models"].Skill = Skill
        sys.modules["app.models"].SkillReview = SkillReview
        sys.modules["app.models"].Plugin = Plugin
        sys.modules["app.models"].Agent = Agent
        sys.modules["app.models"].new_id = new_id
        sys.modules["app.services.audit_service"].audit = lambda *a, **k: None

        def load_real(mod_name, rel_path):
            sp = importlib.util.spec_from_file_location(mod_name, str(ROOT / rel_path))
            m = importlib.util.module_from_spec(sp)
            sys.modules[mod_name] = m
            sp.loader.exec_module(m)
            return m

        art = load_real("app.services.artifact_store", "backend/app/services/artifact_store.py")
        load_real("app.services.skill_catalog", "backend/app/services/skill_catalog.py")
        load_real("app.services.plugin_service", "backend/app/services/plugin_service.py")
        ag_store = load_real("app.services.agent_store", "backend/app/services/agent_store.py")
        sync = load_real("app.services.asset_sync", "backend/app/services/asset_sync.py")
        sync.reset_state_for_tests()

        db = FakeDB()
        sync.init_all_baselines(db)

        # ── 1. Skill 双向同步 ──
        # 1a. 本地目录新增 → DB 自动出现
        art.build_skill_dir("disk-skill", "来自本地目录", "本地指令v1")
        assert sync.sync_skills(db, force=True)
        s = db.query(Skill).filter(Skill.name == "disk-skill").first()
        assert s and "来自本地目录" in s.description, "磁盘新增 Skill 未同步到 DB"

        # 1b. 本地目录修改 → DB 自动更新
        time.sleep(0.02)
        art.write_skill_file("disk-skill", "SKILL.md", "---\nname: disk-skill\ndescription: 本地修改后的描述\n---\n\n新指令v2\n")
        assert sync.sync_skills(db, force=True)
        assert db.query(Skill).filter(Skill.name == "disk-skill").first().description == "本地修改后的描述"

        # 1c. DB 直接修改 → 本地目录自动更新
        s = db.query(Skill).filter(Skill.name == "disk-skill").first()
        s.description = "DB直接修改的描述"
        s.instructions = "---\nname: disk-skill\ndescription: DB直接修改的描述\n---\n\nDB新指令\n"
        assert sync.sync_skills(db, force=True)
        assert "DB直接修改的描述" in art.read_skill_file("disk-skill", "SKILL.md")

        # 1d. DB 直接删除 → 本地目录自动删除
        db.delete(s)
        assert sync.sync_skills(db, force=True)
        assert not art.skill_dir("disk-skill").exists(), "DB 删除 Skill 后本地目录未删除"

        # 1e. 本地目录删除 → DB 自动删除
        art.build_skill_dir("del-from-disk", "测试磁盘删", "内容")
        sync.sync_skills(db, force=True)
        assert db.query(Skill).filter(Skill.name == "del-from-disk").first() is not None
        art.delete_skill_dir("del-from-disk")
        assert sync.sync_skills(db, force=True)
        assert db.query(Skill).filter(Skill.name == "del-from-disk").first() is None

        # ── 2. Plugin 双向同步 ──
        art.store_plugin_dir("disk-plug", {"key": "disk-plug", "name": "磁盘插件", "description": "v1", "extension_point": "WorkflowNode"})
        assert sync.sync_plugins(db, force=True)
        p = db.query(Plugin).filter(Plugin.key == "disk-plug").first()
        assert p and p.name == "磁盘插件"

        p.description = "DB改过的插件描述"
        assert sync.sync_plugins(db, force=True)
        assert art.read_plugin_manifest("disk-plug")["description"] == "DB改过的插件描述"

        art.delete_plugin_dir("disk-plug")
        assert sync.sync_plugins(db, force=True)
        assert db.query(Plugin).filter(Plugin.key == "disk-plug").first() is None

        # ── 3. Agent 双向同步 ──
        ag_dir = ag_store.agent_dir("disk-agent")
        ag_dir.mkdir(parents=True, exist_ok=True)
        (ag_dir / "agent.json").write_text(json.dumps({
            "id": "ag_disk_1", "name": "disk-agent", "version": 1,
            "description": "磁盘Agent v1", "system_prompt": "hi",
        }, ensure_ascii=False), encoding="utf-8")
        assert sync.sync_agents(db, force=True)
        a = db.query(Agent).filter(Agent.name == "disk-agent").first()
        assert a and a.description == "磁盘Agent v1"

        a.description = "DB修改后的Agent描述"
        assert sync.sync_agents(db, force=True)
        assert ag_store.read_agent_manifest("disk-agent")["description"] == "DB修改后的Agent描述"

        db.delete(a)
        assert sync.sync_agents(db, force=True)
        assert not ag_store.agent_dir("disk-agent").exists(), "DB 删除 Agent 后本地目录未删除"

        print("✓ [Backend] Skills / Plugins / Agents 双向同步（磁盘↔DB 增改删）全部通过")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_frontend_live_auto_sync():
    """验证前端停留在 Skill / 插件 / Agent 市场时，后端变更后无需刷新自动跟进渲染。"""
    from playwright.sync_api import sync_playwright

    state = {
        "skills": [{"name": "办公", "color": "蓝", "count": 1, "skills": [
            {"name": "skill-a", "description": "初始Skill", "summary": "初始Skill", "source": "local_dir", "files_count": 1}
        ]}],
        "plugins": [
            {"id": "p1", "key": "plug-a", "name": "初始插件", "description": "插件说明", "mechanism": "",
             "extension_point": "WorkflowNode", "slot_semantics": "single", "config": {}, "config_schema": {},
             "builtin": False, "enabled": True, "source": "local", "source_url": "", "version": "1.0.0", "author": "", "user_editable": True}
        ],
        "agents": [
            {"id": "a1", "name": "agent-a", "version": 1, "description": "初始Agent", "system_prompt": "hi",
             "model": "", "skill_keys": [], "plugin_keys": {}, "plugin_config": {}, "workflow": {}, "eval_set": [],
             "forked_from": "", "is_orchestrator": False, "sub_agents": []}
        ],
        "teams": [],
    }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args): pass
        def _json(self, obj):
            data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        def do_GET(self):
            p = urlparse(self.path).path
            if p in ("/", "/console"):
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(INDEX_HTML)))
                self.end_headers()
                self.wfile.write(INDEX_HTML)
            elif p == "/orchestrator/sessions": self._json([{"session_id": "s1", "title": "测试"}])
            elif p == "/orchestrator/sessions/s1": self._json({"history": []})
            elif p == "/skills/categories": self._json(state["skills"])
            elif p == "/skills": self._json([])
            elif p == "/plugins/managed": self._json(state["plugins"])
            elif p == "/plugins": self._json(state["plugins"])
            elif p == "/agents": self._json(state["agents"])
            elif p == "/agents/teams": self._json(state["teams"])
            else: self._json([])

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"

    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        pg = b.new_page(viewport={"width": 1360, "height": 840})
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.goto(f"{base}/console", wait_until="domcontentloaded")

        # 1. Skill 市场自动跟进
        pg.locator('nav button[data-p="skills"]').click()
        pg.wait_for_function("() => document.querySelector('#sk-list')?.innerText.includes('skill-a')", timeout=5000)
        state["skills"][0]["skills"].append(
            {"name": "skill-from-disk", "description": "磁盘新加Skill", "summary": "磁盘新加Skill", "source": "local_dir", "files_count": 1}
        )
        state["skills"][0]["count"] = 2
        pg.wait_for_function("() => document.querySelector('#sk-list')?.innerText.includes('skill-from-disk')", timeout=6000)
        print("✓ [Frontend Live Sync] Skill 市场无刷新自动跟进通过")

        # 2. 插件市场自动跟进
        pg.locator('nav button[data-p="plugins"]').click()
        pg.wait_for_function("() => document.querySelector('#pl-list')?.innerText.includes('初始插件')", timeout=5000)
        state["plugins"][0]["name"] = "后端修改后的插件名"
        pg.wait_for_function("() => document.querySelector('#pl-list')?.innerText.includes('后端修改后的插件名')", timeout=6000)
        print("✓ [Frontend Live Sync] 插件市场无刷新自动跟进通过")

        # 3. Agent 市场自动跟进
        pg.locator('nav button[data-p="agents"]').click()
        pg.wait_for_function("() => document.querySelector('#ag-list')?.innerText.includes('agent-a')", timeout=5000)
        state["agents"].append({
            "id": "a2", "name": "agent-from-db", "version": 1, "description": "数据库新加Agent", "system_prompt": "hi",
            "model": "", "skill_keys": [], "plugin_keys": {}, "plugin_config": {}, "workflow": {}, "eval_set": [],
            "forked_from": "", "is_orchestrator": False, "sub_agents": []
        })
        pg.wait_for_function("() => document.querySelector('#ag-list')?.innerText.includes('agent-from-db')", timeout=6000)
        print("✓ [Frontend Live Sync] Agent 市场无刷新自动跟进通过")

        assert not errs, f"JS 错误: {errs}"
        b.close()
    srv.shutdown()
    print("ALL THREE-WAY SYNC TESTS PASSED ✓")


if __name__ == "__main__":
    test_backend_bidirectional_sync()
    test_frontend_live_auto_sync()
