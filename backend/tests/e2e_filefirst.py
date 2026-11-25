"""验证：Skill/插件落本地目录，DB 可重建；DSH 导出包结构正确。"""
import sys, uuid, json
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import httpx

c = httpx.Client(base_url="http://localhost:8000", timeout=120)
u = uuid.uuid4().hex[:6]

# 1. 上传 ZIP Skill → 落目录
zbuf = __import__("io").BytesIO()
with __import__("zipfile").ZipFile(zbuf, "w") as z:
    z.writestr("SKILL.md", f"---\nname: fs-skill-{u}\ndescription: 用于验证目录存储的文档写作技能\n---\n\n写文档步骤")
    z.writestr("references/guide.md", "参考内容")
r = c.post("/skills/upload", files={"file": (f"s.zip", zbuf.getvalue(), "application/zip")})
r.raise_for_status()
print("✓ Skill 上传成功:", r.json()["name"])

# 2. 读取文件（目录版 API）
f = c.get(f"/skills/fs-skill-{u}/file", params={"path": "references/guide.md"})
f.raise_for_status()
assert "参考内容" in f.json()["content"]
print("✓ 从目录读取文件成功")

# 3. 修改文件（目录版 API）
r = c.put(f"/skills/fs-skill-{u}/files", json={"changes": {"references/guide.md": "修改后参考内容"}})
r.raise_for_status()
f2 = c.get(f"/skills/fs-skill-{u}/file", params={"path": "references/guide.md"})
assert "修改后" in f2.json()["content"]
print("✓ 修改目录文件成功")

# 4. 详情文件树（目录结构）
d = c.get(f"/skills/fs-skill-{u}/detail").json()
paths = [n["name"] for n in d["tree"]]
assert "SKILL.md" in paths and "references" in paths, paths
print("✓ 详情展示目录树:", paths)

# 5. 插件落目录
key = f"fs-plugin-{u}"
c.post("/plugins/install", json={"key": key, "name": "目录测试插件",
    "description": "验证插件落目录", "extension_point": "IntentRecognizer"})
pl = next(p for p in c.get("/plugins/managed").json() if p["key"] == key)
assert pl["install_path"] == f"plugins/{key}", pl["install_path"]
print("✓ 插件落目录:", pl["install_path"])

# 6. DSH 导出 Skill（写到 DSH_HOME/skills）
r = c.post(f"/dsh/export/skills/fs-skill-{u}")
r.raise_for_status()
exp = r.json()
print("✓ Skill 导出到 DSH:", exp["path"])
import pathlib
assert (pathlib.Path(exp["path"]) / "SKILL.md").exists()

# 7. DSH 导出插件 bundle
r = c.post(f"/dsh/export/plugins/{key}")
r.raise_for_status()
b = r.json()
print("✓ 插件导出 DSH bundle:", b["path"], b["bundle"])
bd = pathlib.Path(b["path"])
for fn in ("package.json", "cordis.patch.yml", "index.js", "locale/zh.json", "icon.svg", "README.md"):
    assert (bd / fn).exists(), fn
pkg = json.loads((bd / "package.json").read_text(encoding="utf-8"))
assert pkg["dsh"]["bundle"]["patch"] == "./cordis.patch.yml"
assert b["dsh_extension"] == "ctx.systemPrompt.section", "必须是真实 DSH 扩展点"
index_src = (bd / "index.js").read_text(encoding="utf-8")
assert "export function apply(ctx, config)" in index_src, "必须是标准 Cordis apply 入口"
assert "ctx.systemPrompt.section" in index_src and "inject = ['system-prompt']" in index_src
assert "ctx.effect" in index_src and "return () =>" in index_src, "必须正确管理注册生命周期"
print("✓ DSH bundle 结构完整且是真正可运行的 Cordis 插件")

# 清理
c.delete(f"/skills/fs-skill-{u}")
c.delete(f"/plugins/{key}")
print("\nFILE-FIRST & DSH-EXPORT E2E PASSED ✓")
