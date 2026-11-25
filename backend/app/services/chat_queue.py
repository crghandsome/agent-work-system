"""对话消息队列与中断机制。

设计（对应需求 4）：
- 每个会话一个串行队列：同一会话的消息按 FIFO 处理，避免并发串话
- 每个提交返回一个 message_id；前端可凭 id 查询状态、取消排队中/执行中的任务
- 中断：取消标志（threading.Event）注入流式生成器；LLM 流每 chunk 检查，取消则立即停止并落库"已取消"
- 进程内实现（单机部署）；多副本时需替换为 Redis Stream + 广播取消信号

不引入 Celery 等重依赖：生成由 FastAPI 的 StreamingResponse 消费生成器驱动，
队列只负责"排队与取消状态"，实际执行仍在 SSE 连接中（断开连接也触发取消）。
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field

from ..models import new_id


@dataclass
class QueuedMessage:
    id: str
    session_id: str
    text: str
    state: str = "queued"          # queued | running | done | canceled | failed
    cancel: threading.Event = field(default_factory=threading.Event)
    created_at: float = field(default_factory=time.time)
    error: str = ""
    # 后台执行结果缓存：SSE 只转发，任务不依赖前端连接存活
    events: list[dict] = field(default_factory=list)   # 已产生的 SSE 事件
    event_lock: threading.Condition = field(default_factory=lambda: threading.Condition(threading.Lock()))
    finished: threading.Event = field(default_factory=threading.Event)


class ConversationQueue:
    """会话级串行队列。"""

    def __init__(self):
        self._lock = threading.RLock()
        self._queues: dict[str, deque[QueuedMessage]] = {}
        self._tasks: dict[str, QueuedMessage] = {}
        self._running: set[str] = set()
        self._condition = threading.Condition(self._lock)

    def submit(self, session_id: str, text: str) -> QueuedMessage:
        msg = QueuedMessage(id=new_id("msg"), session_id=session_id, text=text)
        with self._condition:
            self._reap_stale(session_id)
            self._queues.setdefault(session_id, deque()).append(msg)
            self._tasks[msg.id] = msg
            self._condition.notify_all()
        return msg

    def _reap_stale(self, session_id: str, max_age: float = 30.0) -> None:
        """清理超时残留任务（客户端断开且 finally 未触发时的兜底），避免阻塞队列。"""
        now = time.time()
        q = self._queues.get(session_id)
        if not q:
            return
        stale = [m for m in q if now - m.created_at > max_age and m.state in ("queued", "running")]
        for m in stale:
            m.state = "failed"
            m.error = "超时（客户端断开或执行中断）"
        if stale:
            self._queues[session_id] = deque(m for m in q if m not in stale)
            self._running.discard(session_id)

    def acquire(self, session_id: str, msg_id: str) -> bool:
        """队首取得执行权；非队首返回 False（调用方应等待）。"""
        with self._condition:
            q = self._queues.get(session_id)
            if not q or q[0].id != msg_id:
                return False
            if session_id in self._running:
                return False
            q.popleft()
            msg = self._tasks[msg_id]
            msg.state = "running"
            self._running.add(session_id)
            return True

    def wait_and_acquire(self, session_id: str, msg_id: str, timeout: float = 120) -> bool:
        """等待直到取得执行权或超时/被取消。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._condition:
                self._reap_stale(session_id)
            msg = self._tasks.get(msg_id)
            if not msg or msg.state in ("failed", "canceled"):
                return False
            if msg.cancel.is_set():
                with self._lock:
                    msg.state = "canceled"
                self._drop_from_queue(session_id, msg_id)
                return False
            if self.acquire(session_id, msg_id):
                return True
            with self._condition:
                self._condition.wait(timeout=0.2)
        return False

    def _drop_from_queue(self, session_id: str, msg_id: str) -> None:
        with self._lock:
            q = self._queues.get(session_id)
            if q:
                self._queues[session_id] = deque(m for m in q if m.id != msg_id)

    def finish(self, session_id: str, msg_id: str, state: str = "done", error: str = "") -> None:
        with self._condition:
            msg = self._tasks.get(msg_id)
            if msg:
                # 已取消的消息不被后续完成覆盖
                if msg.state != "canceled":
                    msg.state = state
                    msg.error = error
                msg.finished.set()
            self._running.discard(session_id)
            self._condition.notify_all()

    def cancel(self, msg_id: str) -> bool:
        """中断：设置取消标志。执行中的流会在下一个 chunk 检测到并停止。"""
        with self._condition:
            msg = self._tasks.get(msg_id)
            if not msg or msg.state in ("done", "canceled", "failed"):
                return False
            msg.cancel.set()
            if msg.state == "queued":
                msg.state = "canceled"
                self._drop_from_queue(msg.session_id, msg_id)
            else:
                # running：立即标记 canceled（流式生成器会在下个 chunk 停止并落库）
                msg.state = "canceled"
            msg.finished.set()
            with msg.event_lock:
                msg.event_lock.notify_all()
            self._condition.notify_all()
            return True

    def cancel_session(self, session_id: str) -> int:
        """中断会话内所有排队/执行中的消息。"""
        with self._condition:
            ids = [m.id for m in self._tasks.values()
                   if m.session_id == session_id and m.state in ("queued", "running")]
        count = 0
        for mid in ids:
            if self.cancel(mid):
                count += 1
        return count

    def status(self, msg_id: str) -> dict | None:
        with self._lock:
            m = self._tasks.get(msg_id)
            if not m:
                return None
            return {"id": m.id, "session_id": m.session_id, "state": m.state,
                    "canceled": m.cancel.is_set(), "error": m.error}

    def append_event(self, msg_id: str, event: dict) -> None:
        """后台执行器写入一个 SSE 事件，唤醒所有等待的 SSE 消费者。"""
        msg = self._tasks.get(msg_id)
        if not msg:
            return
        with msg.event_lock:
            msg.events.append(event)
            msg.event_lock.notify_all()

    def iter_events(self, msg_id: str, start_index: int = 0, timeout: float = 0.5):
        """SSE 消费者：从 start_index 起产生新事件；任务结束且无更多事件时返回 None。

        这是生成器：不断等待新事件，前端断连只是停止消费，不影响后台任务继续写。
        """
        msg = self._tasks.get(msg_id)
        if not msg:
            return
        idx = start_index
        while True:
            with msg.event_lock:
                while idx >= len(msg.events) and not msg.finished.is_set():
                    msg.event_lock.wait(timeout=timeout)
                if idx >= len(msg.events):
                    return  # finished 且无更多事件
                ev = msg.events[idx]
                idx += 1
            yield ev

    def message_snapshot(self, msg_id: str) -> dict | None:
        """返回消息的事件快照（供前端重连时重建进行中的对话）。"""
        msg = self._tasks.get(msg_id)
        if not msg:
            return None
        with msg.event_lock:
            events = list(msg.events)
        return {"id": msg.id, "session_id": msg.session_id, "text": msg.text,
                "state": msg.state, "events": events}

    def running_sessions(self) -> list[dict]:
        """返回每个会话的运行状态（供会话列表显示转圈/完成）。"""
        with self._lock:
            out = []
            for sid in set(self._queues) | self._running:
                msgs = [m for m in self._tasks.values() if m.session_id == sid
                        and m.state in ("queued", "running")]
                if msgs:
                    out.append({"session_id": sid, "running": True,
                                "message_ids": [m.id for m in msgs],
                                "messages": [{"id": m.id, "text": m.text[:80],
                                              "state": m.state,
                                              "events": list(m.events)} for m in msgs]})
            return out

    def session_pending(self, session_id: str) -> list[dict]:
        with self._lock:
            out = []
            for m in self._tasks.values():
                if m.session_id == session_id and m.state in ("queued", "running"):
                    out.append({"id": m.id, "state": m.state, "text": m.text[:40]})
            return out


queue = ConversationQueue()
