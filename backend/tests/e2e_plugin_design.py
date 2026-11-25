"""验证：在智能对话中开发插件（主 Agent 委托插件设计子 Agent）。"""
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import httpx

c = httpx.Client(base_url="http://localhost:8000", timeout=180)

# 1. 子 Agent 已注册
agents = c.get("/agents").json()
assert any(a["name"] == "plugin-designer" for a in agents), "插件设计子 Agent 未注册"
print("✓ 插件设计子 Agent 已注册")

# 2. 对话中提出插件开发需求
s = c.post("/orchestrator/sessions").json()
r = c.post(f"/orchestrator/sessions/{s['session_id']}/chat", json={
    "text": "帮我开发一个插件，用于识别用户输入里是否包含退款相关的关键词，命中就标记为退款意图"})
r.raise_for_status()
body = r.json()
reply = body["reply"]
print("✓ 主 Agent 回复:", reply[:160].replace("\n", " "))
assert "插件" in reply, reply

# 3. 插件已注册到市场
plugins = c.get("/plugins/managed").json()
made = [p for p in plugins if p["source"] == "agent_dialog"]
assert made, "插件未进入市场"
pl = made[-1]
print("✓ 生成插件:", pl["key"], "|", pl["description"][:40])
assert pl["extension_point"] in ("IntentRecognizer","MemoryProvider","ChunkingStrategy","ContextCompressor","WorkflowNode")
assert pl["mechanism"], "缺运行机制说明"
assert pl["enabled"] is True
print("   扩展点:", pl["extension_point"], "| 机制:", pl["mechanism"][:60])

# 4. 该插件可被 Agent 组装引用
a = c.post("/agents", json={"name": f"pd-agent-{pl['key']}",
    "plugin_keys": {"intent": pl["key"]}, "workflow": {"steps": ["intent", "llm"]}})
a.raise_for_status()
assert a.json()["plugin_keys"]["intent"] == pl["key"]
print("✓ 新插件可被 Agent 组装引用")

# 5. 运行时真正执行
run = c.post(f"/agents/{a.json()['id']}/runs", json={"input": "我要退款", "use_rag": False})
run.raise_for_status()
st = next((x for x in run.json()["steps"] if x["step"] == "intent"), None)
assert st and st.get("plugin") == pl["key"], run.json()["steps"]
print("✓ 运行时执行该插件:", st.get("result"))

# 清理
c.delete(f"/plugins/{pl['key']}")
print("\nPLUGIN-DESIGN E2E PASSED ✓")
