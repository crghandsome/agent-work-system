"""把一个同步、带回调用的函数包装成可流式消费的事件生成器。

用途：ReAct 循环是同步函数（run_agent_loop(on_event=...)），
但 SSE 端点需要边执行边推送事件。用后台线程 + 队列桥接：
- 后台线程跑同步函数，on_event 把事件放入队列
- SSE 生成器从队列取事件实时 yield
"""
from __future__ import annotations

import queue
import threading


END = {"type": "__end__"}


def stream_callable(fn, poll_timeout: float = 0.5):
    """fn(emit) 在后台线程执行；生成器实时 yield fn 通过 emit 发出的事件。"""
    q: queue.Queue = queue.Queue()

    def runner():
        def emit(ev: dict):
            q.put(ev)
        try:
            fn(emit)
        except Exception as exc:
            q.put({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
        finally:
            q.put(END)

    threading.Thread(target=runner, daemon=True).start()
    while True:
        try:
            ev = q.get(timeout=poll_timeout)
        except queue.Empty:
            continue
        if ev is END or ev.get("type") == "__end__":
            break
        yield ev
