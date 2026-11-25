"""验证本地真实模型已接入：provider=tei、语义检索、重排、无关查询过滤。"""
import sys, uuid
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import httpx

c = httpx.Client(base_url="http://localhost:8000", timeout=180)
# 快照：只清理本次测试新增的文档，保留用户已有文档
EXISTING_DOC_IDS = {d['id'] for d in c.get('/rag/documents').json()}
u = uuid.uuid4().hex[:6]

# 1. 确认真实模型已启用
h = c.get("/health").json()
assert h["embedding"] == "tei" and h["rerank"] == "tei", f"未启用本地模型: {h}"
print("✓ 已启用本地模型 provider (tei/tei):", h)

# 2. 上传语义各异的文档
for d in c.get("/rag/documents").json():
    if d["id"] not in EXISTING_DOC_IDS:
        c.delete(f"/rag/documents/{d['id']}")
docs = [
    (f"product-{u}.md", "# 产品定价\n\n专业版每席位每月199元，试用期30天。企业版联系销售获取报价。"),
    (f"refund-{u}.md", "# 退款政策\n\n购买后7天内可无理由退款，退款将原路返回。"),
    (f"support-{u}.md", "# 技术支持\n\n技术支持工作时间每天9点到21点，可通过邮件联系我们。"),
]
for n, content in docs:
    r = c.post("/rag/documents/upload", files={"file": (n, content.encode(), "text/markdown")})
    assert r.status_code == 200
print("✓ 上传 3 篇语义各异的文档")

# 3. 语义查询（故意不用文档原词）
cases = [
    ("多少钱一个月", "product"),
    ("不想要了能退钱吗", "refund"),
    ("客服什么时候在线", "support"),
]
for q, expect in cases:
    r = c.post("/rag/search", json={"content": q}).json()
    assert r["reranked"] is True, "应经过真实重排"
    assert r["hits"], f"相关查询无结果: {q}"
    assert expect in r["hits"][0]["title"], f"语义召回错误: {q} -> {r['hits'][0]['title']}"
    print(f"✓ 语义召回 '{q}' → {r['hits'][0]['title']} (rel={r['hits'][0]['relevance']})")

# 4. 无关查询被过滤
for q in ["今天天气怎么样", "量子物理与相对论"]:
    r = c.post("/rag/search", json={"content": q}).json()
    assert not r["hits"], f"无关查询不应召回: {q} -> {r['hits']}"
print("✓ 无关查询被阈值过滤")

for d in c.get("/rag/documents").json():
    if d["id"] not in EXISTING_DOC_IDS:
        c.delete(f"/rag/documents/{d['id']}")
print("\nLOCAL-MODEL-RAG E2E PASSED ✓")
