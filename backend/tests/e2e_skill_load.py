"""验证：装配 Skill 的 Agent 能加载完整 Skill 指令并执行其脚本。"""
import sys
sys.path.insert(0, "/srv/app")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from app.db import get_session_factory
from app.models import Agent
from app.services.agent_loop import _load_skill_context
from app.services import tool_registry

db = get_session_factory()()
a = db.query(Agent).filter(Agent.name == "construction_reporter").first()
assert a, "请先创建 construction_reporter Agent"

# 1. Skill 完整指令被加载（不是只有标题/描述）
ctx = _load_skill_context(db, a)
assert len(ctx) > 10000, f"Skill 上下文过短，可能未加载完整: {len(ctx)}"
assert "GB/T 9704" in ctx and "qxb" in ctx
assert all(k in ctx for k in (a.skill_keys or []))
print("✓ Skill 完整指令已加载（含步骤/数据源），长度:", len(ctx))

# 2. Skill 内脚本可定位并执行
tool_registry.init_builtin_tools()
r = tool_registry._skill_script(db, a, "daily-report-builder", "build_daily_report.py", "--help")
assert "usage:" in r and "劳资日报" in r, r[:300]
print("✓ Skill 脚本可执行（--help 正常）")

db.close()
print("PASS-SKILL-LOAD ✓")
