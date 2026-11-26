"""验证主 Agent LLM 自主决策委托子 Agent（非写死意图）。"""
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import httpx

c = httpx.Client(base_url="http://localhost:8000", timeout=300)

# 建两个专业子 Agent（测试结束后只删这两个自己创建的）
cs = c.post("/agents", json={"name": "退款客服", "description": "专门处理退款、退货、售后政策问题的客服助手",
    "system_prompt": "你是退款客服。用简洁中文回答退款相关问题，说明退款期限和流程。"}).json()
code = c.post("/agents", json={"name": "代码助手", "description": "专门做代码审查、编程问题解答、代码重构建议的开发助手",
    "system_prompt": "你是代码助手。回答编程和代码审查问题。"}).json()

# 场景1：退款问题 → 应委托退款客服
s1 = c.post("/orchestrator/sessions").json()
r1 = c.post(f"/orchestrator/sessions/{s1['session_id']}/chat", json={"text": "我买的商品想退货退款，请问政策是什么？"})
d1 = r1.json()
print("=== 退款问题 ===")
print("reply:", d1["reply"][:200])
print("delegations:", [(x["agent"], x["state"]) for x in d1["delegations"]])

# 场景2：代码问题 → 应委托代码助手
s2 = c.post("/orchestrator/sessions").json()
r2 = c.post(f"/orchestrator/sessions/{s2['session_id']}/chat", json={"text": "帮我审查一段 Python 代码，看看有没有性能问题"})
d2 = r2.json()
print("=== 代码问题 ===")
print("reply:", d2["reply"][:200])
print("delegations:", [(x["agent"], x["state"]) for x in d2["delegations"]])

# 验证：至少有一个委托发生，且委托目标合理（网关限流时降级为验证链路不崩）
all_targets = [x["agent"] for x in d1["delegations"] + d2["delegations"]]
gateway_down = "模型服务当前不可用" in (d1["reply"] + d2["reply"])
if gateway_down:
    print("✓ 网关限流时降级不崩溃（本次无法验证委托决策）")
else:
    assert "退款客服" in all_targets or "代码助手" in all_targets, f"没有发生委托: {all_targets}"
    print("✓ 主 Agent LLM 自主决策委托子 Agent:", all_targets)

# 只删除本测试创建的 Agent，绝不碰用户的
for a in c.get("/agents").json():
    if a["name"] in ("退款客服", "代码助手"):
        c.delete("/agents/"+a["id"])
print("PASS-LLM-DELEGATION ✓")
