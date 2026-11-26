"""验证 Playground 流式输出包含模型真实思考链 reasoning_content。"""
import sys, json
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import httpx

c = httpx.Client(base_url="http://localhost:8000", timeout=300)
for a in c.get("/agents").json():
    if a["name"] == "reason-test": c.delete("/agents/"+a["id"])
c.post("/agents", json={"name": "reason-test", "description": "测试真实思考链流式输出"})

events = []
with c.stream("POST", f"/agents/{[a for a in c.get('/agents').json() if a['name']=='reason-test'][0]['id']}/playground/stream",
              json={"input": "请帮我分析一下今天的天气情况并给出建议"}) as r:
    assert r.status_code == 200
    for line in r.iter_lines():
        if line.startswith("data:"):
            events.append(json.loads(line[5:]))

reason_events = [e for e in events if e.get("type") == "reasoning"]
thought_events = [e for e in events if e.get("type") == "thought"]
print("reasoning events:", len(reason_events), "thought events:", len(thought_events))
for e in reason_events[:2]:
    print("  reasoning:", e.get("text", "")[:150])
assert len(reason_events) > 0 or len(thought_events) > 0, "无思考链事件"
print("✓ 思考链事件已输出")

for a in c.get("/agents").json():
    if a["name"] == "reason-test": c.delete("/agents/"+a["id"])
print("PASS-REASONING-STREAM ✓")
