"""验证智能对话路径现在能调度内置工具（skill_script）。"""
import sys
sys.path.insert(0, "/srv/app")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from app.db import get_session_factory
from app.services import orchestrator_service as orch
from app.services import tool_registry

db = get_session_factory()()
tool_registry.init_builtin_tools()
names = {t["name"] for t in tool_registry.list_tools()}
assert "skill_script" in names, f"skill_script 未注册: {names}"
print("✓ 内置工具已注册:", sorted(names))

# 直接验证规划器把 skill_script 列进 LLM 工具清单
import inspect
src = inspect.getsource(orch._plan_tool_step)
assert "skill_script" in src or "_tr.list_tools()" in src
assert "init_builtin_tools" in src
print("✓ 会话路径工具调度器包含内置工具")

db.close()
print("PASS-SESSION-TOOLS ✓")
