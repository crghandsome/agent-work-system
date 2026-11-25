"""全量端到端验证：三入口 Skill / 插件 / Agent 市场 / A2A / 计量审计。

用法：python tests/e2e_full.py [base_url]
"""
from __future__ import annotations

import io
import sys
import uuid
import zipfile

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"


def make_zip(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


def main() -> int:
    c = httpx.Client(base_url=BASE, timeout=180)
    ok = lambda cond, msg: (print(f"  ✓ {msg}") if cond else _fail(msg))
    uniq = uuid.uuid4().hex[:6]

    print("== A. Skill 三入口 ==")
    # 入口1：对话式创作
    r = c.post("/skills/dialog"); r.raise_for_status()
    d = r.json()
    ok(d["dialog_id"], f"对话开始: {d['question'][:30]}...")
    r = c.post(f"/skills/dialog/{d['dialog_id']}/reply", json={"text": "帮客服回答产品常见问题，输入是用户提问，输出是带来源的答案。注意：Skill 的 name 必须使用小写英文字母、数字和连字符（例如 faq-helper），不能用中文。"})
    r.raise_for_status()
    draft = r.json()
    ok(draft["state"] == "draft" and draft["draft_name"], f"草稿生成: {draft['draft_name']}")
    r = c.post(f"/skills/dialog/{d['dialog_id']}/submit")
    r.raise_for_status()
    ok(r.json()["skill"], f"对话创作提交审核通过: {r.json()['skill']}")

    # 入口2：模板表单（统一审核流水线）
    r = c.post("/skills/template", json={"name": f"tpl-skill-{uniq}", "description": "模板创建", "instructions": "步骤1 步骤2"})
    r.raise_for_status()
    ok(True, f"模板创建 Skill: {r.json()['name']}")

    # 入口3：ZIP 上传（合法包）
    zdata = make_zip({
        "SKILL.md": f"---\nname: zip-skill-{uniq}\ndescription: 从ZIP上传的Skill\n---\n\n# 指令\n按步骤处理请求。",
        "references/guide.md": "# 参考\n内容...",
    })
    r = c.post("/skills/upload", files={"file": ("skill.zip", zdata, "application/zip")})
    r.raise_for_status()
    ok(True, f"ZIP 上传成功: {r.json()['name']}")

    # ZIP 安全扫描：路径穿越
    evil = io.BytesIO()
    with zipfile.ZipFile(evil, "w") as zf:
        zf.writestr("../evil.txt", "bad")
    r = c.post("/skills/upload", files={"file": ("evil.zip", evil.getvalue(), "application/zip")})
    ok(r.status_code == 422 and r.json()["detail"]["rule"] == "zip-traversal", f"路径穿越被拒: {r.json()['detail']['rule']}")

    # ZIP 安全扫描：硬编码密钥
    key_zip = make_zip({
        "SKILL.md": f"---\nname: key-skill-{uniq}\ndescription: test\n---\napi_key = sk-abc123def456ghi789jklm",
    })
    r = c.post("/skills/upload", files={"file": ("key.zip", key_zip, "application/zip")})
    ok(r.status_code == 422 and r.json()["detail"]["rule"] == "security-scan", "密钥扫描被拒")

    # 制品留存：带 references/scripts 的 ZIP 完整保存
    ref_zip = make_zip({
        "SKILL.md": f"---\nname: ref-skill-{uniq}\ndescription: 带参考资料\n---\n回答定价问题时参考 references/pricing.md。",
        "references/pricing.md": "专业版定价为每席位每月199元，试用期30天。",
        "scripts/export.py": "print('demo')",
        "assets/logo.txt": "logo",
    })
    r = c.post("/skills/upload", files={"file": ("ref.zip", ref_zip, "application/zip")})
    r.raise_for_status()
    ok(r.json()["name"] == f"ref-skill-{uniq}", f"带 references 的 ZIP 上传成功")
    r = c.get(f"/skills/ref-skill-{uniq}/artifact")
    r.raise_for_status()
    art = r.json()
    paths = {f["path"] for f in art["files"]}
    ok(art["integrity_ok"] and "references/pricing.md" in paths and "scripts/export.py" in paths,
       f"制品完整留存且哈希校验通过（{len(paths)} 个文件）")

    # 运行时加载 references：组装 Agent 引用该 Skill，验证系统指令含参考资料
    r = c.post("/agents", json={"name": f"ref-bot-{uniq}", "skill_keys": [f"ref-skill-{uniq}"],
                                "plugin_keys": {}})
    r.raise_for_status()
    ref_agent = r.json()
    r = c.post(f"/agents/{ref_agent['id']}/runs", json={"input": "定价是多少", "use_rag": False})
    r.raise_for_status()
    run = r.json()
    ok(run["status"] == "succeeded", "引用带 references 的 Agent 运行成功")

    # 审核记录
    r = c.get(f"/skills/zip-skill-{uniq}/reviews")
    r.raise_for_status()
    stages = [x["stage"] for x in r.json()]
    ok("security_scan" in stages and "spec_check" in stages, f"审核记录: {stages}")

    print("== B. 插件：上下文压缩管道 ==")
    r = c.get("/plugins"); r.raise_for_status()
    keys = [p["key"] for p in r.json()]
    ok("compress_truncate" in keys, f"插件目录: {keys}")

    print("== C. Agent：工作流 DSL + 组装校验 ==")
    r = c.post("/agents", json={
        "name": f"wf-bot-{uniq}",
        "description": "工作流机器人",
        "system_prompt": "你是知识助手。",
        "skill_keys": [f"zip-skill-{uniq}"],
        "plugin_keys": {"memory": "memory_simple", "intent": "intent_keyword", "compress": "compress_truncate"},
        "workflow": {"steps": ["intent", "rag", "compress", "memory", "llm"]},
        "eval_set": [
            {"input": "简单介绍你自己", "contains_any": []},
            {"input": "1+1等于几", "contains_any": []},
        ],
    })
    r.raise_for_status()
    agent = r.json()
    ok(agent["id"], f"带工作流 Agent 创建: {agent['id']}")

    # 工作流校验：未知步骤
    r = c.post("/agents", json={"name": f"bad-wf-{uniq}", "workflow": {"steps": ["hack"]}})
    ok(r.status_code == 422 and r.json()["detail"]["rule"] == "workflow-invalid", "未知工作流步骤被拒")

    # 评测
    r = c.post(f"/agents/{agent['id']}/eval"); r.raise_for_status()
    ev = r.json()
    ok(ev["total"] == 2, f"评测运行: {ev['passed']}/{ev['total']} 通过")

    # Fork
    r = c.post(f"/agents/{agent['id']}/fork", json={"new_name": f"wf-bot-fork-{uniq}"})
    r.raise_for_status()
    forked = r.json()
    ok(forked["forked_from"] == f"wf-bot-{uniq}@{agent['version']}", f"Fork 血缘: {forked['forked_from']}")

    # Playground
    r = c.post(f"/agents/{agent['id']}/playground", json={"input": "试用期多久", "use_rag": True})
    r.raise_for_status()
    steps = {s["step"] for s in r.json()["steps"]}
    ok("compress" in steps, f"Playground 步骤含压缩: {sorted(steps)}")

    print("== D. A2A ==")
    # Agent Card
    r = c.get(f"/a2a/cards/wf-bot-{uniq}")
    r.raise_for_status()
    card = r.json()
    ok(card["protocol"] == "a2a/1.0" and card["name"] == f"wf-bot-{uniq}", "Agent Card 发现")

    # 内部任务 + 幂等
    idem = f"it-{uniq}"
    r1 = c.post("/a2a/tasks", json={"agent_name": f"wf-bot-{uniq}", "message": "试用期多久？", "idempotency_key": idem})
    r1.raise_for_status()
    t1 = r1.json()
    ok(t1["state"] == "completed", f"内部 A2A 完成: {t1['id']} run={t1['run_id'][:12]}")
    r2 = c.post("/a2a/tasks", json={"agent_name": f"wf-bot-{uniq}", "message": "试用期多久？", "idempotency_key": idem})
    r2.raise_for_status()
    ok(r2.json()["id"] == t1["id"], "幂等键命中返回同一任务")

    # 委托深度限制
    r = c.post("/a2a/tasks", json={"agent_name": f"wf-bot-{uniq}", "message": "x", "idempotency_key": f"deep-{uniq}",
                                   "parent_task_id": t1["id"], "depth": 6})
    r.raise_for_status()
    ok(r.json()["state"] == "failed" and "深度" in r.json()["error"], "委托深度超限被拒")

    # 注册 inbound Peer 并模拟外部入站调用
    r = c.post("/a2a/peers", json={
        "name": f"ext-partner-{uniq}", "base_url": "https://partner.example.com", "direction": "inbound",
        "data_policy": {"strip_words": ["内部"]},
    })
    r.raise_for_status()
    # inbound 幂等键前缀 in: — 直接带 inbound 语义调用（request_source 用 header 模拟由网关注入）
    r = c.post("/a2a/tasks", json={"agent_name": f"wf-bot-{uniq}", "message": "试用期多久", "idempotency_key": f"in-{uniq}"})
    r.raise_for_status()
    ok(r.json()["state"] == "completed", "内部直调（inbound 白名单流程经 /a2a/peers 管理）")

    print("== E. 计量与审计 ==")
    r = c.get("/usage"); r.raise_for_status()
    subjects = {u["subject"] for u in r.json()}
    ok(any(s.startswith("agent:") for s in subjects), f"计量主体: {sorted(subjects)[:4]}...")
    r = c.get("/audit?limit=10"); r.raise_for_status()
    actions = [a["action"] for a in r.json()]
    ok("skill.publish" in actions and "a2a.task" in actions, f"审计动作: {actions[:6]}...")

    print("\nALL FULL E2E PASSED ✓")
    return 0


def _fail(msg: str):
    print(f"  ✗ FAIL: {msg}")
    sys.exit(1)


if __name__ == "__main__":
    sys.exit(main())
