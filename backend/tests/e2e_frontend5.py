"""前端功能测试 5：A2A 通信 / 知识库 / 计量审计页。

用法：python tests/e2e_frontend5.py [url]
"""
from __future__ import annotations

import sys
import uuid

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from playwright.sync_api import sync_playwright

URL = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8900/console"
U = uuid.uuid4().hex[:6]


def main() -> int:
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        pg = b.new_page(viewport={"width": 1440, "height": 1000})
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.on("response", lambda r: errs.append(f"HTTP {r.status} {r.url}") if r.status >= 400 else None)
        pg.goto(URL, wait_until="networkidle", timeout=30000)

        # 1. 知识库：入库 + 检索
        pg.locator('nav button[data-p="rag"]').first.click()
        pg.wait_for_selector("#doc-c", timeout=10000)
        pg.locator("#doc-t").fill(f"UI测试文档-{U}")
        pg.locator("#doc-c").fill(f"测试内容：产品代码 {U}，试用期三十天，价格一百九十九元。")
        pg.get_by_role("button", name="入库（自动分块+向量化）").click()
        pg.wait_for_function("() => document.querySelector('#doc-msg')?.textContent?.includes('入库成功')", timeout=30000)
        print("✓ 知识库入库成功")
        pg.locator("#q-in").fill("试用期")
        pg.get_by_role("button", name="检索").click()
        pg.wait_for_function("() => document.querySelector('#q-out')?.textContent?.includes('score')", timeout=30000)
        print("✓ 知识库检索成功")

        # 2. A2A：Peer 注册 + 任务
        pg.locator('nav button[data-p="a2a"]').first.click()
        pg.wait_for_selector("#pe-n", timeout=10000)
        pg.locator("#pe-n").fill(f"ui-peer-{U}")
        pg.locator("#pe-u").fill("https://peer.example.com")
        pg.get_by_role("button", name="注册").click()
        pg.wait_for_function("() => document.querySelector('#main')?.textContent?.includes('ui-peer-')", timeout=15000)
        print("✓ A2A Peer 注册成功")

        # 3. 内部任务（需一个 Agent）—— 用 API 数据，不依赖页面渲染
        import httpx
        agents = httpx.get("http://localhost:8900/agents", timeout=30).json()
        target = agents[0]["name"] if agents else "faq-bot"
        if pg.locator("#tk-ag").count() == 0:
            pg.locator('nav button[data-p="a2a"]').first.click()
            pg.wait_for_selector("#tk-ag", timeout=10000)
            pg.wait_for_timeout(1000)
        pg.locator("#tk-ag").fill(target)
        pg.locator("#tk-msg").fill("你好")
        send_btns = pg.get_by_role("button", name="发送")
        if send_btns.count() != 1:
            print("DEBUG send buttons:", send_btns.count())
        send_btns.first.click()
        pg.wait_for_timeout(1000)
        if pg.locator("#tk-result").count() == 0 or not pg.locator("#tk-result").inner_text().strip():
            print("DEBUG tk-result:", pg.locator("#tk-result").count(), "currently at:",
                  pg.locator("h2.vtitle").first.inner_text() if pg.locator("h2.vtitle").count() else "?")
        pg.wait_for_function("() => document.querySelector('#tk-result')?.textContent?.includes('状态')", timeout=90000)
        print("✓ A2A 内部任务成功")

        # 4. 计量与审计页
        pg.locator('nav button[data-p="obs"]').first.click()
        pg.wait_for_function("() => document.querySelector('#main')?.textContent?.includes('计量与审计')", timeout=10000)
        pg.wait_for_selector("#main table", timeout=10000)
        print("✓ 计量与审计页渲染成功")

        assert not errs, f"错误: {errs[:3]}"
        b.close()
    print("\nFRONTEND E2E-5 PASSED ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
