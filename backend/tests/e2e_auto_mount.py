"""验证：插件按 extension_point 自动挂载，不再依赖 workflow.steps。"""
import sys, uuid
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import httpx

c = httpx.Client(base_url="http://localhost:8000", timeout=180)
u = uuid.uuid4().hex[:6]

def make_agent(name, pk, workflow=None):
    body = {"name": name, "plugin_keys": pk}
    if workflow is not None:
        body["workflow"] = workflow
    r = c.post("/agents", json=body)
    r.raise_for_status()
    return r.json()

def run(agent, text="你好"):
    r = c.post(f"/agents/{agent['id']}/runs", json={"input": text, "use_rag": False})
    r.raise_for_status()
    return r.json()

# 1. 装了意图插件 + 完全不写 workflow → 自动执行 intent
a1 = make_agent(f"auto-intent-{u}", {"intent": "intent_keyword"})
r1 = run(a1)
steps1 = [s["step"] for s in r1["steps"]]
assert "intent" in steps1, f"装了意图插件却没执行: {steps1}"
print("✓ 装了意图插件，无 workflow 也自动执行:", steps1)

# 2. 没装意图插件 → 不执行 intent
a2 = make_agent(f"auto-no-intent-{u}", {})
r2 = run(a2)
steps2 = [s["step"] for s in r2["steps"]]
assert "intent" not in steps2, f"没装意图插件却执行了: {steps2}"
print("✓ 没装意图插件，不执行:", steps2)

# 3. 装了 memory + intent + compress → 三个都自动执行，顺序正确
a3 = make_agent(f"auto-all-{u}", {
    "intent": "intent_keyword", "memory": "memory_simple", "compress": "compress_truncate"})
r3 = run(a3)
steps3 = [s["step"] for s in r3["steps"]]
assert "intent" in steps3 and "memory" in steps3 and "compress" in steps3, steps3
assert steps3.index("intent") < steps3.index("compress") < steps3.index("memory") < steps3.index("llm"), steps3
print("✓ 多插件自动按生命周期顺序执行:", steps3)

# 4. 用户自定义意图插件（extension_point=IntentRecognizer）也自动执行
key = f"auto-cust-intent-{u}"
c.post("/plugins/install", json={"key": key, "name": "自定义意图", "description": "测试自动挂载",
    "extension_point": "IntentRecognizer", "source": "market"})
c.put(f"/plugins/{key}", json={"config": {"behavior": "intent_keywords", "rules": {"urgent": ["急", "马上"]}}})
a4 = make_agent(f"auto-cust-agent-{u}", {"intent": key})
r4 = run(a4, "这事很急")
st4 = next((s for s in r4["steps"] if s["step"] == "intent"), None)
assert st4 and st4.get("plugin") == key, r4["steps"]
print("✓ 用户安装的意图插件也自动挂载:", st4.get("result"))
c.delete(f"/plugins/{key}")

# 5. workflow 仍可作为高级手动编排：声明只跑 llm → 插件不跑
a5 = make_agent(f"auto-wf-{u}", {"intent": "intent_keyword"}, {"steps": ["llm"]})
r5 = run(a5)
steps5 = [s["step"] for s in r5["steps"]]
assert "intent" not in steps5, f"workflow 手动编排失效: {steps5}"
print("✓ workflow.steps 高级编排仍生效:", steps5)

print("\nAUTO-MOUNT E2E PASSED ✓")
