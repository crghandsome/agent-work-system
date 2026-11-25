"""测试：Agent 列表对齐 + 成员选择带简介。"""
import sys, uuid
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import httpx
from playwright.sync_api import sync_playwright

c = httpx.Client(base_url="http://localhost:8900", timeout=60)
U = uuid.uuid4().hex[:6]
c.post("/agents", json={"name": f"align-{U}", "description": "专门用于验证成员介绍的测试Agent"})
c.post("/agents", json={"name": f"align2-{U}", "description": "第二个测试Agent"})

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    pg = b.new_page(viewport={"width": 1500, "height": 1000})
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto("http://localhost:8900/console", wait_until="networkidle", timeout=30000)
    pg.locator('nav button[data-p="agents"]').first.click()
    pg.wait_for_selector("#ag-list", timeout=15000)
    pg.wait_for_timeout(1000)

    # 1. 列表与表单对齐（左边界、宽度一致）
    geo = pg.evaluate("""() => {
      const form = document.querySelector('#main .card').getBoundingClientRect();
      const list = document.querySelector('#ag-list').getBoundingClientRect();
      const teams = document.querySelector('#tm-list').getBoundingClientRect();
      return {formL: form.left, formW: form.width, listL: list.left, listW: list.width, teamL: teams.left, teamW: teams.width};
    }""")
    assert abs(geo["formL"] - geo["listL"]) < 2, f"Agent 列表未与表单对齐: {geo}"
    assert abs(geo["formW"] - geo["listW"]) < 2, f"Agent 列表宽度不一致: {geo}"
    assert abs(geo["formL"] - geo["teamL"]) < 2, f"团队列表未对齐: {geo}"
    assert abs(geo["formW"] - geo["teamW"]) < 2, f"团队列表宽度不一致: {geo}"
    print("✓ Agent 列表与团队列表均与表单左对齐、同宽", geo)

    # 卡片内文字与表单内文字左对齐（表单 .card padding 18px）
    inner = pg.evaluate("""() => {
      const formCard = document.querySelector('#main .card');
      const formTitle = formCard.querySelector('.t') || formCard.firstElementChild;
      const agCard = document.querySelector('#ag-list .ag-card');
      const agTitle = agCard.querySelector('.ag-title');
      const sect = document.querySelector('#main .list-section > .sect');
      const grid = document.querySelector('#ag-list');
      const cards = [...grid.querySelectorAll('.ag-card')].slice(0,3);
      const gr = grid.getBoundingClientRect();
      return {
        formTextL: formTitle.getBoundingClientRect().left,
        cardTextL: agTitle.getBoundingClientRect().left,
        sectL: sect.getBoundingClientRect().left,
        gridL: gr.left, gridR: gr.right,
        firstL: cards[0]?.getBoundingClientRect().left,
        lastR: cards[cards.length-1]?.getBoundingClientRect().right,
        cardW: cards[0]?.getBoundingClientRect().width,
      };
    }""")
    assert abs(inner["formTextL"] - inner["cardTextL"]) < 2, f"卡片内文字未对齐: {inner}"
    # .sect 的 padding-left 18px 使标题文本与表单内容对齐（元素自身 rect 不含 padding）
    sect_content_l = inner["sectL"] + 18
    assert abs(inner["formTextL"] - sect_content_l) < 2, f"列表标题文字未对齐: {inner}"
    # 三列卡片总宽度必须撑满网格（与表单同宽 1080px）
    assert abs(inner["firstL"] - inner["gridL"]) < 2, f"首卡未贴网格左边: {inner}"
    assert abs(inner["lastR"] - inner["gridR"]) < 2, f"末卡未贴网格右边: {inner}"
    assert inner["cardW"] > 300, f"卡片被挤窄: {inner}"
    print("✓ 三列卡片撑满与表单同宽", inner)

    # 2. 成员选择带简介
    opts = pg.locator(".member-opt")
    assert opts.count() >= 2
    target = pg.locator(".member-opt", has_text=f"align-{U}")
    assert "专门用于验证成员介绍的测试Agent" in target.inner_text(), target.inner_text()
    print("✓ 成员选择项显示 Agent 简介")

    assert not errs, errs[:3]
    b.close()

# 清理
for ag in c.get("/agents").json():
    if ag["name"].startswith("align"):
        c.delete(f"/agents/{ag['id']}")
print("PASS-AGENT-LAYOUT ✓")
