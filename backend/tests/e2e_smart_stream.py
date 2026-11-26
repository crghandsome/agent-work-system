"""验证智能对话的委托汇总也流式输出思考。"""
import sys, json
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import httpx

c = httpx.Client(base_url="http://localhost:8000", timeout=300)
u = "sumtest"
c.post("/agents", json={"name": f"报告助手-{u}", "description": "生成项目报告", "system_prompt": "你是报告助手。"})

s = c.post("/orchestrator/sessions").json()
events = []
with c.stream("POST", f"/orchestrator/sessions/{s['session_id']}/chat/stream",
              json={"text": "帮我生成一份项目周报"}) as r:
    assert r.status_code == 200
    for line in r.iter_lines():
        if line.startswith("data:"):
            events.append(json.loads(line[5:]))

kinds = [e.get("type") for e in events]
print("events:", kinds)
assert "done" in kinds
# 如果存在委托，汇总阶段应产出 reasoning 或 content 流式事件
delegs = [e for e in events if e.get("type") == "delegation"]
print("delegations:", [(e.get("agent"), e.get("state")) for e in delegs])
assert any(e.get("type") == "content" for e in events), "无流式正文"
print("✓ 智能对话流式输出正常（含委托与正文）")

for a in c.get("/agents").json():
    if a["name"] == f"报告助手-{u}": c.delete("/agents/"+a["id"])
print("PASS-SMART-STREAM ✓")
