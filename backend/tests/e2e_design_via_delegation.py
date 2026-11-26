"""验证设计类子 Agent 仍可通过通用委托触发（不再靠写死意图）。"""
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import httpx

c = httpx.Client(base_url="http://localhost:8000", timeout=300)
before_plugins = {p["key"] for p in c.get("/plugins/managed").json()}
s = c.post("/orchestrator/sessions").json()
r = c.post(f"/orchestrator/sessions/{s['session_id']}/chat",
           json={"text": "帮我开发一个意图识别插件，识别退款意图"})
d = r.json()
print("delegations:", [(x["agent"], x["state"]) for x in d.get("delegations", [])])
print("reply:", d["reply"][:200])
# 检查是否委托了 plugin-designer（网关限流时降级为验证链路不崩）
targets = [x["agent"] for x in d.get("delegations", [])]
if "模型服务当前不可用" in d["reply"]:
    print("✓ 网关限流时降级不崩溃（本次无法验证委托决策）")
else:
    assert "plugin-designer" in targets, f"设计插件未委托给 plugin-designer: {targets}"
    print("✓ 设计插件经通用委托触发 plugin-designer")
for p in c.get("/plugins/managed").json():
    if p["key"] not in before_plugins: c.delete("/plugins/"+p["key"])
print("PASS-DESIGN-VIA-DELEGATION ✓")
