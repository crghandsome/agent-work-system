"""测试：知识库上传（预览→确认）+ 体裁指定 + 文件列表 + 查看完整内容。"""
import sys, uuid
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import httpx
from playwright.sync_api import sync_playwright

c = httpx.Client(base_url="http://localhost:8900", timeout=180)
# 快照：只清理本次测试新增的文档，保留用户已有文档
EXISTING_DOC_IDS = {d['id'] for d in c.get('/rag/documents').json()}
U = uuid.uuid4().hex[:6]
md_content = f"# 上传测试文档 {U}\n\n## 第一节\n\n这是第一节的完整内容，不会被截断。\n\n## 第二节\n\n这是第二节的完整内容。"
r = c.post("/rag/documents/upload", files={"file": (f"upload-{U}.md", md_content.encode(), "text/markdown")})
r.raise_for_status()
did = r.json()["id"]
assert r.json().get("genre"), "上传结果应含体裁"
print("✓ 后端上传成功:", r.json()["genre"])

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    pg = b.new_page(viewport={"width": 1500, "height": 1000})
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto("http://localhost:8900/console", wait_until="networkidle", timeout=30000)
    pg.locator('nav button[data-p="rag"]').first.click()
    pg.wait_for_selector("#doc-file-name", timeout=15000)
    pg.wait_for_timeout(800)

    # 1. 上传通道 + 体裁下拉
    assert pg.locator("#doc-file").count() == 1
    up_btn = pg.locator("button.icon-btn[aria-label='选择文件']").first
    assert up_btn.count() == 1 and up_btn.locator("svg").count() == 1
    assert pg.locator("#doc-genre option").count() >= 11
    print("✓ 上传图标 + 体裁下拉（11类）")

    # 2. 选文件 → 预览 → 识别提示
    pg.locator("#doc-file").set_input_files({"name": f"ui-{U}.md", "mimeType": "text/markdown",
                                              "buffer": f"# 合同\n\n第一条 甲方乙方约定。\n第二条 付款方式。".encode()})
    pg.locator("button", has_text="预览分块").click()
    pg.wait_for_selector("#doc-preview .card", timeout=15000)
    detect = pg.locator("#doc-detect").inner_text()
    assert "自动识别建议" in detect
    print("✓ 预览显示自动识别建议")
    # 手动指定体裁（覆盖自动识别）
    pg.locator("#doc-genre").select_option("legal")
    assert pg.locator("#doc-genre").input_value() == "legal"
    print("✓ 手动指定体裁优先")

    # 3. 确认入库
    pg.locator("button", has_text="确认入库").click()
    pg.wait_for_function("() => document.querySelector('#doc-msg')?.textContent?.includes('入库成功')", timeout=15000)
    print("✓ 确认入库成功")

    # 4. Windows 风格文件列表 + 完整内容
    pg.wait_for_selector(f"#doc-list .fe-row:has-text('upload-{U}')", timeout=5000)
    assert pg.locator("#doc-list .filetype").count() >= 1
    print("✓ Windows 风格文件列表")
    pg.locator("#doc-list .fe-row", has_text=f"upload-{U}").locator(".fe-name").click()
    pg.wait_for_selector("#doc-detail pre", timeout=10000)
    detail = pg.locator("#doc-detail pre").inner_text()
    assert "第一节的完整内容" in detail and "第二节的完整内容" in detail
    print("✓ 点击查看完整内容")
    assert not errs, errs[:3]
    b.close()

for d in c.get("/rag/documents").json():
    if d["id"] not in EXISTING_DOC_IDS:
        c.delete(f"/rag/documents/{d['id']}")
print("PASS-KB-UPLOAD ✓")
