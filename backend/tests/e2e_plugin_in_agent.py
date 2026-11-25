"""验证：用户安装的插件能被 Agent 组装并在运行时执行。"""
import sys, uuid
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import httpx

c = httpx.Client(base_url="http://localhost:8000", timeout=180)
u = uuid.uuid4().hex[:6]

# 1. 安装自定义意图插件（配置驱动，无代码）
k = f"custom-intent-{u}"
r = c.post("/plugins/install", json={"key": k, "name": "自定义意图插件",
    "description": "根据关键词识别意图", "extension_point": "IntentRecognizer", "source": "market"})
r.raise_for_status()
print("✓ 安装用户插件:", k)

# 2. 配置 behavior
r = c.put(f"/plugins/{k}", json={"config": {"behavior": "intent_keywords",
    "rules": {"greet": ["你好", "hi"], "help": ["帮", "怎么"]}}})
r.raise_for_status()
print("✓ 配置插件 behavior")

# 3. 组装 Agent 并选这个插件
r = c.post("/agents", json={"name": f"agent-{u}", "description": "测试 Agent",
    "plugin_keys": {"intent": k}, "workflow": {"steps": ["intent", "llm"]}})
r.raise_for_status()
agent = r.json()
pk = agent["plugin_keys"]
assert pk.get("intent") == k, pk
print("✓ Agent 成功引用用户插件")

# 4. 运行 Agent 验证插件执行
r = c.post(f"/agents/{agent['id']}/runs", json={"input": "你好，能帮我吗", "use_rag": False})
r.raise_for_status()
run = r.json()
assert run["status"] == "succeeded", run.get("error")
intent_step = next((s for s in run["steps"] if s["step"] == "intent"), None)
assert intent_step and intent_step.get("plugin") == k, run["steps"]
assert intent_step.get("builtin") is False, "应标记为用户插件"
result = intent_step.get("result") or {}
# MySQL JSON 列不保留键顺序，greet/help 谁先命中不确定；只验证结果来自用户配置的规则
assert result.get("intent") in ("greet", "help"), result
assert result.get("confidence", 0) >= 0.5, result
print("✓ 运行时执行用户插件，意图识别结果:", result)

# 5. 停用插件后 Agent 不应加载它
c.put(f"/plugins/{k}/enabled", json={"enabled": False})
r = c.post("/agents", json={"name": f"agent2-{u}", "plugin_keys": {"intent": k}})
assert r.status_code == 422, f"停用插件应无法组装: {r.status_code} {r.text}"
print("✓ 停用插件后无法被新 Agent 引用")

# 清理
c.delete(f"/plugins/{k}")
print("\nPLUGIN-IN-AGENT E2E PASSED ✓")
