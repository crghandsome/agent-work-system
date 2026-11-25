"""前端功能测试 2：主题切换、导航、流式思考/委托渲染。

用法：python tests/e2e_frontend2.py [url]
"""
from __future__ import annotations

import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from playwright.sync_api import sync_playwright

URL = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8900/console"


def main() -> int:
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        pg = b.new_page(viewport={"width": 1440, "height": 900})
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.on("response", lambda r: errs.append(f"HTTP {r.status} {r.url}") if r.status >= 400 else None)
        pg.goto(URL, wait_until="networkidle", timeout=30000)
        pg.wait_for_timeout(1200)

        # 1. 主题切换：dark -> light -> 持久化
        assert pg.evaluate("document.documentElement.dataset.theme") in ("dark", "light")
        pg.locator("#themeBtn").click()
        pg.wait_for_timeout(300)
        t2 = pg.evaluate("document.documentElement.dataset.theme")
        print("✓ 主题切换 ->", t2)
        pg.reload(wait_until="networkidle")
        pg.wait_for_timeout(1000)
        assert pg.evaluate("document.documentElement.dataset.theme") == t2, "主题未持久化"
        print("✓ 主题刷新后持久化")

        # 2. 导航：Skill 市场 / Agent 市场 / 智能对话
        for target, marker in [("skills", "#sk-list"), ("agents", "#ag-list"), ("chat", "#orch-in")]:
            pg.locator(f'nav button[data-p="{target}"]').first.click()
            pg.wait_for_selector(marker, state="attached", timeout=10000)
            print(f"✓ 导航 {target} 正常")

        # 3. 发一条会触发流式+知识的问题
        pg.locator("#orch-in").fill("免费试用期是多久？")
        pg.locator("#orch-in").press("Enter")
        pg.wait_for_selector(".turn.ai .content", timeout=15000)
        pg.wait_for_function(
            "() => { const c = document.querySelector('.turn.ai:last-of-type .content');"
            " return c && c.textContent.trim().length > 5 && !c.querySelector('.cursor'); }",
            timeout=90000,
        )
        reply = pg.locator(".turn.ai .content").last.inner_text()
        print("✓ 知识问题回复:", reply[:100].replace("\n", " "))

        # 4. 思考块状态：完成后应存在（有思考）或隐藏（无思考），且默认收起
        think_count = pg.locator(".turn.ai:last-of-type details.think").count()
        if think_count:
            is_open = pg.locator(".turn.ai:last-of-type details.think").first.evaluate("e => e.open")
            assert not is_open, "回答完成后思考块应默认收起"
            print("✓ 思考块默认收起")
        else:
            print("✓ 本次无思考块（模型未返回思考链）")

        assert not errs, f"JS 错误: {errs[:3]}"
        print("✓ 无 JS 错误")
        b.close()
    print("\nFRONTEND E2E-2 PASSED ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
