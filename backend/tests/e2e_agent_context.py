"""验证 ReAct Agent 有上下文：第二轮能利用第一轮的信息。"""
import sys, json
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import httpx

c = httpx.Client(base_url="http://localhost:8000", timeout=300)
for a in c.get("/agents").json():
    if a["name"] == "memory-test": c.delete("/agents/"+a["id"])
ag = c.post("/agents", json={"name": "memory-test", "description": "测试多轮上下文记忆"}).json()

# 第 1 轮：告诉它一个事实
r1 = c.post(f"/agents/{ag['id']}/playground", json={"input": "请记住：我的代号是紫水晶。简单回复收到即可。"}).json()
print("turn1:", r1["output_text"][:100])

# 第 2 轮：不带历史 → 应该不知道代号；带历史 → 应该答出"紫水晶"
hist = [
    {"role": "user", "content": "请记住：我的代号是紫水晶。简单回复收到即可。"},
    {"role": "assistant", "content": r1["output_text"]},
    {"role": "user", "content": "我的代号是什么？"},
]
r2 = c.post(f"/agents/{ag['id']}/playground", json={"input": "我的代号是什么？", "history": hist}).json()
reply2 = r2["output_text"]
print("turn2 (with history):", reply2[:150])
assert "紫水晶" in reply2, f"Agent 没记住代号: {reply2[:200]}"
print("✓ Agent 能利用历史上下文回答问题")

for a in c.get("/agents").json():
    if a["name"] == "memory-test": c.delete("/agents/"+a["id"])
print("PASS-AGENT-CONTEXT ✓")
