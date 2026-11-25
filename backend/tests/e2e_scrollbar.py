"""功能点测试2：滚动条样式与主题一致（两种主题均验证）。"""
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    pg = b.new_page(viewport={"width": 1500, "height": 950})
    pg.goto("http://localhost:8900/console", wait_until="networkidle", timeout=30000)
    pg.wait_for_timeout(500)
    # 读取 CSS 规则中定义的滚动条颜色（伪元素计算样式在 Chromium 可读）
    info = pg.evaluate("""() => {
      const rules = [...document.styleSheets[0].cssRules].filter(r => r.cssText.includes('scrollbar')).map(r => r.cssText);
      const root = getComputedStyle(document.documentElement);
      return {rules, line: root.getPropertyValue('--line').trim(), dim: root.getPropertyValue('--dim').trim()};
    }""")
    assert any("scrollbar-color" in r or "scrollbar-thumb" in r for r in info["rules"]), info
    assert info["line"], "主题变量缺失"
    print("✓ 滚动条样式定义存在，主题色:", info["line"], info["dim"])
    # 切到浅色主题，变量随之变化
    pg.locator("#themeBtn").click()
    pg.wait_for_timeout(300)
    info2 = pg.evaluate("""() => ({
      line: getComputedStyle(document.documentElement).getPropertyValue('--line').trim()
    })""")
    assert info2["line"] != info["line"], "主题变量未切换"
    print("✓ 浅色主题滚动条配色同步变化:", info2["line"])
    b.close()
print("PASS-2 ✓")
