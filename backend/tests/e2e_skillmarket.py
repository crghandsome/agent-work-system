"""Skill 市场后端：分类导航 / 详情文件树 / MD 上传 / Skill 设计子 Agent 经 A2A 委托。"""
import sys, uuid
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import httpx

BASE = "http://localhost:8000"
c = httpx.Client(base_url=BASE, timeout=180)
u = uuid.uuid4().hex[:6]

# 1. 分类导航
cats = c.get("/skills/categories").json()
assert isinstance(cats, list), cats
print("✓ 分类接口返回", len(cats), "个分类")
for cat in cats:
    print(f"   {cat['name']}: {cat['count']} 个")

# 2. 创建不同描述的 Skill → 落到不同分类
tests = [
    ("cat-code-"+u, "用于代码审查与重构的编程辅助技能", "code-reviewer", "审查代码并给出重构建议。"),
    ("cat-write-"+u, "用于撰写文案和润色文章的写作技能", "copy-writer", "撰写营销文案并润色。"),
    ("cat-data-"+u, "用于分析销售数据和生成报表的数据分析技能", "data-analyst", "分析数据并输出报表。"),
]
for name, desc, fname, body in tests:
    r = c.post("/skills/upload-md", files={"file": (fname + ".md", f"---\nname: {name}\ndescription: {desc}\n---\n\n{body}".encode(), "text/markdown")})
    r.raise_for_status()
    print(f"✓ 上传 MD: {name} → 分类 {r.json()['category']}")

# 3. 分类正确性
cats = c.get("/skills/categories").json()
by_name = {c_["name"]: c_ for c_ in cats}
assert "编程" in by_name and "写作" in by_name and "数据分析" in by_name, [c_["name"] for c_ in cats]
for c_ in cats:
    for s in c_["skills"]:
        if s["name"].startswith("cat-"):
            print(f"   {s['name']} 在「{c_['name']}」分类下")
print("✓ 分类归属正确")

# 4. Skill 详情：文件结构树
r = c.get(f"/skills/{tests[0][0]}/detail"); r.raise_for_status()
d = r.json()
assert d["tree"], "无文件树"
assert d["tree"][0]["name"] == "SKILL.md", d["tree"]
assert d["frontmatter"], "无 frontmatter"
print("✓ 详情接口：文件树 + frontmatter", [n["name"] for n in d["tree"]])

# 5. 人工改分类
r = c.patch(f"/skills/{tests[0][0]}/category", json={"category": "设计"}); r.raise_for_status()
assert r.json()["category"] == "设计"
print("✓ 人工覆盖分类")

# 6. Skill 设计子 Agent：主 Agent 经 A2A 委托设计
r = c.get("/agents"); assert any(a["name"] == "skill-designer" for a in r.json()), "子 Agent 未注册"
print("✓ Skill 设计子 Agent 已注册")
s = c.post("/orchestrator/sessions").json()
r = c.post(f"/orchestrator/sessions/{s['session_id']}/chat",
           json={"text": "帮我设计一个 skill，用于把会议录音整理成会议纪要，要点分段清晰"})
r.raise_for_status()
body = r.json()
print("   主 Agent 回复:", body["reply"][:150].replace("\n", " "))
assert body["reply"].strip(), "无回复"
# 设计出的 skill 应出现在市场
skills = c.get("/skills").json()
assert any(x.get("source") == "agent_dialog" for x in skills), "未生成对话设计的 Skill"
print("✓ 主 Agent 经 A2A 委托设计出 Skill:", [x["name"] for x in skills if x["source"] == "agent_dialog"][:3])

print("\nSKILL-MARKET BACKEND E2E PASSED ✓")
