"""验证配插件的 Agent 同时具备插件基础能力与 ReAct 自主工具调用（不互斥）。"""
import sys, uuid
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import httpx

c = httpx.Client(base_url="http://localhost:8000", timeout=300)
u = uuid.uuid4().hex[:6]
c.post("/rag/documents/upload", files={"file": (f"kb-{u}.md",
  "# 退款政策\n\n30天内可无理由退款。".encode(), "text/markdown")})
# 配了插件的 Agent：intent + memory + compress
a = c.post("/agents", json={
    "name": f"both-{u}", "description": "客服助手",
    "plugin_keys": {"intent": "intent_keyword", "memory": "memory_simple", "compress": "compress_truncate"},
}).json()

r = c.post(f"/agents/{a['id']}/playground", json={"input": "退款政策是什么"})
assert r.status_code == 200, r.text
d = r.json()
assert d["status"] == "succeeded", d
steps = {s["step"] for s in d["steps"]}
# 1. 插件基础能力仍执行（前置步骤）
assert "intent" in steps and "memory" in steps and "compress" in steps, f"插件步骤缺失: {steps}"
print("✓ 插件基础能力仍执行:", [s for s in steps if s in ("intent", "memory", "compress")])
# 2. 自主工具能力：网关可用时应有 ReAct 决策步骤；限流降级时不崩即可
all_actions = [s.get("action") for s in d["steps"]]
if "fallback" not in all_actions:
    has_react = any(a in ("knowledge_search", "skill_script", "a2a_delegate", "final") for a in all_actions)
    assert has_react, f"自主工具能力缺失: {d['steps']}"
    print("✓ ReAct 自主能力仍在:", all_actions)
else:
    print("✓ 网关降级时插件基础能力仍执行，链路不崩")

for doc in c.get("/rag/documents").json(): c.delete("/rag/documents/"+doc["id"])
for ag in c.get("/agents").json():
    if u in ag["name"]: c.delete("/agents/"+ag["id"])
print("PASS-PLUGIN-PLUS-REACT ✓")
