"""前端功能测试 3：Skill 市场 —— 两种创作方式（上传 zip/md）+ 分类浏览 + 详情文件结构。

用法：python tests/e2e_frontend3.py [url]
"""
from __future__ import annotations

import io
import sys
import uuid
import zipfile

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from playwright.sync_api import sync_playwright

URL = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8900/console"
U = uuid.uuid4().hex[:6]


def zip_bytes(name: str, desc: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("SKILL.md", f"---\nname: {name}\ndescription: {desc}\n---\n\n回答要简洁。")
        z.writestr("references/guide.md", "参考资料内容")
    return buf.getvalue()


def main() -> int:
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        pg = b.new_page(viewport={"width": 1500, "height": 1000})
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.on("response", lambda r: errs.append(f"HTTP {r.status} {r.url}") if r.status >= 400 else None)
        pg.goto(URL, wait_until="networkidle", timeout=30000)
        pg.locator('nav button[data-p="skills"]').first.click()
        pg.wait_for_selector("#sk-file", state="attached", timeout=15000)
        pg.wait_for_timeout(1200)

        # 1. 创作方式只有两种
        txt = pg.locator("#main").inner_text()
        assert "对话设计 Skill" in txt and "上传 Skill 文件" in txt
        create_area = pg.locator("#sk-file").locator("xpath=ancestor::div[contains(@class,'card')]").first.inner_text()
        assert "模板创建" not in create_area, "不应再有模板创建"
        print("✓ 两种创作方式（对话设计 / 上传文件）")

        # 2. ZIP 上传
        pg.locator("#sk-file").set_input_files({
            "name": f"ui-{U}.zip", "mimeType": "application/zip",
            "buffer": zip_bytes(f"ui-zip-{U}", "用于前端测试的写作技能")})
        pg.get_by_role("button", name="上传").click()
        pg.wait_for_function("() => document.querySelector('#sk-up-msg')?.textContent?.includes('上传成功')", timeout=30000)
        print("✓ ZIP 上传成功")

        # 3. MD 上传
        pg.locator("#sk-file").set_input_files({
            "name": f"ui-{U}.md", "mimeType": "text/markdown",
            "buffer": f"---\nname: ui-md-{U}\ndescription: 用于前端测试的编程技能\n---\n\n代码审查步骤".encode()})
        pg.get_by_role("button", name="上传").click()
        pg.wait_for_function("() => document.querySelector('#sk-up-msg')?.textContent?.includes('上传成功')", timeout=30000)
        print("✓ MD 上传成功")

        # 4. 分类导航 + 卡片
        pg.wait_for_timeout(800)
        chips = pg.locator(".cat-chip").count()
        assert chips >= 2, "分类导航缺失"
        cards = pg.locator(".sk-card").count()
        assert cards >= 2, "卡片缺失"
        print(f"✓ 分类导航 {chips} 个，卡片 {cards} 张")

        # 5. 点击卡片看文件结构
        target = pg.locator(".sk-card", has_text=f"ui-zip-{U}").first
        target.click()
        pg.wait_for_selector(".modal-mask", timeout=8000)
        modal_txt = pg.locator(".modal").inner_text()
        assert "文件结构" in modal_txt and "SKILL.md" in modal_txt and "references" in modal_txt, modal_txt[:300]
        print("✓ 详情显示文件结构（含 references 子目录）")
        pg.locator("#modal-ok").click()
        pg.wait_for_timeout(300)

        assert not errs, f"错误: {errs[:3]}"
        b.close()
    print("\nFRONTEND E2E-3 PASSED ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
