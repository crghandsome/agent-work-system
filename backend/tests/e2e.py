"""端到端验证：创建 Skill → 入库文档 → 组装 Agent → 运行 → 校验 RAG/插件生效。

用法：python tests/e2e.py [base_url]   默认 http://localhost:8000（容器内）
"""
from __future__ import annotations

import sys

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"


def main() -> int:
    client = httpx.Client(base_url=BASE, timeout=120)

    print("== 1. health ==")
    r = client.get("/health")
    r.raise_for_status()
    print(r.json())

    print("== 2. 注册 Skill ==")
    r = client.post(
        "/skills/template",
        json={
            "name": "faq-assistant",
            "description": "基于知识库回答产品常见问题，回答需引用知识来源。",
            "instructions": "回答用户问题时：1) 优先使用知识库检索结果；2) 标注来源；3) 知识库没有的内容明确说明不知道。",
            "source": "template",
        },
    )
    r.raise_for_status()
    print(r.json()["name"], "OK")

    print("== 3. 校验器拒绝非法 Skill（name 格式错误应 422）==")
    r = client.post("/skills/template", json={"name": "Bad Name!", "description": "x", "instructions": "y"})
    assert r.status_code == 422, f"期望 422，得到 {r.status_code}"
    print("rule:", r.json()["detail"]["rule"], "OK")

    print("== 4. 入库 RAG 文档 ==")
    r = client.post(
        "/rag/documents",
        json={
            "title": "测试FAQ",
            "content": "测试文档的免费试用期为30天，超过后需要购买许可证。"
            "产品支持中文和英文界面，暂不支持其他语言。技术支持的工作时间是每天9点到21点。"
            "数据默认存储在华东区域，可以在设置中切换到其他区域。定价为每席位每月199元。",
        },
    )
    r.raise_for_status()
    doc = r.json()
    print("chunks:", doc["chunk_count"], "OK")

    print("== 5. RAG 检索 ==")
    r = client.post("/rag/search", json={"content": "试用期多久"})
    r.raise_for_status()
    hits = r.json()["hits"]
    assert hits, "检索无结果"
    # 内容精确校验见 e2e_full.py（使用唯一标识词避免历史文档干扰）；此处只验证检索链路通
    print(f"top1: {hits[0]['text'][:40]}... OK")
    print(f"top1: {hits[0]['text'][:60]}... reranked={r.json()['reranked']} OK")

    print("== 6. 组装 Agent（memory + intent 插件，引用 faq-assistant）==")
    r = client.post(
        "/agents",
        json={
            "name": "faq-bot",
            "description": "产品 FAQ 问答机器人",
            "system_prompt": "你是客服助手，回答简短准确。",
            "skill_keys": ["faq-assistant"],
            "plugin_keys": {"memory": "memory_simple", "intent": "intent_keyword"},
        },
    )
    r.raise_for_status()
    agent = r.json()
    print(f"agent: {agent['id']} v{agent['version']} OK")

    print("== 7. 组装校验：引用不存在的 Skill 应 422 ==")
    r = client.post("/agents", json={"name": "bad-bot", "skill_keys": ["no-such-skill"], "plugin_keys": {}})
    assert r.status_code == 422, f"期望 422，得到 {r.status_code}"
    print("rule:", r.json()["detail"]["rule"], "OK")

    print("== 8. 运行 Agent ==")
    r = client.post(f"/agents/{agent['id']}/runs", json={"input": "免费试用期是多久？", "use_rag": True})
    r.raise_for_status()
    run = r.json()
    assert run["status"] == "succeeded", f"运行失败: {run.get('error')}"
    steps = {s["step"] for s in run["steps"]}
    assert "rag" in steps and "intent" in steps and "memory" in steps, f"步骤缺失: {steps}"
    print("output:", run["output_text"][:200].replace("\n", " "))
    print("steps:", [(s["step"], s.get("ms", 0)) for s in run["steps"]])
    print("tokens:", run["token_usage"])

    print("\nALL E2E PASSED ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
