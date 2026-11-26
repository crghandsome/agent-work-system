"""验证报告 Agent 的工具列表里包含 skill_script。"""
import sys
sys.path.insert(0, "/srv/app")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from app.db import get_session_factory
from app.models import Agent
from app.services import tool_registry as tr
from app.services import mcp_service

db = get_session_factory()()
mcp_service.register_all_enabled(db)
tr.init_builtin_tools()
names = [t["name"] for t in tr.list_tools()]
print("total tools:", len(names))
print("skill_script in tools:", "skill_script" in names)
assert "skill_script" in names
# 验证 skill_script 能真正执行报告脚本（--help 不取数）
a = db.query(Agent).filter(Agent.name == "建筑报告生成机器人").first()
print("agent skills:", a.skill_keys)
r = tr._skill_script(db, a, "daily-report-builder", "build_daily_report.py", "--help", "verify-report")
print("script result head:", r[:120].replace("\n", " "))
assert "usage:" in r
print("PASS-REPORT-TOOLS ✓")
db.close()
