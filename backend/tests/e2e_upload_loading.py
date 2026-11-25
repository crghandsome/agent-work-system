"""验证确认入库时显示转圈加载符。"""
import sys, uuid
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import httpx
from playwright.sync_api import sync_playwright

c = httpx.Client(base_url="http://localhost:8900", timeout=60)
# 快照：只清理本次测试新增的文档，保留用户已有文档
EXISTING_DOC_IDS = {d['id'] for d in c.get('/rag/documents').json()}
U = uuid.uuid4().hex[:6]

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    pg = b.new_page(viewport={"width":1500,"height":1000})
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto("http://localhost:8900/console", wait_until="networkidle", timeout=30000)
    pg.locator('nav button[data-p="rag"]').first.click()
    pg.wait_for_selector("#doc-genre", timeout=15000)

    # 选文件 → 预览
    pg.locator("#doc-file").set_input_files({"name": f"load-{U}.md", "mimeType":"text/markdown",
        "buffer": "# 产品定价\n\n专业版每席位每月199元。".encode()})
    pg.locator("button", has_text="预览分块").click()
    pg.wait_for_selector("#doc-preview .card", timeout=15000)

    # 延迟 upload 响应，观察转圈
    def delay_route(route):
        import time; time.sleep(1.5)
        route.continue_()
    pg.route("**/rag/documents/upload", delay_route)

    btn = pg.locator("button", has_text="确认入库").first
    btn.click()
    # 入库中：转圈符出现 + 按钮禁用
    pg.wait_for_selector("#doc-msg .spinner", timeout=5000)
    assert "正在入库" in pg.locator("#doc-msg").inner_text()
    assert btn.is_disabled()
    print("✓ 入库中显示转圈符并禁用按钮")

    # 入库完成：转圈消失，成功提示
    pg.wait_for_function("() => document.querySelector('#doc-msg')?.textContent?.includes('入库成功')", timeout=20000)
    assert pg.locator("#doc-msg .spinner").count() == 0
    print("✓ 入库完成后转圈消失")

    assert not errs, errs[:3]
    b.close()

for d in c.get("/rag/documents").json():
    if d["id"] not in EXISTING_DOC_IDS:
        c.delete(f"/rag/documents/{d['id']}")
print("PASS-UPLOAD-LOADING ✓")
