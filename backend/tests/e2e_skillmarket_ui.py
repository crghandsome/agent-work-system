"""前端测试：Skill 市场（分类导航 / hover 简介 / 点击看文件结构 / 两种创作入口）。"""
import sys, uuid, io, zipfile
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from playwright.sync_api import sync_playwright

URL = "http://localhost:8900/console"
u = uuid.uuid4().hex[:6]


def md_skill(name, desc, body="步骤一\n步骤二"):
    return f"---\nname: {name}\ndescription: {desc}\n---\n\n{body}".encode()


def zip_skill(name, desc):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("SKILL.md", f"---\nname: {name}\ndescription: {desc}\n---\n\n正文内容")
        z.writestr("references/guide.md", "参考资料内容")
    return buf.getvalue()


with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    pg = b.new_page(viewport={"width": 1500, "height": 950})
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto(URL, wait_until="networkidle", timeout=30000)
    pg.wait_for_timeout(1000)

    # 1. 两种创作方式（先进入 Skill 市场）
    pg.locator('nav button[data-p="skills"]').first.click()
    pg.wait_for_selector("#sk-file", state="attached", timeout=10000)
    txt = pg.locator("#main").inner_text()
    assert "对话设计 Skill" in txt and "上传 Skill 文件" in txt, "缺少两种创作入口"
    # 只校验创作区没有「模板创建」卡片，不扫描整个页面（Skill 列表里可能有历史数据含这个词）
    create_area = pg.locator("#sk-file").locator("xpath=ancestor::div[contains(@class,'card')]").first.inner_text()
    assert "模板创建" not in create_area, "创作区不应再有模板创建入口"
    print("✓ 两种创作方式：对话设计 / 上传文件")

    # 2. 分类导航存在且有多个分类
    pg.wait_for_selector("#sk-cats", timeout=10000)
    pg.wait_for_timeout(800)
    chips = pg.locator(".cat-chip").all_inner_texts()
    assert len(chips) >= 2, f"分类不足: {chips}"
    print("✓ 分类导航:", [c.strip() for c in chips])

    # 3. 上传 md → 出现在对应分类
    pg.locator("#sk-file").set_input_files({"name": f"ui-{u}.md", "mimeType": "text/markdown",
                                          "buffer": md_skill(f"ui-md-{u}", "用于代码审查的编程辅助技能")})
    pg.get_by_role("button", name="上传").click()
    pg.wait_for_function("() => document.querySelector('#sk-up-msg')?.textContent?.includes('上传成功')", timeout=30000)
    print("✓ MD 上传成功")

    # 4. 上传 zip（含 references）
    pg.locator("#sk-file").set_input_files({"name": f"ui-{u}.zip", "mimeType": "application/zip",
                                          "buffer": zip_skill(f"ui-zip-{u}", "用于撰写报告的写作技能")})
    pg.get_by_role("button", name="上传").click()
    pg.wait_for_function("() => document.querySelector('#sk-up-msg')?.textContent?.includes('上传成功')", timeout=30000)
    print("✓ ZIP 上传成功")

    # 5. 卡片网格渲染
    pg.wait_for_timeout(800)
    cards = pg.locator(".sk-card")
    assert cards.count() >= 2, f"卡片数 {cards.count()}"
    print(f"✓ 卡片网格渲染 {cards.count()} 张")

    # 6. 分类筛选：点击「写作」只看写作类
    pg.get_by_role("button", name="写作").click()
    pg.wait_for_timeout(500)
    names = pg.locator(".sk-card .sn").all_inner_texts()
    assert any("ui-zip" in n for n in names), f"写作分类未包含 zip skill: {names}"
    print("✓ 分类筛选生效:", names)

    # 7. hover 显示简介浮层
    card = pg.locator(".sk-card").first
    assert card.locator(".sk-tip").count() == 1
    op = pg.evaluate("() => getComputedStyle(document.querySelector('.sk-card .sk-tip')).opacity")
    assert op == "0", f"浮层初始应隐藏，实际 {op}"
    card.hover()
    pg.wait_for_timeout(400)
    op2 = pg.evaluate("() => getComputedStyle(document.querySelector('.sk-card .sk-tip')).opacity")
    assert op2 == "1", f"hover 后浮层未显示，实际 {op2}"
    print("✓ hover 显示简介浮层")

    # 8. 点击卡片 → 详情弹框显示文件结构
    card.click()
    pg.wait_for_selector(".modal-mask", timeout=8000)
    modal_txt = pg.locator(".modal").inner_text()
    assert "文件结构" in modal_txt, modal_txt[:200]
    assert "SKILL.md" in modal_txt, "文件树缺少 SKILL.md"
    print("✓ 详情弹框显示文件结构")
    pg.locator("#modal-ok").click()
    pg.wait_for_timeout(300)

    assert not errs, errs[:3]
    b.close()
print("PASS-SKILL-MARKET ✓")
