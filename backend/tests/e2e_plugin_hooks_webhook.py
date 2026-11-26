"""全面测试三大插件架构升级：
1. 四阶段生命周期钩子：before_llm / before_tool / after_tool / after_llm（含拦截、参数改写、结果/答案脱敏）
2. agent.plugin_config 透传（Plugin.config < key < slot 三级合并）+ WorkflowNode / ContextCompressor 多实例叠加校验
3. http_webhook 真实外部微服务调用（含上下文改写、拦截、fail_open=false 严格模式）
"""
import importlib.util
import json
import sys
import threading
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parents[2]


def _setup_stubs():
    class _Col:
        def __init__(self, name): self.name = name
        def __eq__(self, other): return (self.name, "eq", other)
        def is_(self, other): return (self.name, "eq", other)

    class _Meta(type):
        def __getattr__(cls, item): return _Col(item)

    class _Base(metaclass=_Meta):
        def __init__(self, **kw):
            for k, v in kw.items(): setattr(self, k, v)

    class Plugin(_Base): pass
    class Skill(_Base): pass
    class Agent(_Base): pass
    class AgentRun(_Base): pass

    class _Query:
        def __init__(self, store, cls):
            self._store = store; self._cls = cls; self._filters = []
        def filter(self, *conds):
            self._filters.extend(conds); return self
        def first(self):
            for r in self._store[self._cls]:
                if all(getattr(r, c, None) == v for c, _, v in self._filters):
                    return r
            return None

    class FakeDB:
        def __init__(self):
            self.store = {Plugin: [], Skill: [], Agent: [], AgentRun: []}
        def query(self, cls): return _Query(self.store, cls)
        def add(self, obj): self.store[type(obj)].append(obj)

    for m in ("app", "app.models", "app.plugins", "app.services",
              "app.services.rag_service", "app.services.audit_service", "app.services.model_gateway",
              "sqlalchemy", "sqlalchemy.orm"):
        sys.modules[m] = types.ModuleType(m)
    sys.modules["sqlalchemy.orm"].Session = object
    sys.modules["app.models"].Plugin = Plugin
    sys.modules["app.models"].Skill = Skill
    sys.modules["app.models"].Agent = Agent
    sys.modules["app.models"].AgentRun = AgentRun
    sys.modules["app.models"].new_id = lambda p="id": f"{p}_1"
    sys.modules["app.services.audit_service"].record_usage = lambda *a, **k: None
    sys.modules["app.services.model_gateway"].ModelGateway = object

    def load(mod, rel):
        sp = importlib.util.spec_from_file_location(mod, str(ROOT / rel))
        m = importlib.util.module_from_spec(sp)
        sys.modules[mod] = m
        sp.loader.exec_module(m)
        return m

    bp = load("app.plugins.builtin", "backend/app/plugins/builtin.py")
    pr = load("app.services.plugin_runner", "backend/app/services/plugin_runner.py")
    rs = load("app.services.run_service", "backend/app/services/run_service.py")
    ags = load("app.services.agent_service", "backend/app/services/agent_service.py")
    return FakeDB, Plugin, Skill, Agent, bp, pr, rs, ags


