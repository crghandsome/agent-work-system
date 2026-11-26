"""全面测试：对话任务切换页面/切换会话/刷新后，实时思考过程、子 Agent 过程与历史持久化。

覆盖：
1. 后端 ConversationQueue 单元测试：长文本不截断、并发快照与 SSE 续读无丢帧、长任务不被 _reap_stale 误杀、finish 即时唤醒。
2. 全链路浏览器 E2E（真实 ConversationQueue + 真实 backend/web/index.html）：
   - 场景 A：主 Agent 深度思考中途切到其他页面再切回 → 思考块自动展开（open + live）且实时续读后续思考与正文
   - 场景 B：子 Agent 委托执行中途切走再切回 → 委托标签、子 Agent 实时思考/工具步骤完整恢复并续读，交付文件卡片正常呈现
   - 场景 C：执行中切换到其他会话再切回 → 无重复用户气泡、无误报「已中断」
   - 场景 D：单次任务执行期间连续两次切走再切回 → 不会误判为「任务已完成」
   - 场景 E：任务完成落库后切页/刷新页面（F5）→ 恢复当前会话，且主思考链、委托徽章、子 Agent 过程、文件卡片全部持久化展示
"""
import importlib.util
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ── 直接加载真实的 ConversationQueue（不依赖 sqlalchemy/mysql）──
ROOT = Path(__file__).resolve().parents[2]
INDEX_HTML = (ROOT / "backend" / "web" / "index.html").read_bytes()

# 为 chat_queue 提供轻量的 models.new_id 桩，直接导入真实模块
import types
pkg_app = types.ModuleType("app")
pkg_models = types.ModuleType("app.models")
_id_seq = 0
def _new_id(prefix: str = "id") -> str:
    global _id_seq
    _id_seq += 1
    return f"{prefix}_{_id_seq}"
pkg_models.new_id = _new_id
pkg_services = types.ModuleType("app.services")
sys.modules["app"] = pkg_app
sys.modules["app.models"] = pkg_models
sys.modules["app.services"] = pkg_services

spec = importlib.util.spec_from_file_location(
    "app.services.chat_queue",
    str(ROOT / "backend" / "app" / "services" / "chat_queue.py"),
)
cq_mod = importlib.util.module_from_spec(spec)
sys.modules["app.services.chat_queue"] = cq_mod
spec.loader.exec_module(cq_mod)
ConversationQueue = cq_mod.ConversationQueue


def test_backend_queue():
    q = ConversationQueue()

    # 1. 长文本不截断（修复前 m.text[:80] 会截断）
    long_text = "请帮我分析这份超过八十个字符的详细业务需求报告，并生成包含完整财务指标、渠道转化率、留存趋势以及风险提示的 Word 日报文档和 JSON 原始指标数据文件。" * 2
    m1 = q.submit("s_long", long_text)
    assert q.acquire("s_long", m1.id)
    running = q.running_sessions()
    assert len(running) == 1
    assert running[0]["messages"][0]["text"] == long_text, "长文本在 running_sessions 中被截断"

    # 2. 长任务超过 30 秒时，提交第二条排队消息不能把正在运行的会话从 _running 误删
    m1.created_at -= 120
    m2 = q.submit("s_long", "第二条排队消息")
    m2.created_at -= 45  # 45s < 600s，不应被误杀
    q._reap_stale("s_long")
    assert m2.state == "queued", f"排队消息在 45s 被误杀: {m2.state}"
    assert "s_long" in q._running, "正在运行的会话被 _reap_stale 误删"

    # 即使第二条超过 600s 被清理，仍在运行的 s_long 也不应从 _running 移除
    m2.created_at -= 700
    q._reap_stale("s_long")
    assert m2.state == "failed"
    assert "s_long" in q._running, "清理超时排队消息时误删了正在运行会话的 _running 标记"

    # 3. 快照 + from_index 续读零丢帧、零重复，且 finish 即时唤醒（< 0.2s）
    q.append_event(m1.id, {"type": "status", "stage": "thinking"})
    q.append_event(m1.id, {"type": "reasoning", "delta": "步骤1"})
    snap = q.message_snapshot(m1.id)
    assert len(snap["events"]) == 2

    received = []
    def consumer():
        for ev in q.iter_events(m1.id, start_index=len(snap["events"])):
            received.append(ev)

    t = threading.Thread(target=consumer)
    t.start()
    time.sleep(0.05)
    q.append_event(m1.id, {"type": "reasoning", "delta": "步骤2"})
    q.append_event(m1.id, {"type": "done", "reply": "完成"})
    t0 = time.time()
    q.finish("s_long", m1.id, "done")
    t.join(timeout=1.0)
    elapsed = time.time() - t0
    assert not t.is_alive(), "finish 后 SSE 消费者未及时退出"
    assert elapsed < 0.25, f"finish 唤醒过慢: {elapsed:.3f}s"
    assert [e["type"] for e in received] == ["reasoning", "done"]
    print("✓ [Backend] ConversationQueue 单元测试全部通过")


