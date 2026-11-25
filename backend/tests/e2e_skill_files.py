"""前端测试：Skill 文件点击查看/编辑、删除 Skill、箭头上传图标。"""
import sys, uuid
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from playwright.sync_api import sync_playwright

URL = "http://localhost:8900/console"
U = uuid.uuid4().hex[:6]
NAME = f"ui-edit-{U}"
MD = f"---\nname: {NAME}\ndescription: 用于测试文件编辑的编程技能\n---\n\n原始指令内容".encode()

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    pg = b.new_page(viewport={"width": 1500, "height": 1000})
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.on("response", lambda r: errs.append(f"HTTP {r.status} {r.url}") if r.status >= 400 else None)
    pg.goto(URL, wait_until="networkidle", timeout=30000)
    pg.locator('nav button[data-p="skills"]').first.click()
    pg.wait_for_selector("#sk-file", state="attached", timeout=10000)
    pg.wait_for_timeout(800)

    # 1. 上传控件是箭头按钮，原生 input 隐藏
    assert pg.locator("#sk-file").is_hidden(), "原生文件输入框应隐藏"
    arrows = pg.locator("button.icon-btn", has=pg.locator("svg"))
    assert arrows.count() >= 1, "应有圆形箭头上传按钮"
    print("✓ 箭头上传按钮，原生控件隐藏")

    # 2. 上传测试 Skill
    pg.locator("#sk-file").set_input_files({"name": f"{NAME}.md", "mimeType": "text/markdown", "buffer": MD})
    pg.get_by_role("button", name="上传").click()
    pg.wait_for_function("() => document.querySelector('#sk-up-msg')?.textContent?.includes('上传成功')", timeout=30000)
    print("✓ 测试 Skill 上传成功")

    # 3. 点击卡片 → 详情 → 点击文件 → 编辑弹框
    pg.wait_for_timeout(600)
    pg.locator(".sk-card", has_text=NAME).first.click()
    pg.wait_for_selector(".modal-mask", timeout=8000)
    assert "文件结构" in pg.locator(".modal").inner_text()
    pg.locator(".file-item", has_text="SKILL.md").first.click()
    pg.wait_for_selector("#sk-edit-content", timeout=8000)
    old = pg.locator("#sk-edit-content").input_value()
    assert "原始指令内容" in old, old
    print("✓ 点击文件打开编辑弹框，显示原内容")

    # 4. 修改并保存
    new_content = old.replace("原始指令内容", "修改后的指令内容")
    pg.locator("#sk-edit-content").fill(new_content)
    pg.locator(".modal-mask").last.locator("#modal-ok").click()  # 编辑框的保存按钮
    pg.wait_for_timeout(1200)
    print("✓ 文件修改保存成功")

    # 5. 保存后前端自动刷新详情：等待文件树更新，再点开文件验证内容已持久化
    pg.wait_for_selector(".file-item", timeout=8000)
    pg.locator(".file-item", has_text="SKILL.md").first.click()
    pg.wait_for_selector("#sk-edit-content", timeout=8000)
    assert "修改后的指令内容" in pg.locator("#sk-edit-content").input_value()
    print("✓ 修改后的内容已持久化")
    pg.locator(".modal-mask").last.locator("#modal-cancel").click()   # 关闭验证用的编辑框
    pg.wait_for_timeout(300)

    # 6. 详情框里删除 Skill（保存后详情已自动重开，且只有这一个框）
    pg.wait_for_timeout(400)
    assert pg.locator("button.icon-btn.trash", has=pg.locator("svg")).count() >= 1, "详情框未打开"
    pg.locator("button.icon-btn.trash", has=pg.locator("svg")).first.click()
    pg.wait_for_timeout(300)
    pg.locator(".modal-mask").last.locator("#modal-ok").click()
    pg.wait_for_timeout(1200)
    # 卡片应消失
    assert pg.locator(".sk-card", has_text=NAME).count() == 0, "Skill 未删除"
    print("✓ 删除 Skill 成功")

    assert not errs, errs[:3]
    b.close()
print("PASS-SKILL-FILES ✓")
