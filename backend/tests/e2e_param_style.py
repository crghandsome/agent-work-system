"""验证自定义分块参数输入框：透明背景 + 原生调节箭头保留且可用。"""
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    pg = b.new_page(viewport={"width": 1500, "height": 1000})
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto("http://localhost:8900/console", wait_until="networkidle", timeout=30000)
    pg.locator('nav button[data-p="rag"]').first.click()
    pg.wait_for_selector("#doc-genre", timeout=15000)
    pg.locator("#doc-genre").select_option("custom")
    pg.wait_for_timeout(300)

    info = pg.evaluate("""() => {
      const e = document.querySelector('#doc-size');
      const spin = getComputedStyle(e, '::-webkit-inner-spin-button');
      return {inputBg: getComputedStyle(e).backgroundColor,
              spinBg: spin.backgroundColor, spinAppearance: spin.appearance,
              width: e.getBoundingClientRect().width};
    }""")
    assert info["inputBg"] in ("rgba(0, 0, 0, 0)", "transparent")
    assert info["spinBg"] in ("rgba(0, 0, 0, 0)", "transparent"), f"调节箭头背景不透明: {info}"
    assert info["spinAppearance"] == "auto", f"原生调节箭头被隐藏: {info}"
    assert abs(info["width"] - 110) < 3
    print("✓ 输入框与调节箭头均透明，原生箭头保留", info)

    # 调节功能可用（键盘上下键等价于点击箭头）
    pg.locator("#doc-size").fill("500")
    pg.locator("#doc-size").press("ArrowUp")
    assert pg.locator("#doc-size").input_value() == "501"
    pg.locator("#doc-size").press("ArrowDown")
    assert pg.locator("#doc-size").input_value() == "500"
    print("✓ 上/下调节功能正常")

    assert not errs, errs[:3]
    b.close()
print("PASS-PARAM-TRANSPARENT ✓")
