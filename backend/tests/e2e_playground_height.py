"""测试：Playground 聊天框占满页面高度。"""
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    pg = b.new_page(viewport={"width": 1500, "height": 950})
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto("http://localhost:8900/console", wait_until="networkidle", timeout=30000)
    pg.locator('nav button[data-p="playground"]').first.click()
    pg.wait_for_selector("#pg-in", timeout=15000)
    pg.wait_for_timeout(600)

    geo = pg.evaluate("""() => {
      const shell = document.querySelector('.pg-shell').getBoundingClientRect();
      const page = document.querySelector('#main').getBoundingClientRect();
      const body = document.querySelector('#pg-body').getBoundingClientRect();
      const input = document.querySelector('.chat-input').getBoundingClientRect();
      return {shellH: shell.height, shellBottom: shell.bottom, pageBottom: page.bottom,
              bodyH: body.height, inputBottom: input.bottom};
    }""")
    # 聊天框几乎占满主区（shell 底部与页面底部接近）
    assert abs(geo["shellBottom"] - geo["pageBottom"]) < 30, f"聊天框未占满页面: {geo}"
    assert geo["shellH"] > 700, f"聊天框太矮: {geo}"
    assert geo["bodyH"] > 500, f"对话区太矮: {geo}"
    print("✓ Playground 聊天框占满页面高度", geo)

    assert not errs, errs[:3]
    b.close()
print("PASS-PLAYGROUND-FULLHEIGHT ✓")
