"""后端：混合检索（向量+BM25/RRF/阈值0.5/top_k5）。"""
import sys, uuid
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import httpx

c = httpx.Client(base_url="http://localhost:8000", timeout=60)
# 快照：只清理本次测试新增的文档，保留用户已有文档
EXISTING_DOC_IDS = {d['id'] for d in c.get('/rag/documents').json()}
u = uuid.uuid4().hex[:6]
# 清空 + 上传明确主题文档
for d in c.get("/rag/documents").json():
    if d["id"] not in EXISTING_DOC_IDS:
        c.delete(f"/rag/documents/{d['id']}")
c.post("/rag/documents/upload", files={"file": (f"pricing-{u}.md",
    f"# 产品定价 {u}\n\n专业版每席位每月199元，试用期30天。\n\n## 退款政策\n\n购买后7天内可无理由退款。".encode(),
    "text/markdown")})

# 1. 相关查询召回
r = c.post("/rag/search", json={"content": "退款政策 几天可以退款"}).json()
assert r["hybrid"] is True and r["top_k"] == 5 and r["threshold"] == 0.5, r
assert len(r["hits"]) >= 1, "相关查询应召回"
h = r["hits"][0]
assert "relevance" in h and "bm25_score" in h and "score" in h
print("✓ 相关查询召回，含 RRF/双路分数:", {k: h[k] for k in ("relevance","score","bm25_score")})

# 2. 无关查询被阈值过滤
for q in ["今天的天气怎么样", "量子物理与相对论", "asdfghjkl 乱码"]:
    r2 = c.post("/rag/search", json={"content": q}).json()
    assert len(r2["hits"]) == 0, f"无关查询不应召回: {q} -> {r2['hits']}"
print("✓ 无关查询被阈值 0.5 过滤")

# 3. top_k 上限
r3 = c.post("/rag/search", json={"content": "产品定价 退款 试用"}).json()
assert len(r3["hits"]) <= 5
print("✓ top_k=5 生效")

for d in c.get("/rag/documents").json():
    if d["id"] not in EXISTING_DOC_IDS:
        c.delete(f"/rag/documents/{d['id']}")
print("\nHYBRID-SEARCH E2E PASSED ✓")
