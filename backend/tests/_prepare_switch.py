"""在容器内准备一个带交付文件历史的会话，sid 写到共享文件供前端测试读取。"""
import sys
sys.path.insert(0, "/srv/app")
from pathlib import Path
from app.db import get_session_factory
from app.models import OrchestratorSession, new_id

db = get_session_factory()()
s = OrchestratorSession(id=new_id("orch"), agent_id="", trace_id=new_id("trace"))
s.title = "文件恢复测试"
s.history = [
    {"role": "user", "content": "帮我生成日报"},
    {"role": "assistant", "content": "日报已生成，请下载。",
     "reasoning": "用户要日报，我用脚本生成了文件",
     "files": [{"name": "日报.docx", "url": "/files/restore-test/日报.docx", "size": 12345}]},
]
db.add(s)
db.commit()
sid = s.id
db.close()
Path("/srv/app/tests/_sid.txt").write_text(sid, encoding="utf-8")
print("prepared", sid)
