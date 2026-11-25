"""插件市场后端：简介/安装/启停/修改/卸载/GitHub/本地目录。"""
import sys, uuid, json, tempfile, pathlib
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import httpx

c = httpx.Client(base_url="http://localhost:8000", timeout=60)
u = uuid.uuid4().hex[:6]

# 1. 管理列表 + 内置插件简介
lst = c.get("/plugins/managed").json()
assert isinstance(lst, list) and len(lst) >= 4, lst
for p in lst:
    if p["builtin"]:
        assert p["description"], f"内置插件 {p['key']} 缺简介"
print("✓ 插件列表有功能简介:", [(p["key"], p["description"][:20]) for p in lst[:4]])

# 2. 从市场安装
key = f"market-plugin-{u}"
r = c.post("/plugins/install", json={
    "key": key, "name": "测试市场插件", "description": "用于验证市场安装的插件，能够快速处理特定任务",
    "extension_point": "WorkflowNode", "slot_semantics": "graph", "source": "market"})
r.raise_for_status()
assert r.json()["enabled"] is True
print("✓ 市场安装成功:", key)

# 3. 重复安装被拒
r2 = c.post("/plugins/install", json={"key": key, "name": "dup"})
assert r2.status_code == 409, r2.status_code
print("✓ 重复安装被拒")

# 4. 停用/启用
r = c.put(f"/plugins/{key}/enabled", json={"enabled": False}); r.raise_for_status()
assert r.json()["enabled"] is False
r = c.put(f"/plugins/{key}/enabled", json={"enabled": True}); r.raise_for_status()
print("✓ 停用/启用正常")

# 5. 修改用户插件
r = c.put(f"/plugins/{key}", json={"name": "改名插件", "description": "修改后的简介", "config": {"x": 1}})
r.raise_for_status()
assert r.json()["config"] == {"x": 1}
print("✓ 用户插件可修改")

# 6. 内置插件也可修改（删除用用户插件验证，避免破坏内置数据）
r = c.put("/plugins/memory_simple", json={"description": "临时改动的简介"})
assert r.status_code == 200, r.status_code
c.put("/plugins/memory_simple", json={"description": "短期会话记忆插件：按用户与会话隔离保存记忆条目，供 Agent 在对话中回忆历史上下文。"})
print("✓ 内置插件也可修改")

# 7. GitHub 来源安装
gh = f"github-plugin-{u}"
r = c.post("/plugins/install", json={"key": gh, "name": "GitHub 插件",
    "description": "从 GitHub 下载的插件", "extension_point": "IntentRecognizer",
    "source": "github", "source_url": "https://github.com/example/plugin"})
r.raise_for_status()
assert r.json()["source"] == "github"
print("✓ GitHub 来源安装成功")

# 8. 本地目录安装（bundle manifest）
d = pathlib.Path(tempfile.mkdtemp())
(d / "plugin.json").write_text(json.dumps({
    "key": f"local-plugin-{u}", "name": "本地插件", "description": "从本地目录安装的插件",
    "extension_point": "MemoryProvider", "slot_semantics": "single", "version": "2.1.0"}), encoding="utf-8")
r = c.post("/plugins/install-local", json={"directory": str(d)})
r.raise_for_status()
assert r.json()["source"] == "local" and r.json()["version"] == "2.1.0"
local_key = r.json()["key"]
print("✓ 本地目录安装成功:", local_key)

# 9. 卸载
for k in (key, gh, local_key):
    r = c.delete(f"/plugins/{k}")
    assert r.status_code == 200 and r.json()["ok"], (k, r.status_code)
assert all(p["key"] not in (key, gh, local_key) for p in c.get("/plugins/managed").json())
print("✓ 所有用户插件可卸载")

print("\nPLUGIN-MARKET BACKEND E2E PASSED ✓")
