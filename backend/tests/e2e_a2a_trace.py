"""后端：A2A 调用链查询（智能对话产生 task → 按 trace 查树）。"""
import sys, uuid
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import httpx

c = httpx.Client(base_url="http://localhost:8000", timeout=180)
u = uuid.uuid4().hex[:6]
leader, member = f"tr-leader-{u}", f"tr-member-{u}"
for n in (leader, member):
    c.post("/agents", json={"name": n, "description": f"调用链测试 {n}"})
tn = f"tr-team-{u}"
c.post("/agents/teams", json={"name": tn, "description": "调用链测试团队", "leader": leader, "members": [member]})

# 触发团队调用（产生 A2A tasks）
s = c.post("/orchestrator/sessions").json()
r = c.post(f"/orchestrator/sessions/{s['session_id']}/chat", json={"text": f"请让团队 {tn} 处理任务"})
r.raise_for_status()
trace_id = r.json()["trace_id"]
print("✓ 对话产生 trace:", trace_id)

# 调用链详情
r = c.get(f"/a2a/trace/{trace_id}")
r.raise_for_status()
d = r.json()
assert d["trace_id"] == trace_id and d["total"] >= 1, d
agents = set()
def walk(ns):
    for n in ns:
        if n["agent"]: agents.add(n["agent"])
        walk(n.get("children", []))
walk(d["tree"])
assert leader in agents, f"调用链缺主理: {agents}"
assert member in agents, f"调用链缺成员: {agents}"
print("✓ 调用链包含主理与成员:", agents)

# 链列表
lst = c.get("/a2a/traces?limit=5").json()
assert any(x["trace_id"] == trace_id for x in lst)
print("✓ 调用链列表")

c.delete(f"/agents/teams/{tn}")
for n in (leader, member):
    for a in c.get("/agents").json():
        if a["name"] == n: c.delete(f"/agents/{a['id']}")
print("\nA2A-TRACE E2E PASSED ✓")
