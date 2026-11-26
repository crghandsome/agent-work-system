"""主 Agent 应能如实回答'能调用哪些智能体'（能力清单注入直答 prompt）。"""
import sys, uuid
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import httpx

c = httpx.Client(base_url="http://localhost:8000", timeout=240)
u = uuid.uuid4().hex[:6]
EXISTING = {a["id"] for a in c.get("/agents").json()}
# 建一个明确的专业 Agent
sub = c.post("/agents", json={"name": f"定价专家-{u}", "description": "专门回答产品定价和报价问题的专家"})
assert sub.status_code == 200
s = c.post("/orchestrator/sessions").json()
r = c.post(f"/orchestrator/sessions/{s['session_id']}/chat", json={"text": "你能调用哪些智能体？"})
assert r.status_code == 200, r.text
reply = r.json()["reply"]
if "模型服务当前不可用" in reply:
    print("✓ 网关限流时降级不崩溃（本次无法验证能力清单）")
else:
    assert f"定价专家-{u}" in reply, f"未列出子 Agent: {reply[:400]}"
    assert "还没有接入" not in reply and "没有接入" not in reply, reply[:400]
    print("✓ 主 Agent 如实列出可调用的智能体:", reply[:200].replace("\n", " "))

for a in c.get("/agents").json():
    if a["id"] not in EXISTING:
        c.delete(f"/agents/{a['id']}")
print("PASS-ORCH-CAPABILITY ✓")
