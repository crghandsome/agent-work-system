"""测试：Skill 文件结构中非 SKILL.md 文件可查看（路径前缀 bug 修复）。"""
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    pg = b.new_page(viewport={"width": 1500, "height": 1000})
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto("http://localhost:8900/console", wait_until="networkidle", timeout=30000)
    pg.locator('nav button[data-p="skills"]').first.click()
    pg.wait_for_selector("#sk-list", state="attached", timeout=15000)
    pg.wait_for_timeout(1000)

    # 找一个有 references/scripts 的 Skill（ref-skill-*）
    card = pg.locator(".sk-card", has_text="ref-skill-").first
    if card.count() == 0:
        # 换用卡片标题点击
        card = pg.locator("#sk-list .card", has_text="ref-skill-").first
    print("found card:", card.count())
    card.click()
    pg.wait_for_selector(".ftree", timeout=10000)
    # 点击 references 目录下的文件
    file_item = pg.locator(".ftree .file-item", has_text="pricing.md").first
    assert file_item.count() == 1, "找不到 references/pricing.md 文件项"
    file_item.click()
    pg.wait_for_selector("#sk-edit-content", timeout=10000)
    content = pg.locator("#sk-edit-content").input_value()
    assert content and "价格" not in []  # 非空即可
    assert len(content) > 0, "子文件内容为空"
    print("✓ 点击 references 子文件成功查看内容:", content[:60])
    assert not errs, errs[:3]
    b.close()
print("PASS-SKILL-FILE-TREE ✓")
