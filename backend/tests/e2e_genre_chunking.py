"""体裁驱动分块：识别、边界、手动优先、自定义参数、预览。"""
import sys, uuid
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import httpx

c = httpx.Client(base_url="http://localhost:8000", timeout=240)
# 快照：只清理本次测试新增的文档，保留用户已有文档
EXISTING_DOC_IDS = {d['id'] for d in c.get('/rag/documents').json()}
u = uuid.uuid4().hex[:6]
for d in c.get("/rag/documents").json():
    if d["id"] not in EXISTING_DOC_IDS:
        c.delete(f"/rag/documents/{d['id']}")

# 1. 预览：体裁识别建议 + 分块
contract = "甲方ABC 乙方XYZ。\n第一条 服务内容\n乙方提供服务。\n第二条 付款\n甲方付款100元。"
r = c.post("/rag/documents/preview", files={"file": (f"合同-{u}.md", contract.encode(), "text/markdown")},
           data={"chunk_size": 500, "overlap": 50}).json()
assert r["suggestion"]["genre"] == "contract", r["suggestion"]
assert r["chunks"] and all(x["locator"].get("clause") or x["kind"] == "paragraph" for x in r["chunks"]), r["chunks"]
print("✓ 预览：合同识别为 contract，按条款切分", [(x["locator"], x["kind"]) for x in r["chunks"]])

# 2. FAQ 不拆问答对
faq = "问：怎么退款？\n答：7天内退。\n问：怎么联系？\n答：发邮件。"
r2 = c.post("/rag/documents/preview", files={"file": (f"faq-{u}.md", faq.encode(), "text/markdown")}).json()
assert r2["suggestion"]["genre"] == "faq", r2["suggestion"]
assert len(r2["chunks"]) == 2 and all("问：" in x["text"] and "答：" in x["text"] for x in r2["chunks"]), r2["chunks"]
print("✓ FAQ 识别正确且问答对不被拆散")

# 3. 用户手动指定优先于自动识别
r3 = c.post("/rag/documents/preview", files={"file": (f"合同-{u}.md", contract.encode(), "text/markdown")},
            data={"genre": "faq"}).json()
assert r3["genre"] == "faq" and r3["suggestion"]["genre"] == "contract", r3
print("✓ 用户手动指定优先于自动识别建议")

# 4. 自定义参数 chunk_size/overlap
long = "内容" * 300
r4 = c.post("/rag/documents/preview", files={"file": (f"自定义-{u}.txt", long.encode(), "text/plain")},
            data={"genre": "custom", "chunk_size": 100, "overlap": 20}).json()
assert r4["chunk_count"] > 2 and len(r4["chunks"][0]["text"]) == 100, r4["chunk_count"]
print(f"✓ 自定义参数：chunk_size=100 → {r4['chunk_count']} 块，首块 {len(r4['chunks'][0]['text'])} 字")

# 5. 上传入库：手动指定体裁生效
r5 = c.post("/rag/documents/upload", files={"file": (f"合同-{u}.md", contract.encode(), "text/markdown")},
            data={"genre": "contract", "chunk_size": "500", "overlap": "50"})
assert r5.status_code == 200 and r5.json()["genre"] == "contract" and r5.json()["genre_source"] == "user", r5.text
print("✓ 上传入库：手动体裁与分块参数生效")

for d in c.get("/rag/documents").json():
    if d["id"] not in EXISTING_DOC_IDS:
        c.delete(f"/rag/documents/{d['id']}")
print("\nGENRE-CHUNKING E2E PASSED ✓")
