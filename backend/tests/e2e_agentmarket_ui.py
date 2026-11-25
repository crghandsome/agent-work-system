"""前端测试：Agent 市场（独立下拉菜单+推荐、团队、卡片布局、删除/重组装）。"""
import sys, uuid
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import httpx
from playwright.sync_api import sync_playwright

c = httpx.Client(base_url="http://localhost:8900", timeout=60)
U = uuid.uuid4().hex[:6]
N = f"ui-market-{U}"

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    pg = b.new_page(viewport={"width": 1500, "height": 1000})
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.on("response", lambda r: errs.append(f"HTTP {r.status} {r.url}") if r.status >= 400 else None)
    pg.goto("http://localhost:8900/console", wait_until="networkidle", timeout=30000)
    pg.locator('nav button[data-p="agents"]').first.click()
    pg.wait_for_selector("#ag-skill-menu", timeout=15000)
    pg.wait_for_timeout(1000)

    # 1. 两个独立菜单（Skill / 插件）
    assert pg.locator("#ag-skill-menu").count() == 1 and pg.locator("#ag-plugin-menu").count() == 1
    assert pg.locator("#ag-skill-list .menu-item").count() >= 1
    assert pg.locator("#ag-plugin-list .menu-item").count() >= 1
    print("✓ Skill / 插件 两个独立下拉菜单")

    # 2. 菜单搜索过滤
    pg.locator("#ag-plugin-q").fill("intent")
    pg.wait_for_timeout(300)
    items = pg.locator("#ag-plugin-list .menu-item").all_inner_texts()
    assert any("intent" in x for x in items), items
    print("✓ 菜单搜索过滤")

    # 3. 按描述智能推荐
    pg.locator("#ag-d").fill("我需要一个文档写作和意图识别的助手")
    pg.wait_for_timeout(1500)
    rec = pg.locator("#ag-recommend").inner_text()
    assert "推荐" in rec, rec
    print("✓ 按描述智能推荐:", rec.replace("\n", " ")[:80])

    # 4. 从菜单选 Skill 与插件，发布
    pg.locator("#ag-skill-list .menu-item").first.click()
    pg.locator("#ag-plugin-q").fill("")
    pg.wait_for_timeout(300)
    pg.locator("#ag-plugin-list .menu-item").first.click()
    pg.locator("#ag-n").fill(N)
    pg.locator("button", has_text="校验并发布").first.click()
    pg.wait_for_function("() => document.querySelector('#ag-msg')?.textContent?.includes('发布成功')", timeout=15000)
    print("✓ 从菜单选择并发布 Agent")

    # 5. 卡片布局：简介、重新组装、删除按钮
    pg.wait_for_timeout(500)
    card = pg.locator(".ag-card", has_text=N).first
    assert card.locator(".ag-desc").count() == 1
    assert card.locator("button", has_text="重新组装").count() == 1
    assert card.locator("button", has_text="删除").count() == 1
    print("✓ Agent 卡片布局（简介/重新组装/删除）")

    # 6. 重新组装：点按钮回填表单
    card.locator("button", has_text="重新组装").click()
    pg.wait_for_timeout(300)
    assert pg.locator("#ag-n").input_value() == N
    assert "重新组装" in pg.locator("#ag-edit-note").inner_text()
    print("✓ 重新组装回填表单")

    # 7. 团队区块（成员勾选 + 自动创建主理）
    assert pg.locator("#tm-leader").count() == 1 and pg.locator("#tm-members").count() == 1
    # 勾选一个成员 Agent
    pg.locator(".tm-member").first.check()
    checked = pg.locator(".tm-member:checked").count()
    assert checked >= 1, "成员勾选失效"
    pg.locator("#tm-n").fill(f"team-{U}")
    pg.locator("#tm-leader").fill(f"leader-{U}")  # 留空也可，这里显式指定
    pg.locator("button", has_text="创建团队").click()
    pg.wait_for_function("() => document.querySelector('#tm-msg')?.textContent?.includes('团队创建成功')", timeout=10000)
    team_msg = pg.locator("#tm-msg").inner_text()
    assert "个成员" in team_msg and "0 个成员" not in team_msg, team_msg
    print("✓ 团队创建（成员数正确）:", team_msg[:60])
    # 团队列表有成员（等渲染完成）
    pg.wait_for_selector(f"#tm-list .ag-card:has-text('team-{U}')", timeout=5000)
    team_card = pg.locator("#tm-list .ag-card", has_text=f"team-{U}").first
    assert "成员" in team_card.inner_text()
    print("✓ 团队卡片显示成员")

    # 8. 布局：团队列表在 Agent 列表下方，两者都是三列
    order = pg.evaluate("""() => {
      const al = document.querySelector('#ag-list').getBoundingClientRect().top;
      const tl = document.querySelector('#tm-list').getBoundingClientRect().top;
      return {al, tl, cols: getComputedStyle(document.querySelector('#ag-list')).gridTemplateColumns.split(' ').length};
    }""")
    assert order["tl"] > order["al"], f"团队列表不在 Agent 列表下方: {order}"
    assert order["cols"] == 3, f"不是三列: {order}"
    print("✓ 团队列表在 Agent 列表下方，三列布局")

    # 8. 删除 Agent（确认弹框）
    pg.locator('nav button[data-p="agents"]').first.click()
    pg.wait_for_selector("#ag-list", state="attached", timeout=10000)
    pg.wait_for_timeout(600)
    card = pg.locator(".ag-card", has_text=N).first
    card.locator("button", has_text="删除").click()
    pg.wait_for_selector(".modal-mask", timeout=5000)
    pg.locator("#modal-ok").click()
    pg.wait_for_timeout(800)
    assert pg.locator(".ag-card", has_text=N).count() == 0
    print("✓ 删除 Agent")

    # 清理团队与测试数据
    c.delete(f"/agents/teams/team-{U}")
    assert not errs, errs[:3]
    b.close()
print("PASS-AGENT-MARKET-UI ✓")
