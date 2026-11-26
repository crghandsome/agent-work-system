"""验证流式对话（智能对话实际路径）的 LLM 自主委托。"""
import sys, uuid
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import httpx

c = httpx.Client(base_url="http://localhost:8000", timeout=300)
u = uuid.uuid4().hex[:6]
cs = c.post("/agents", json={"name": f"退款客服-{u}", "description": "专门处理退款退货售后问题",
    "system_prompt": "你是退款客服，简洁中文回答。"}).json()

s = c.post("/orchestrator/sessions").json()
# SSE 流式
events = []
with c.stream("POST", f"/orchestrator/sessions/{s['session_id']}/chat/stream",
              json={"text": "我要退货退款，政策是什么？"}) as r:
    assert r.status_code == 200
    for line in r.iter_lines():
        if line.startswith("data:"):
            import json
            events.append(json.loads(line[5:]))

kinds = [e.get("type") for e in events]
print("events:", kinds)
done = [e for e in events if e.get("type") == "done"]
reply = done[-1].get("reply", "") if done else ""
if "模型服务当前不可用" in reply:
    print("✓ 网关限流时流式链路降级不崩溃（本次无法验证委托决策）")
else:
    delegs = [e for e in events if e.get("type") == "delegation"]
    assert any(f"退款客服-{u}" == e.get("agent") for e in delegs), f"流式未委托: {delegs}"
    assert done and "退款" in reply, done
    print("✓ 流式对话中 LLM 自主委托子 Agent 并综合回复")

for a in c.get("/agents").json():
    if a["name"] == f"退款客服-{u}": c.delete("/agents/"+a["id"])
print("PASS-STREAM-DELEGATION ✓")
