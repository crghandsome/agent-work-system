"""编排对话验证（v2：通用主 Agent，无团队委托）。用法: python tests/e2e_orch.py [base]

注：团队 Agent 的组装接口仍保留，但智能对话已改为通用主 Agent；
本测试验证新模式下会话创建/通用直答/无委托。
"""
from __future__ import annotations

import sys
import uuid

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
c = httpx.Client(base_url=BASE, timeout=180)
uniq = uuid.uuid4().hex[:6]

# 1. 会话创建（不再需要 with-agent）
s = c.post("/orchestrator/sessions").json()
assert s["session_id"] and "title" in s, s
print("✓ 通用会话创建:", s["session_id"][:14])

# 2. 通用直答（无委托）
r = c.post(f"/orchestrator/sessions/{s['session_id']}/chat", json={"text": "你好，你是谁"}).json()
assert r["reply"].strip(), "回复为空"
assert r["delegations"] == [], "通用模式不应有委托"
print("✓ 通用主 Agent 直答，无团队委托")

# 3. 会话历史
h = c.get(f"/orchestrator/sessions/{s['session_id']}").json()
assert len(h["history"]) >= 2, h
print("✓ 历史持久化")

# 4. 流式对话仍可用
events = []
with c.stream("POST", f"/orchestrator/sessions/{s['session_id']}/chat/stream",
              json={"text": "1+1等于几"}) as resp:
    assert resp.status_code == 200
    buf = ""
    for ch in resp.iter_text():
        buf += ch
        while "\n\n" in buf:
            f, buf = buf.split("\n\n", 1)
            for line in f.split("\n"):
                if line.startswith("data:"):
                    events.append(__import__("json").loads(line[5:]))
assert any(e["type"] == "done" for e in events), [e["type"] for e in events]
print("✓ 流式对话正常")

print("\nORCH E2E PASSED ✓（通用主 Agent 模式）")
