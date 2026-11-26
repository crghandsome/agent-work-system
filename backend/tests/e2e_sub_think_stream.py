"""验证委托时子 Agent 的实时思考事件透传到主对话 SSE（phase=sub）。"""
import sys, json
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import httpx

c = httpx.Client(base_url="http://localhost:8000", timeout=300)
# 建一个专业子 Agent
ag = c.post("/agents", json={
    "name": "sub-think-test",
    "description": "专门回答技术问题的工程师助手",
    "system_prompt": "你是工程师，回答技术问题时要先分析再回答。",
}).json()

s = c.post("/orchestrator/sessions").json()
events = []
with c.stream("POST", f"/orchestrator/sessions/{s['session_id']}/chat/stream",
              json={"text": "帮我分析一下 Python 里列表和元组的区别，让工程师来回答"}) as r:
    assert r.status_code == 200
    for line in r.iter_lines():
        if line.startswith("data:"):
            events.append(json.loads(line[5:]))

sub_events = [e for e in events if e.get("phase") == "sub"]
print("total events:", len(events), "sub events:", len(sub_events))
for e in sub_events[:5]:
    print("  sub:", e.get("type"), e.get("sub_agent"), str(e.get("text", e.get("delta","")))[:60])
delegs = [e for e in events if e.get("type") == "delegation"]
print("delegations:", [(e.get("agent"), e.get("state")) for e in delegs])
assert any(e.get("type") == "delegation" for e in events), "未发生委托"
if delegs:
    assert len(sub_events) > 0, "委托发生了但没有子 Agent 实时事件"
    print("✓ 子 Agent 实时事件已透传")
else:
    print("(本次 LLM 未选择委托，委托机制已在其它测试验证)")

for a in c.get("/agents").json():
    if a["name"] == "sub-think-test": c.delete("/agents/"+a["id"])
print("PASS-SUB-THINK-STREAM ✓")
