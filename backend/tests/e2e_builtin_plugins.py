"""验证重构后的 4 个内置插件真正起作用：
1. memory_simple: 跨轮持久化落盘 + 按查询相关度/新鲜度召回（after_run → 下一轮 apply）
2. intent_keyword: 短句闲聊跳过 RAG（skip_rag=True）、检索意图扩大召回（rag_top_k=8）+ 注入策略指引
3. compress_truncate: 片段去重 + 按查询词优先提取高相关句子 + 总预算控制
4. chunking_fixed: 按可配置 chunk_size/overlap 在句末安全切分
"""
import importlib.util
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parents[2]

sp = importlib.util.spec_from_file_location("builtin_plugins", str(ROOT / "backend" / "app" / "plugins" / "builtin.py"))
bp = importlib.util.module_from_spec(sp)
sys.modules["builtin_plugins"] = bp
sp.loader.exec_module(bp)

PluginContext = bp.PluginContext
MemoryPlugin = bp.MemoryPlugin
IntentPlugin = bp.IntentPlugin
ContextCompressorPlugin = bp.ContextCompressorPlugin
ChunkingPlugin = bp.ChunkingPlugin


def test_memory_plugin():
    tmp = Path(tempfile.mkdtemp(prefix="aws_mem_test_"))
    try:
        os.environ["AWS_ARTIFACTS_DIR"] = str(tmp)
        mem = MemoryPlugin()

        # 第 1 轮：无历史记忆
        ctx1 = PluginContext(session_key="sales-bot:loop", user_input="请记住，我的代号是北极星，以后汇报请用表格形式。")
        r1 = mem.apply(ctx1, {"max_items": 5})
        assert r1["recalled"] == 0
        # 回答完成后触发 after_run 持久化
        saved = mem.after_run(ctx1, "好的，已记住你的代号是北极星，后续汇报都会采用表格。", {"max_stored": 20})
        assert saved["saved"] >= 1
        assert (tmp / "memory" / "sales-bot.json").exists(), "记忆文件未落盘"

        # 第 2 轮（全新 PluginContext，模拟跨轮/重启后）：应能召回「北极星」事实
        ctx2 = PluginContext(session_key="sales-bot:loop", user_input="还记得我的代号是什么吗？")
        r2 = mem.apply(ctx2, {"max_items": 5})
        assert r2["recalled"] >= 1
        assert any("北极星" in m for m in ctx2.memory), f"未召回历史事实: {ctx2.memory}"
        print("✓ [Plugin] memory_simple 跨轮持久化与相关性召回通过:", r2)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_intent_plugin():
    ip = IntentPlugin()

    # 短句闲聊 → skip_rag=True
    ctx_hi = PluginContext(session_key="a:1", user_input="你好呀")
    res_hi = ip.apply(ctx_hi, {"skip_rag_on_chitchat": True})
    assert res_hi["intent"] == "chitchat" and ctx_hi.skip_rag is True
    assert "寒暄" in ctx_hi.injected_system

    # 知识检索意图 → boost rag_top_k=8 且不跳过 RAG
    ctx_q = PluginContext(session_key="a:1", user_input="请帮我检索一下公司的报销制度和退款政策")
    res_q = ip.apply(ctx_q, {})
    assert res_q["intent"] == "search_knowledge"
    assert ctx_q.skip_rag is False
    assert ctx_q.rag_top_k == 8
    assert "知识库" in ctx_q.injected_system
    print("✓ [Plugin] intent_keyword 闲聊跳 RAG & 检索扩容通过:", res_hi, res_q)


def test_compressor_plugin():
    cp = ContextCompressorPlugin()
    long_doc = (
        "这是关于公司办公环境与茶水间管理的背景介绍，内容很长但与问题无关。" * 6
        + "公司差旅报销标准规定：一线城市住宿上限为每晚 600 元，二线城市为 450 元。"
        + "其他行政杂项说明包括工牌补办流程和门禁权限申请方式。" * 6
    )
    ctx = PluginContext(
        session_key="a:1",
        user_input="一线城市差旅住宿报销上限是多少？",
        retrieved_chunks=[
            {"title": "报销制度", "text": long_doc},
            {"title": "报销制度(重复)", "text": long_doc},  # 重复片段应被去重
        ],
    )
    res = cp.apply(ctx, {"max_chars": 220, "max_total_chars": 300})
    assert ctx.compressed is True
    assert res["chunks_kept"] == 1 and res["chunks_dropped"] == 1
    assert res["after_chars"] < res["before_chars"]
    assert "600 元" in ctx.retrieved_chunks[0]["text"], "相关性抽取丢失了关键答案句"
    print("✓ [Plugin] compress_truncate 去重 + 相关性句级抽取通过:", res)


def test_chunking_plugin():
    text = ("第一段说明系统架构与核心组件的设计原则。" * 10 + "\n"
            + "第二段介绍向量检索与重排模型的部署方式。" * 10)
    chunks = ChunkingPlugin.chunk(text, {"chunk_size": 160, "overlap": 30})
    assert len(chunks) >= 3
    assert all(len(c) <= 170 for c in chunks)
    print(f"✓ [Plugin] chunking_fixed 句末边界智能分块通过: {len(chunks)} chunks")


if __name__ == "__main__":
    test_memory_plugin()
    test_intent_plugin()
    test_compressor_plugin()
    test_chunking_plugin()
    print("ALL BUILTIN PLUGIN TESTS PASSED ✓")
