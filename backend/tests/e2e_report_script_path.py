"""验证日报脚本能找到同级 wage-pay-judge 并 import 取数函数。"""
import sys, subprocess, os
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

script = "/srv/data/artifacts/skills/daily-report-builder/daily-report-builder/scripts/build_daily_report.py"
# 用 --help 触发模块加载，验证路径解析不再 SystemExit
r = subprocess.run(["python", script, "--help"], capture_output=True, text=True, timeout=30)
print("returncode:", r.returncode)
print("stderr head:", r.stderr[:300])
assert r.returncode == 0, r.stderr[:500]
assert "usage:" in r.stdout
print("✓ 日报脚本加载成功，未报『找不到 wage-pay-judge 数据源目录』")

# 直接验证 import datasource_qxb 可用
code = "import sys; sys.path.insert(0,'/srv/data/artifacts/skills/wage-pay-judge/wage-pay-judge/scripts'); import datasource_qxb; print('QxbMcpDataSource:', hasattr(datasource_qxb,'QxbMcpDataSource'))"
r2 = subprocess.run(["python", "-c", code], capture_output=True, text=True, timeout=30)
print(r2.stdout.strip(), r2.stderr[:200])
assert "True" in r2.stdout
print("PASS-REPORT-SCRIPT-PATH ✓")
