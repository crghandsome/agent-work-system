"""验证 Playground 流式端点实时输出思考/工具/结果事件。"""
import sys, json
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import httpx

c = httpx.Client(base_url="http://localhost:8000", timeout=300)
a = c.post("/agents", json={"name": "stream-test-agent", "description": "测试流式输出"}).json()
events = []
with c.stream("POST", f"/agents/{a['id']}/playground/stream",
              json={"input": "你好，简单介绍一下你自己"}) as r:
    assert r.status_code == 200, r.status_code
    for line in r.iter_lines():
        if line.startswith("data:"):
            events.append(json.loads(line[5:]))

kinds = [e.get("type") for e in events]
print("event kinds:", kinds)
has_thought_or_content = any(e.get("type") in ("thought", "content") for e in events)
assert any(e.get("type") == "done" for e in events), kinds
assert has_thought_or_content, f"无思考/内容事件: {kinds}"
print("✓ Playground 流式端点输出思考与结果事件")
for e in events[:6]:
    print("  ", e.get("type"), str(e.get("text", e.get("output", "")))[:80])

c.delete("/agents/" + a["id"])
print("PASS-PLAYGROUND-STREAM ✓")