def test_frontend_e2e():
    from playwright.sync_api import sync_playwright

    q = ConversationQueue()
    sessions = {
        "s1": {"session_id": "s1", "title": "测试主会话", "history": []},
        "s2": {"session_id": "s2", "title": "其他会话", "history": [
            {"role": "user", "content": "旧消息"},
            {"role": "assistant", "content": "旧回复", "reasoning": "", "files": [], "delegations": [], "sub_events": []},
        ]},
    }
    # 控制后台流式任务推进节奏的信号
    gates: dict[str, threading.Event] = {}

    def run_simulated_task(sid: str, msg_id: str, text: str, mode: str):
        q.append_event(msg_id, {"type": "queued", "message_id": msg_id, "pending": 0})
        assert q.wait_and_acquire(sid, msg_id, timeout=10)
        q.append_event(msg_id, {"type": "status", "stage": "thinking"})
        sub_events = []
        delegations = []
        files = []
        reasoning_parts = []

        if mode == "sub":
            q.append_event(msg_id, {"type": "status", "stage": "delegating", "agent": "report-agent"})
            e1 = {"type": "thought", "phase": "sub", "sub_agent": "report-agent", "text": "子Agent正在拉取今日经营指标"}
            e2 = {"type": "tool", "phase": "sub", "sub_agent": "report-agent", "action": "query_metrics"}
            sub_events.extend([e1, e2])
            q.append_event(msg_id, e1)
            q.append_event(msg_id, e2)
            # 等待前端切走再切回后放行第二阶段事件
            gates[msg_id].wait(timeout=15)
            e3 = {"type": "observation", "phase": "sub", "sub_agent": "report-agent", "text": "指标查询成功：总营收 128 万"}
            sub_events.append(e3)
            q.append_event(msg_id, e3)
            deli = {"type": "delegation", "agent": "report-agent", "state": "completed", "reason": "日报取数与排版", "task_id": "t1"}
            delegations.append({"agent": "report-agent", "state": "completed", "reason": "日报取数与排版", "task_id": "t1"})
            q.append_event(msg_id, deli)
            fobj = {"name": "daily-report.docx", "size": 9216, "url": "/files/run-test/daily-report.docx"}
            files.append(fobj)
            q.append_event(msg_id, {"type": "file", **fobj})
            q.append_event(msg_id, {"type": "reasoning", "delta": "主Agent汇总子Agent结果并校对数据。"})
            reasoning_parts.append("主Agent汇总子Agent结果并校对数据。")
            reply = "今日日报已生成完毕，点击下方卡片下载。"
            q.append_event(msg_id, {"type": "content", "delta": reply})
        else:
            q.append_event(msg_id, {"type": "reasoning", "delta": "第一阶段思考：正在拆解问题结构…\n"})
            reasoning_parts.append("第一阶段思考：正在拆解问题结构…\n")
            gates[msg_id].wait(timeout=15)
            q.append_event(msg_id, {"type": "reasoning", "delta": "第二阶段思考：切回后实时续读的思考内容！"})
            reasoning_parts.append("第二阶段思考：切回后实时续读的思考内容！")
            time.sleep(0.35)
            reply = "这是最终的完整回答内容。"
            q.append_event(msg_id, {"type": "content", "delta": reply})

        rsn_total = "".join(reasoning_parts)
        sessions[sid]["history"].extend([
            {"role": "user", "content": text},
            {
                "role": "assistant", "content": reply, "reasoning": rsn_total,
                "files": files, "delegations": delegations, "sub_events": sub_events,
            },
        ])
        q.append_event(msg_id, {
            "type": "done", "reply": reply, "reasoning": rsn_total,
            "files": files, "delegations": delegations, "sub_events": sub_events,
            "message_id": msg_id,
        })
        q.finish(sid, msg_id, "done")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def _json(self, obj, code=200):
            data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _sse(self, mid: str, start_index: int):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            try:
                for ev in q.iter_events(mid, start_index=start_index):
                    frame = f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode("utf-8")
                    self.wfile.write(frame)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

        def do_GET(self):
            u = urlparse(self.path)
            p = u.path
            if p in ("/", "/console"):
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(INDEX_HTML)))
                self.end_headers()
                self.wfile.write(INDEX_HTML)
            elif p == "/orchestrator/sessions":
                self._json([{"session_id": k, "title": v["title"]} for k, v in sessions.items()])
            elif p == "/orchestrator/running":
                self._json(q.running_sessions())
            elif p.startswith("/orchestrator/sessions/"):
                sid = p.split("/")[-1]
                self._json(sessions.get(sid, {"history": []}))
            elif p.startswith("/orchestrator/messages/") and p.endswith("/snapshot"):
                mid = p.split("/")[-2]
                snap = q.message_snapshot(mid)
                self._json(snap or {"detail": "not found"}, 200 if snap else 404)
            elif p.startswith("/orchestrator/messages/") and p.endswith("/stream"):
                mid = p.split("/")[-2]
                qs = parse_qs(u.query)
                fi = int(qs.get("from_index", ["0"])[0])
                self._sse(mid, fi)
            else:
                self._json([])

        def do_POST(self):
            u = urlparse(self.path)
            p = u.path
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length else b"{}"
            body = json.loads(raw.decode("utf-8") or "{}")
            if p.startswith("/orchestrator/sessions/") and p.endswith("/chat/stream"):
                sid = p.split("/")[-3]
                text = body.get("text", "")
                msg = q.submit(sid, text)
                gates[msg.id] = threading.Event()
                mode = "sub" if "日报" in text else "think"
                threading.Thread(target=run_simulated_task, args=(sid, msg.id, text, mode), daemon=True).start()
                self._sse(msg.id, 0)
            else:
                self._json({"ok": True})

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"

    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        pg = b.new_page(viewport={"width": 1360, "height": 860})
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.goto(f"{base}/console", wait_until="domcontentloaded")
        pg.wait_for_selector("#orch-in", timeout=10000)

        # ── 场景 A + C + D：主 Agent 深度思考中途切页、切会话、二次切走再切回 ──
        pg.locator("#orch-in").fill("请分析微服务架构演进方案")
        pg.locator("#orch-go").click()
        pg.wait_for_function("() => document.querySelector('details.think .think-body')?.textContent.includes('第一阶段思考')", timeout=5000)
        mid1 = list(gates.keys())[-1]

        # 切到其他页面（MCP 市场）再切回智能对话
        pg.locator('nav button[data-p="mcp"]').click()
        pg.wait_for_timeout(250)
        pg.locator('nav button[data-p="chat"]').click()
        pg.wait_for_function("() => document.querySelector('details.think .think-body')?.textContent.includes('第一阶段思考')", timeout=5000)

        # 断言：思考块处于展开 + live 状态，无重复用户气泡，无「已中断」
        assert pg.locator("details.think").last.evaluate("e => e.open === true && e.classList.contains('live')"), "切回后思考块未展开或缺少 live 状态"
        assert pg.locator("#orch-body .turn.user").count() == 1, f"用户气泡重复: {pg.locator('#orch-body .turn.user').count()}"
        assert "已中断" not in pg.locator("#orch-body").inner_text(), "切页被误判为已中断"

        # 二次切走（切换到会话 s2 再切回 s1）
        pg.locator(".sess-item").nth(1).click()
        pg.wait_for_timeout(250)
        pg.locator(".sess-item").nth(0).click()
        pg.wait_for_function("() => document.querySelector('details.think .think-body')?.textContent.includes('第一阶段思考')", timeout=5000)
        assert "任务已完成" not in pg.locator("#orch-body").inner_text(), "二次切走误判为任务已完成"
        assert pg.locator("#orch-body .turn.user").count() == 1, "切会话后用户气泡重复"

        # 放行第二阶段实时思考与正文
        gates[mid1].set()
        pg.wait_for_function("() => document.querySelector('details.think .think-body')?.textContent.includes('第二阶段思考')", timeout=5000)
        pg.wait_for_function("() => document.querySelector('#orch-body .turn.ai .content')?.textContent.includes('这是最终的完整回答内容')", timeout=5000)
        print("✓ [E2E 场景 A/C/D] 主 Agent 实时思考切页/切会话/二次切走恢复与续读全部通过")

        # ── 场景 B：子 Agent 委托执行中途切走再切回 ──
        pg.locator("#orch-in").fill("帮我生成今天的经营日报")
        pg.locator("#orch-go").click()
        pg.wait_for_function("() => document.querySelector('.sub-process')?.textContent.includes('query_metrics')", timeout=5000)
        mid2 = list(gates.keys())[-1]

        # 切到知识库页面再切回
        pg.locator('nav button[data-p="rag"]').click()
        pg.wait_for_timeout(250)
        pg.locator('nav button[data-p="chat"]').click()
        pg.wait_for_function("() => document.querySelector('.sub-process')?.textContent.includes('query_metrics')", timeout=5000)
        assert pg.locator("#orch-body .deli").count() >= 1, "切回后委托状态丢失"

        # 放行子 Agent 后续事件 + 文件生成 + 主 Agent 综合思考与回答
        gates[mid2].set()
        pg.wait_for_function("() => document.querySelectorAll('#orch-body .deliver-file').length >= 1", timeout=5000)
        pg.wait_for_function("() => document.querySelector('#orch-body').innerText.includes('今日日报已生成完毕')", timeout=5000)
        print("✓ [E2E 场景 B] 子 Agent 实时过程、委托轨迹与生成文件卡片切页恢复通过")

        # ── 场景 E：刷新页面（F5）后完整历史持久化验证 ──
        pg.reload(wait_until="domcontentloaded")
        pg.wait_for_function("() => document.querySelector('#orch-body')?.innerText.includes('今日日报已生成完毕')", timeout=5000)
        assert pg.locator("#orch-body .sub-process").count() >= 1, "刷新后子 Agent 过程历史未持久化展示"
        assert pg.locator("#orch-body .deli").count() >= 1, "刷新后委托徽章未持久化展示"
        assert pg.locator("#orch-body details.think").count() >= 2, "刷新后主 Agent 思考链未持久化展示"
        assert pg.locator("#orch-body .deliver-file").count() >= 1, "刷新后文件卡片未持久化展示"
        print("✓ [E2E 场景 E] 刷新页面后会话定位 + 主思考链 + 子 Agent 过程 + 委托徽章 + 文件卡片全部持久化通过")

        assert not errs, f"页面存在 JS 错误: {errs}"
        b.close()
    srv.shutdown()
    print("ALL PERSISTENCE TESTS PASSED ✓")


if __name__ == "__main__":
    test_backend_queue()
    test_frontend_e2e()
