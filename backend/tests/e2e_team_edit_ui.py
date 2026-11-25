"""前端：编辑已建团队（回填、增删成员、改描述、保存）。"""
import sys, uuid
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import httpx
from playwright.sync_api import sync_playwright

c = httpx.Client(base_url="http://localhost:8900", timeout=120)
U = uuid.uuid4().hex[:6]
a1, a2, a3 = f"te-a-{U}", f"te-b-{U}", f"te-c-{U}"
for n in (a1, a2, a3):
    c.post("/agents", json={"name": n, "description": f"成员 {n}"})
tn = f"te-team-{U}"
c.post("/agents/teams", json={"name": tn, "description": "原描述", "leader": "", "members": [a1]})

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    pg = b.new_page(viewport={"width": 1500, "height": 1000})
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto("http://localhost:8900/console", wait_until="networkidle", timeout=30000)
    pg.locator('nav button[data-p="agents"]').first.click()
    pg.wait_for_selector("#tm-list", timeout=15000)
    pg.wait_for_timeout(1000)

    # 点编辑
    card = pg.locator("#tm-list .ag-card", has_text=tn).first
    card.locator("button", has_text="编辑成员/信息").click()
    pg.wait_for_timeout(600)
    assert pg.locator("#tm-n").input_value() == tn
    assert pg.locator("#tm-d").input_value() == "原描述"
    assert pg.locator("#tm-submit-btn").inner_text() == "保存修改"
    assert pg.locator("#tm-n").is_disabled()
    print("✓ 编辑回填团队信息")

    # 改描述 + 加成员
    pg.locator("#tm-d").fill("修改后的团队描述")
    pg.locator(f".tm-member[value='{a2}']").check()
    pg.locator(f".tm-member[value='{a3}']").check()
    pg.locator("#tm-submit-btn").click()
    pg.wait_for_function("() => document.querySelector('#tm-msg')?.textContent?.includes('团队更新成功')", timeout=10000)
    print("✓ 保存团队修改（描述 + 新增 2 成员）")

    # 验证团队卡片显示新成员与描述
    pg.wait_for_selector(f"#tm-list .ag-card:has-text('{tn}')", timeout=5000)
    tcard = pg.locator("#tm-list .ag-card", has_text=tn).first.inner_text()
    assert "修改后的团队描述" in tcard and a2 in tcard and a3 in tcard, tcard
    print("✓ 团队卡片显示更新后的信息与成员")

    assert not errs, errs[:3]
    b.close()

# 后端确认
t = next(x for x in c.get("/agents/teams").json() if x["name"] == tn)
assert set(t["members"]) == {a1, a2, a3} and t["description"] == "修改后的团队描述"
print("✓ 后端持久化确认")

c.delete(f"/agents/teams/{tn}")
for n in (a1, a2, a3, f"{tn}-leader"):
    for ag in c.get("/agents").json():
        if ag["name"] == n:
            c.delete(f"/agents/{ag['id']}")
print("PASS-TEAM-EDIT-UI ✓")
