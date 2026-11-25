"""验证分块预览中文不乱码（UTF-8 与 GBK 编码）。"""
import sys, uuid
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from playwright.sync_api import sync_playwright

U = uuid.uuid4().hex[:6]
utf8 = "# 产品定价\n\n专业版每席位每月199元，试用期30天。\n\n## 退款\n\n7天内可退款。"

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    pg = b.new_page(viewport={"width":1500,"height":1000})
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto("http://localhost:8900/console", wait_until="networkidle", timeout=30000)
    pg.locator('nav button[data-p="rag"]').first.click()
    pg.wait_for_selector("#doc-genre", timeout=15000)

    for enc_name, data in [("utf8", utf8.encode("utf-8")), ("gbk", utf8.encode("gbk"))]:
        pg.locator("#doc-file").set_input_files({"name": f"预览-{enc_name}-{U}.md",
            "mimeType":"text/markdown", "buffer": data})
        pg.locator("button", has_text="预览分块").click()
        pg.wait_for_selector("#doc-preview .card", timeout=15000)
        txt = pg.locator("#doc-preview").inner_text()
        assert "产品定价" in txt and "199元" in txt and "退款" in txt, f"{enc_name} 乱码: {txt[:200]}"
        assert "\ufffd" not in txt, f"{enc_name} 含替换字符: {txt[:200]}"
        print(f"✓ {enc_name.upper()} 编码预览中文正常")

    assert not errs, errs[:3]
    b.close()
print("PASS-PREVIEW-ENCODING ✓")
