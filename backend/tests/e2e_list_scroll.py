"""测试：Agent 列表/团队列表为固定高度可滚动窗口，且卡片仍与表单对齐。"""
import sys, uuid
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import httpx
from playwright.sync_api import sync_playwright

c = httpx.Client(base_url="http://localhost:8900", timeout=120)
U = uuid.uuid4().hex[:6]
# 造 8 个 Agent 让列表超过一屏
names = []
for i in range(8):
    n = f"scroll-{U}-{i}"
    names.append(n)
    c.post("/agents", json={"name": n, "description": f"滚动测试 Agent {i}"})

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    pg = b.new_page(viewport={"width": 1500, "height": 1000})
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto("http://localhost:8900/console", wait_until="networkidle", timeout=30000)
    pg.locator('nav button[data-p="agents"]').first.click()
    pg.wait_for_selector("#ag-list", timeout=15000)
    pg.wait_for_timeout(1000)

    info = pg.evaluate("""() => {
      const views = [...document.querySelectorAll('.ag-list-view')];
      const f = document.querySelector('#main .card').getBoundingClientRect();
      const g = document.querySelector('#ag-list').getBoundingClientRect();
      return {viewCount: views.length,
        maxH: views.map(v => getComputedStyle(v).maxHeight),
        overflow: views.map(v => getComputedStyle(v).overflowY),
        scrollH: views[0].scrollHeight, clientH: views[0].clientHeight,
        formL: f.left, formR: f.right, gridL: g.left, gridR: g.right};
    }""")
    assert info["viewCount"] == 2, info
    assert all(x == "420px" for x in info["maxH"]), info
    assert all(x == "auto" for x in info["overflow"]), info
    assert info["scrollH"] > info["clientH"], f"列表未超出窗口，无法测试滚动: {info}"
    assert abs(info["formL"] - info["gridL"]) < 2 and abs(info["formR"] - info["gridR"]) < 2, info
    print("✓ 两个列表为固定高度可滚动窗口，且宽度与表单对齐", info)

    # 实际滚动
    pg.locator(".ag-list-view").first.evaluate("e => e.scrollTop = 300")
    pg.wait_for_timeout(300)
    top = pg.locator(".ag-list-view").first.evaluate("e => e.scrollTop")
    assert top > 0, "滚动失效"
    print("✓ 列表可滑动浏览")

    assert not errs, errs[:3]
    b.close()

# 清理
for ag in c.get("/agents").json():
    if ag["name"].startswith(f"scroll-{U}-"):
        c.delete(f"/agents/{ag['id']}")
print("PASS-LIST-SCROLL ✓")
