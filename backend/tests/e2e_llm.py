"""LLM 验证：真实模型直答 + 网关不可用时诚实降级。用法: python tests/e2e_llm.py [base]"""
from __future__ import annotations

import sys

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
c = httpx.Client(base_url=BASE, timeout=180)

h = c.get("/health").json()
print("health:", h)
assert "mock" not in h["llm"], "LLM 未配置"

s = c.post("/orchestrator/sessions").json()
r1 = c.post(f"/orchestrator/sessions/{s['session_id']}/chat", json={"text": "你好，用一句话介绍你自己"}).json()
print("直答:", r1["reply"][:180].replace("\n", " "))
r2 = c.post(f"/orchestrator/sessions/{s['session_id']}/chat", json={"text": "帮我算一下 128 乘以 64 等于多少"}).json()
print("计算:", r2["reply"][:120].replace("\n", " "))
c.post("/rag/documents", json={"title": "LLM测试FAQ", "content": "测试文档免费试用期30天，专业版定价每席位每月199元。"})
r3 = c.post(f"/orchestrator/sessions/{s['session_id']}/chat", json={"text": "免费试用期是多久？"}).json()
print("知识直答:", r3["reply"][:160].replace("\n", " "))

for line in (r1["reply"], r2["reply"], r3["reply"]):
    assert "[mock-llm]" not in line, "mock 伪回答泄漏给用户"
availability = "模型服务当前不可用" not in r1["reply"]
print("网关可用:", availability)
if availability:
    assert not r1["delegations"], "通用问候不应委托"
    assert ("8192" in r2["reply"] or "八千" in r2["reply"]), "计算结果不正确"
else:
    for r in (r1, r2):
        assert "模型服务当前不可用" in r["reply"], "降级提示缺失"
print("\nLLM E2E PASSED ✓（真实模型或诚实降级提示）")
