"""端到端验证文件交付通道：脚本生成文件 → [交付文件]标记 → 下载 API 可取回。"""
import sys
sys.path.insert(0, "/srv/app")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import httpx
from pathlib import Path

# 1. 造一个会生成文件的测试 Skill 脚本
root = Path("/srv/data/artifacts/skills")
d = root / "file-gen-test"
(d / "scripts").mkdir(parents=True, exist_ok=True)
(d / "SKILL.md").write_text("---\nname: file-gen-test\ndescription: 测试\n---\n测试", encoding="utf-8")
script = d / "scripts" / "gen.py"
script.write_text(
    "import sys, pathlib\n"
    "out = sys.argv[sys.argv.index('--out')+1]\n"
    "pathlib.Path(out).write_bytes(b'docxtest')\n"
    "print('generated')\n", encoding="utf-8")

# 2. 用 skill_script 工具执行（指定 run_key）
from app.services import tool_registry
from app.db import get_session_factory
db = get_session_factory()()
result = tool_registry._skill_script(db, None, "file-gen-test", "gen.py", "", "e2e-delivery")
print("result:", result[:300])
assert "[交付文件]" in result, result
assert "file-gen-test.docx" in result, result
print("✓ 脚本执行返回交付文件标记")

# 3. 下载 API 可取回
c = httpx.Client(base_url="http://localhost:8000", timeout=30)
r = c.get("/files/e2e-delivery")
print("list:", r.json())
assert any(f["name"] == "file-gen-test.docx" for f in r.json())
r2 = c.get("/files/e2e-delivery/file-gen-test.docx")
assert r2.status_code == 200 and r2.content == b"docxtest", (r2.status_code, r2.content[:20])
print("✓ 下载 API 可取回生成的文件")

db.close()
print("PASS-FILE-DELIVERY ✓")
