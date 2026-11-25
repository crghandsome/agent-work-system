"""前端：检索测试展示混合检索信息。"""
import sys, uuid
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import httpx
from playwright.sync_api import sync_playwright

c = httpx.Client(base_url="http://localhost:8900", timeout=60)
# 快照：只清理本次测试新增的文档，保留用户已有文档
EXISTING_DOC_IDS = {d['id'] for d in c.get('/rag/documents').json()}
u = uuid.uuid4().hex[:6]
c.post("/rag/documents/upload", files={"file": (f"hybrid-{u}.md",
    "# 退款政策\n\n购买后7天内可无理由退款。".encode(), "text/markdown")})

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    pg = b.new_page(viewport={"width": 1500, "height": 1000})
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto("http://localhost:8900/console", wait_until="networkidle", timeout=30000)
    pg.locator('nav button[data-p="rag"]').first.click()
    pg.wait_for_selector("#q-in", timeout=15000)
    pg.locator("#q-in").fill("退款政策")
    pg.locator("button", has_text="检索").click()
    pg.wait_for_selector("#q-out .card", timeout=10000)
    txt = pg.locator("#q-out").inner_text()
    assert "混合检索" in txt and "阈值 0.5" in txt and "RRF" in txt and "BM25" in txt, txt[:300]
    print("✓ 检索测试展示 RRF/BM25/阈值信息")
    assert not errs, errs[:3]
    b.close()

for d in c.get("/rag/documents").json():
    if d["id"] not in EXISTING_DOC_IDS:
        c.delete(f"/rag/documents/{d['id']}")
print("PASS-HYBRID-UI ✓")
