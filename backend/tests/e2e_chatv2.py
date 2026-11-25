"""后端功能测试：新会话/历史列表/队列/中断/通用对话。用法: python tests/e2e_chatv2.py [base]"""
from __future__ import annotations

import json
import sys
import time

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
c = httpx.Client(base_url=BASE, timeout=180)

# 1. 创建会话
s1 = c.post("/orchestrator/sessions").json()
s2 = c.post("/orchestrator/sessions").json()
assert s1["session_id"] != s2["session_id"]
print("✓ 新增会话", s1["session_id"][:14])

# 2. 会话列表
lst = c.get("/orchestrator/sessions").json()
ids = [x["session_id"] for x in lst]
assert s1["session_id"] in ids and s2["session_id"] in ids
print("✓ 历史会话列表", len(lst), "个")

# 3. 流式对话（通用主 Agent，无委托）
sid = s1["session_id"]
events = []
with c.stream("POST", f"/orchestrator/sessions/{sid}/chat/stream", json={"text": "你好，简单介绍下你自己"}) as r:
    assert r.status_code == 200
    buf = ""
    for ch in r.iter_text():
        buf += ch
        while "\n\n" in buf:
            f, buf = buf.split("\n\n", 1)
            for line in f.split("\n"):
                if line.startswith("data:"):
                    events.append(json.loads(line[5:]))
types = [e["type"] for e in events]
print("✓ SSE 事件:", types[:8])
assert "queued" in types and "done" in types, "队列事件缺失"
done = next(e for e in events if e["type"] == "done")
assert done["delegations"] == [], "通用模式不应有团队委托"
assert done["reply"].strip(), "回复为空"
print("✓ 通用主 Agent 回复:", done["reply"][:60].replace("\n", " "))

# 4. 消息状态查询
mid = done["message_id"]
st = c.get(f"/orchestrator/messages/{mid}").json()
assert st["state"] == "done", st
print("✓ 消息队列状态:", st["state"])

# 5. 中断：排队消息取消
# 先提交一条长任务（拿 message_id），再提交第二条排队消息并取消
e1 = []
with c.stream("POST", f"/orchestrator/sessions/{sid}/chat/stream",
              json={"text": "详细解释RAG的原理，至少300字"}) as r:
    # 只读取第一个 queued 事件后断开连接（模拟中断）
    buf = ""
    for ch in r.iter_text():
        buf += ch
        if "message_id" in buf:
            f = buf.split("\n\n")[0]
            ev = json.loads([l for l in f.split("\n") if l.startswith("data:")][0][5:])
            if ev["type"] == "queued":
                mid2 = ev["message_id"]
                break
print("✓ 取到排队消息 id:", mid2[:14])
res = c.post(f"/orchestrator/messages/{mid2}/cancel").json()
assert res["ok"] is True, res
print("✓ 中断消息成功:", res["status"]["state"])
# 客户端断开后状态最终应为 canceled 或 failed（不残留 running）
c.post(f"/orchestrator/sessions/{sid}/cancel")
time.sleep(0.5)
final = c.get(f"/orchestrator/messages/{mid2}").json()
assert final["state"] in ("canceled", "failed", "done"), final
print("✓ 中断后状态已清理:", final["state"])

# 6. 历史持久化：会话详情包含刚完成的对话（一轮 = user + assistant = 2 条）
detail = c.get(f"/orchestrator/sessions/{sid}").json()
assert len(detail["history"]) >= 2, f"历史未持久化: {len(detail['history'])}"
assert any(m.get("role") == "assistant" and m.get("content", "").strip() for m in detail["history"]), "AI 回复未落库"
print("✓ 对话持久化:", len(detail["history"]), "条记录")

# 7. 删除会话
res = c.delete(f"/orchestrator/sessions/{s2['session_id']}").json()
assert res["ok"]
assert s2["session_id"] not in [x["session_id"] for x in c.get("/orchestrator/sessions").json()]
print("✓ 删除会话成功")

print("\nCHAT-V2 BACKEND E2E PASSED ✓")
