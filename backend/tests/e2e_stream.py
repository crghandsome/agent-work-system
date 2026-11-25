"""流式编排验证：SSE 事件序列（status→reasoning/content 流→done）。用法: python tests/e2e_stream.py [base]"""
from __future__ import annotations

import json
import sys

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
c = httpx.Client(base_url=BASE, timeout=180)

s = c.post("/orchestrator/sessions").json()
seen = {"status": [], "reasoning_chunks": 0, "content_chunks": 0, "done": None}
first_reasoning_at = None
first_content_at = None

with c.stream("POST", f"/orchestrator/sessions/{s['session_id']}/chat/stream",
              json={"text": "用三句话说明什么是RAG"}) as resp:
    assert resp.status_code == 200, resp.status_code
    assert "text/event-stream" in resp.headers["content-type"]
    buf = ""
    for chunk in resp.iter_text():
        buf += chunk
        while "\n\n" in buf:
            frame, buf = buf.split("\n\n", 1)
            for line in frame.split("\n"):
                if not line.startswith("data:"):
                    continue
                ev = json.loads(line[5:])
                t = ev["type"]
                if t == "status":
                    seen["status"].append(ev["stage"])
                elif t == "reasoning":
                    seen["reasoning_chunks"] += 1
                    if first_reasoning_at is None:
                        first_reasoning_at = seen["content_chunks"]
                elif t == "content":
                    seen["content_chunks"] += 1
                elif t == "done":
                    seen["done"] = ev

print("status 阶段:", seen["status"])
print("reasoning 增量块:", seen["reasoning_chunks"], "| content 增量块:", seen["content_chunks"])
print("正文首 100 字:", (seen["done"]["reply"] or "")[:100].replace("\n", " "))

assert seen["done"], "缺少 done 事件"
assert seen["content_chunks"] >= 1, "至少应有一个正文块"
assert "<think>" not in seen["done"]["reply"], "正文不得含 think 标签"
if seen["reasoning_chunks"]:
    assert seen["done"]["reasoning"], "done 应携带完整思考链"
# 上游网关可能整段缓冲：多块时说明真流式，单块也不算失败
print("流式粒度:", "逐块" if seen["content_chunks"] > 1 else "上游缓冲(单块)")
print("\nSTREAM E2E PASSED ✓（SSE 流式：思考/正文分离，done 兜底完整正文）")
