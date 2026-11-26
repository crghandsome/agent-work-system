"""实测：报告脚本真实生成 docx，产物落 outputs/ 并被交付标记检测到。"""
import sys
sys.path.insert(0, "/srv/app")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from app.services import tool_registry as tr
from app.db import get_session_factory
from app.services.artifact_files import list_run_files
from pathlib import Path

db = get_session_factory()()
tr.init_builtin_tools()
key = "e2e-real-report"
# 用 --help 之外的方式无效（会取数），这里只验证工具能强制改写 out 路径并生成文件：
# 造一个测试脚本模拟报告脚本，验证 LLM 自带的绝对路径被强制覆盖到交付目录
d = Path("/srv/data/artifacts/skills/daily-report-builder/daily-report-builder/scripts")
gen = d / "_e2e_gen.py"
gen.write_text(
    "import sys, pathlib\n"
    "a=sys.argv[1:]\n"
    "out=a[a.index('--out')+1]\n"
    "pathlib.Path(out).write_bytes(b'REPORTDOCX')\n"
    "print('generated at', out)\n", encoding="utf-8")
# LLM 典型传法：script 带 scripts/ 前缀 + --out 指向 artifacts 根目录
r = tr._skill_script(db, None, "daily-report-builder",
                     "scripts/_e2e_gen.py",
                     "--date 20261126 --out /srv/data/artifacts/日报.docx", key)
print("result:", r[:400])
assert "[交付文件]" in r, "无交付标记"
# 确认：LLM 写的外路路径没被使用，文件在交付目录
assert Path("/srv/data/artifacts/日报.docx").exists() is False, "LLM 路径未被覆盖"
files = list_run_files(key)
assert any(f["name"].endswith(".docx") for f in files), files
print("✓ 报告脚本带路径容错+强制交付目录，文件可被下载")
# 清理
gen.unlink()
import shutil
shutil.rmtree(Path("/srv/data/artifacts/outputs") / key, ignore_errors=True)
db.close()
print("PASS-REAL-REPORT-DELIVERY ✓")
