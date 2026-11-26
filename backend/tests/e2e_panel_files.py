"""端到端：通过智能对话触发 skill_script 生成文件，验证文件进入会话历史（消息面板数据源）。"""
import sys, json, time
sys.path.insert(0, "/srv/app")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import httpx
from pathlib import Path

# 造一个测试 Skill 脚本
d = Path("/srv/data/artifacts/skills/daily-report-builder/daily-report-builder/scripts")
g = d / "_e2e_panel.py"
g.write_text("import sys,pathlib\nout=sys.argv[sys.argv.index('--out')+1]\npathlib.Path(out).write_bytes(b'PANELTEST')\nprint('done')\n", encoding="utf-8")

c = httpx.Client(base_url="http://localhost:8000", timeout=180)
s = c.post("/orchestrator/sessions").json()
sid = s["session_id"]

# 用确定性快速路径触发 skill_script（模拟用户说"用 skill_script 执行..."）
events = []
with c.stream("POST", f"/orchestrator/sessions/{sid}/chat/stream",
              json={"text": "用 skill_script 工具执行 daily-report-builder 的 _e2e_panel.py 脚本生成报告"}) as r:
    for line in r.iter_lines():
        if line.startswith("data:"):
            events.append(json.loads(line[5:]))

file_events = [e for e in events if e.get("type") == "file"]
done = [e for e in events if e.get("type") == "done"]
print("file events:", [(f.get("name"), f.get("url")) for f in file_events])
assert len(file_events) >= 1, f"无 file 事件: {[e.get('type') for e in events]}"

# 关键：会话历史（消息面板的数据源）必须带 files
hist = c.get(f"/orchestrator/sessions/{sid}").json().get("history", [])
assistant_msgs = [m for m in hist if m.get("role") == "assistant"]
print("history assistant files:", [len(m.get("files", [])) for m in assistant_msgs])
assert any(len(m.get("files", [])) >= 1 for m in assistant_msgs), "会话历史没有 files，消息面板无法显示"
print("✓ 文件进入会话历史，消息面板可显示下载按钮")

g.unlink()
import shutil
for f in Path("/srv/data/artifacts/outputs").rglob("daily-report-builder_result*"):
    f.unlink()
print("PASS-PANEL-FILES ✓")