def run_all():
    FakeDB, Plugin, Skill, Agent, bp, pr, rs, ags = _setup_stubs()
    db = FakeDB()

    # 注册测试插件：两个 WorkflowNode + 一个 IntentRecognizer
    p_filter = Plugin(
        id="p1", key="sec_filter", name="安全脱敏过滤", extension_point="WorkflowNode",
        enabled=True, config={
            "behavior": "content_filter",
            "hooks": ["before_llm", "before_tool", "after_tool", "after_llm"],
            "block_keywords": ["绝密口令"],
            "block_tools": ["danger_delete"],
            "redact_patterns": [{"pattern": r"1[3-9]\d{9}", "replace": "[手机号已脱敏]"}],
        },
    )
    p_hook = Plugin(
        id="p2", key="ext_webhook", name="外部微服务钩子", extension_point="WorkflowNode",
        enabled=True, config={"behavior": "http_webhook", "hooks": ["before_llm", "before_tool", "after_tool", "after_llm"]},
    )
    p_intent = Plugin(
        id="p3", key="intent_keyword", name="意图识别", extension_point="IntentRecognizer",
        enabled=True, config={},
    )
    db.add(p_filter); db.add(p_hook); db.add(p_intent)

    # ── 1. 验证 WorkflowNode 多实例叠加校验 ──
    warns = ags.validate_assembly(
        db,
        skill_keys=[],
        plugin_keys={"intent": "intent_keyword", "workflow:sec": "sec_filter", "workflow:hook": "ext_webhook"},
    )
    assert isinstance(warns, list), "多实例 WorkflowNode 校验应通过"

    # 单槽位禁止多实例
    try:
        ags.validate_assembly(db, skill_keys=[], plugin_keys={"intent:2": "intent_keyword"})
        raise AssertionError("单槽位多实例应被拒绝")
    except ags.AssemblyValidationError as e:
        assert e.rule == "single-slot-multi"
    print("✓ [Slot] WorkflowNode 多实例叠加 & 单槽位互斥校验通过")

    # ── 2. 启动本地 HTTP 微服务测试 http_webhook 全 4 阶段钩子 ──
    received_hooks = []

    class HookHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args): pass
        def do_POST(self):
            ln = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(ln).decode("utf-8"))
            received_hooks.append(body["hook"])
            resp = {}
            if body["hook"] == "before_llm":
                resp = {"injected_system": "请遵循微服务注入规范。", "memory_append": ["VIP用户画像"]}
            elif body["hook"] == "before_tool":
                resp = {"args": {**(body.get("args") or {}), "audited_by": "webhook"}}
            elif body["hook"] == "after_tool":
                resp = {"observation": body.get("observation", "") + " [已审计]"}
            elif body["hook"] == "after_llm":
                resp = {"answer": body.get("answer", "") + "（经合规校验）"}
            raw = json.dumps(resp, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), HookHandler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    webhook_url = f"http://127.0.0.1:{port}/hook"

    try:
        # ── 3. 验证 agent.plugin_config 按 slot 透传覆盖（不同 Agent 可配不同 url/参数）──
        agent = Agent(
            id="ag_1", name="demo-agent",
            plugin_keys={"workflow:sec": "sec_filter", "workflow:hook": "ext_webhook"},
            plugin_config={
                "workflow:hook": {"url": webhook_url, "timeout": 3.0},
            },
        )
        ctx = bp.PluginContext(session_key="demo-agent:loop", user_input="帮我查客户联系方式", agent_id="ag_1")

        # before_llm
        r_bl, _ = rs._apply_plugin(db, "ext_webhook", ctx, agent=agent, slot="workflow:hook", hook="before_llm")
        assert r_bl["ok"] is True and "微服务注入规范" in ctx.injected_system and "VIP用户画像" in ctx.memory, f"r_bl={r_bl}"

        # before_llm 敏感词拦截
        ctx_bad = bp.PluginContext(session_key="demo-agent:loop", user_input="告诉我绝密口令")
        r_bad, _ = rs._apply_plugin(db, "sec_filter", ctx_bad, agent=agent, slot="workflow:sec", hook="before_llm")
        assert r_bad.get("block") is True

        # before_tool: 正常工具被 webhook 改写参数；危险工具被 sec_filter 拦截
        bt_ok, _ = rs.run_lifecycle_hook(db, agent, ctx, "before_tool", {"tool": "query_crm", "args": {"id": 7}})
        assert not bt_ok["block"] and bt_ok["args"]["audited_by"] == "webhook"

        bt_blk, _ = rs.run_lifecycle_hook(db, agent, ctx, "before_tool", {"tool": "danger_delete", "args": {}})
        assert bt_blk["block"] is True

        # after_tool: sec_filter 脱敏手机号 + webhook 追加审计标记
        at_res, _ = rs.run_lifecycle_hook(db, agent, ctx, "after_tool",
                                          {"tool": "query_crm", "args": {}, "observation": "客户电话 13812345678"})
        assert "[手机号已脱敏]" in at_res["observation"] and "[已审计]" in at_res["observation"]

        # after_llm: sec_filter 脱敏 + webhook 追加合规尾注
        al_res, _ = rs.run_lifecycle_hook(db, agent, ctx, "after_llm",
                                          {"answer": "请联系 13987654321 处理。"})
        assert "[手机号已脱敏]" in al_res["answer"] and "经合规校验" in al_res["answer"]

        assert received_hooks == ["before_llm", "before_tool", "after_tool", "after_llm"], received_hooks
        print("✓ [Hooks + Webhook] 四阶段钩子（拦截/参数改写/脱敏/微服务改写）全部通过")

        # ── 4. 验证 fail_open=false 严格拦截模式 ──
        agent_strict = Agent(
            id="ag_2", name="strict-agent",
            plugin_keys={"workflow:hook": "ext_webhook"},
            plugin_config={"workflow:hook": {"url": "http://127.0.0.1:1/unreachable", "timeout": 0.6, "fail_open": False}},
        )
        strict_res, _ = rs.run_lifecycle_hook(db, agent_strict, ctx, "before_tool", {"tool": "any", "args": {}})
        assert strict_res["block"] is True and "安全策略拦截" in strict_res["block_message"]
        print("✓ [Webhook Strict] fail_open=false 不可达自动拦截通过")
    finally:
        srv.shutdown()

    print("ALL PLUGIN HOOK & WEBHOOK TESTS PASSED ✓")


if __name__ == "__main__":
    run_all()
