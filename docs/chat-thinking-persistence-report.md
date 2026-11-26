# 对话缓存与实时思考过程持久化——问题清单、根因分析与修复方案

> **排查范围**：智能对话前后端全链路（`backend/web/index.html`、`backend/app/services/chat_queue.py`、`backend/app/routers/api_full.py`、`backend/app/services/orchestrator_service.py`）  
> **核心症状**：在智能对话发布任务后，切换到其他功能页（如 MCP/知识库/Skill 市场）或切换到其他会话，再切回原对话（或刷新页面）时，看不到 Agent 正在思考的实时过程、子 Agent 执行轨迹丢失、偶发重复用户气泡或误报「已中断 / 任务已完成」。

---

## 一、问题清单总览

| 编号 | 所属层 | 严重级别 | 问题现象 | 涉及文件 | 状态 |
|---|---|---|---|---|---|
| **P1** | 前端 | 严重 | 切页/切会话断开 SSE 时被误判为「用户中断」，向 `ORCH_CACHE` 写入空回复污染缓存 | `backend/web/index.html` | 已修复 |
| **P2** | 前端 | 严重 | `orchResumeRunning()` 切回重连时完全忽略子 Agent 实时事件（`phase === 'sub'`）与 `status` 状态事件 | `backend/web/index.html` | 已修复 |
| **P3** | 前端 | 严重 | `orchResumeRunning()` 恢复进行中的思考链时，`details.think` 默认折叠、文案写死「已深度思考」、缺少 `.live` 状态与自动滚动 | `backend/web/index.html` | 已修复 |
| **P4** | 前端 | 高 | 切回执行中会话时出现重复用户消息气泡；二次切走导致 `orchResumeRunning()` 误判为「任务已完成」并错误重绘；刷新页面丢失当前会话定位 | `backend/web/index.html` | 已修复 |
| **P5** | 后端 | 严重 | `chat_queue.py` 截断用户消息为 80 字符、无锁拷贝 `events` 存在线程竞争、`_reap_stale()` 在长任务 >30s 时误杀排队消息并破坏 `_running` 集合、`finish()` 未即时唤醒 SSE 消费者 | `backend/app/services/chat_queue.py` | 已修复 |
| **P6** | 后端 + 前端 | 严重 | 委托子 Agent 模式下，主 Agent 综合思考链 `_sum_reason` 与子 Agent 过程 `sub_events` 未写入数据库历史，且历史渲染未恢复委托与子 Agent 区块 | `backend/app/services/orchestrator_service.py`、`backend/web/index.html` | 已修复 |
| **P7** | 后端 | 中 | 后台任务异常分支先调用 `queue.finish()` 再 `append_event(error)`，导致重连/消费端可能丢失错误事件 | `backend/app/routers/api_full.py` | 已修复 |
| **P8** | 后端 + 前端 | 中 | 切回页面时先拉 `/orchestrator/running` 再拉 `/sessions/{sid}`，快照与重连之间存在时间窗口，且 `done` 事件未完整合并 `reasoning`/`files`/`delegations` | `backend/app/routers/api_full.py`、`backend/web/index.html` | 已修复 |

---

## 二、问题根因与修复方案详解

### P1：切页/切会话被误判为「任务中断」，污染本地对话缓存

- **造成原因**：
  - 切换左侧导航（`showView()`）或切换会话（`orchSwitch()`）时都会调用 `orchStop(false)`，主动执行 `orchStreamAbort.abort()` 断开前端 SSE 读取（预期仅断开前端连接，后台任务继续运行）。
  - 但 `orchSend()` 的 `catch` 块未区分断开原因，只要捕获到 `AbortError` 就直接置 `canceled = true`，删除了 `pending` 占位并往 `ORCH_CACHE[sid]` 写入一条 `{ role: 'assistant', content: '' }` 的空完成记录，同时向 DOM 追加「已中断」。
- **解决方法**：
  - 引入中断原因标识 `orchAbortReason`（`'leave'` 表示切页/切会话离开，`'cancel'` 表示用户主动点击停止）。
  - 在 `orchSend()` 与 `orchResumeRunning()` 中，当 `AbortError` 且 `orchAbortReason === 'leave'` 时直接安静退出（`return`），**不标记 `canceled`、不写「已中断」、不向 `ORCH_CACHE` 写入脏数据**，等待切回时无缝接管。

---

### P2：`orchResumeRunning()` 漏处理子 Agent 实时过程（`phase === 'sub'`）与 `status` 事件

- **造成原因**：
  - 前期迭代中只给 `orchSend()` 添加了 `ev.phase === 'sub'`（渲染 `.sub-process` 子 Agent 思考/工具调用/工具返回）和 `ev.type === 'status'`（渲染「思考中… / 正在调用子 Agent…」），而**切回重连函数 `orchResumeRunning()` 维护着一份独立的、过时的 `if/else` 分支**，无论是回放快照还是续读新 SSE 帧都完全丢弃了这两类事件。
- **解决方法**：
  - 抽取统一的事件渲染引擎 `createOrchTurn()` + `applyOrchEvent(turn, state, ev, body, isReplay)`，让 `orchSend()`、`orchResumeRunning()` 快照回放和 `orchResumeRunning()` SSE 续读**完全共用同一套事件处理逻辑**，消除双份代码漂移问题。

---

### P3：切回后思考块默认折叠且文案写死「已深度思考」

- **造成原因**：
  - `orchResumeRunning()` 回放 `m.events` 中的 `reasoning` 后，硬编码插入 `<details class="think">`（缺少 `open` 属性和 `live` 样式类，标签固定写死「已深度思考」）。
  - 在后续 SSE 续读 `reasoning` 时，既未添加 `.live` 脉冲提示，也未更新 `body.scrollTop = body.scrollHeight` 自动滚到底部。
- **解决方法**：
  - 在 `applyOrchEvent()` 与 `orchResumeRunning()` 回放结束后，只要任务尚未收到 `done`/`canceled` 且尚未进入正文输出，`details.think` 自动保持 **`open` 展开态 + `.live` 呼吸脉冲态**，标签动态显示「思考中… / 主 Agent 整理中…」，并在收到新 chunk 时实时滚动到底部；直到正文开始流式输出或任务完成才切换为「已深度思考」。

---

### P4：用户气泡重复渲染、二次切走误判「任务已完成」、刷新丢失当前会话

- **造成原因**：
  - `views.chat` / `orchSwitch` 先通过 `ORCH_CACHE` 里的 `pending` 渲染了一遍用户消息，随后 `orchResumeRunning()` 又无条件 `insertAdjacentHTML('beforeend', '<div class="turn user">...')`，导致同一条提问出现两次。
  - 当用户在 `orchResumeRunning()` 期间**再次切到其他导航页**时，`orch.id` 仍然等于 `sid`；`AbortError` 跳过 `catch` 后落入收尾逻辑，由于 `if (orch.id === sid)` 仍为真，错误地将内容改写为「任务已完成」并重置 `orch.loading = false`。
  - 刷新页面（F5）后 `orch.id` 为 `null`，`views.chat` 默认选中列表第一条而非用户当前正在操作的会话。
- **解决方法**：
  - `orchResumeRunning()` 插入用户气泡前检查末尾是否已存在同内容用户气泡，杜绝重复。
  - 离开判断同时校验 `leftPage || orch.id !== sid || !document.querySelector('#orch-body')`，并用局部 `myAbort` 比对 `orchStreamAbort === myAbort`，彻底隔离多次切页/切会话的并发生命周期。
  - 将当前会话 ID 持久化到 `localStorage('aws-orch-sid')`，刷新页面后自动回到原会话并恢复进行中的实时思考过程。

---

### P5：后端 `ConversationQueue` 文本截断、无锁读竞态与 `_reap_stale()` 误杀长任务

- **造成原因**：
  - `running_sessions()` 返回 `"text": m.text[:80]`，超过 80 字的提问切回后被截断，且与前端原文不匹配；同时 `list(m.events)` 未持有 `m.event_lock`，在后台线程高频写入时存在线程安全隐患。
  - `_reap_stale(max_age=30.0)` 清理超时项时无条件执行 `self._running.discard(session_id)`：一旦某会话有耗时超过 30 秒的深度思考/报告生成任务且队列中有第 2 条排队消息，第 2 条被误杀的同时还会把正在执行的会话从 `_running` 中移除，导致 `/orchestrator/running` 不再返回该会话，前端切回后彻底丢失正在运行的任务。
  - `finish()` 置位 `msg.finished` 后未调用 `msg.event_lock.notify_all()`，导致 `iter_events()` 最多额外空等 0.5 秒才结束。
- **解决方法**（`backend/app/services/chat_queue.py`）：
  - `running_sessions()` 返回完整 `m.text`，并在 `with m.event_lock:` 保护下拷贝 `list(m.events)`。
  - `_reap_stale()` 默认超时调整为 `600.0s`，仅清理 `state == "queued"` 的条目，且仅在确认该会话没有任何 `state == "running"` 任务时才移除 `self._running`。
  - `finish()` 内在 `with msg.event_lock:` 下调用 `msg.event_lock.notify_all()`，即时唤醒所有 SSE 消费者。

---

### P6：委托模式下主思考链与子 Agent 过程未落库持久化

- **造成原因**：
  - `backend/app/services/orchestrator_service.py` 的委托分支虽然用 `_sum_reason` 收集了主 Agent 综合思考链、用 `sub_sink` 转发了子 Agent 实时事件，但在落库 `_append(...)` 和发送 `done` 事件时将 `reasoning` 硬编码为空字符串 `""`，且未保存 `sub_events`。
  - 前端 `orchRenderHistory()` 在渲染历史消息时也未渲染 `delegations` 和 `sub_events`，导致任务结束后一刷新或切页，委托轨迹与子 Agent 过程就永久消失。
- **解决方法**：
  - `orchestrator_service.py` 委托分支完整收集 `sub_events`（对超长 observation 做合理截断防膨胀），并将 `"".join(_sum_reason).strip()` 作为 `reasoning` 与 `sub_events` 一并传入 `_append()` 落入 `OrchestratorSession.history` 及 `done` 事件。
  - 前端 `orchRenderHistory()` 新增 `renderDelegationsHtml()` 与 `renderSubEventsHtml()`，刷新/切回后完整还原历史中的委托徽章、子 Agent 执行过程、主 Agent 思考链与生成文件卡片。

---

### P7：异常分支 `finish()` 与 `append_event(error)` 顺序颠倒

- **造成原因**：
  - `backend/app/routers/api_full.py` 的 `background_run()` 异常处理中先调用了 `queue.finish(...)`（置位 `msg.finished`），再调用 `queue.append_event(error)`。SSE 消费者可能在 `finished` 置位瞬间退出，错过 `error` 事件。
- **解决方法**：
  - 调换顺序：先 `queue.append_event(...)` 写入错误事件，再调用 `queue.finish(...)`。

---

### P8：切回对账与快照拉取存在竞态窗口

- **造成原因**：
  - `views.chat` / `orchSwitch` 先拉 `/orchestrator/running` 拿到旧快照，再 `await` 拉 `/orchestrator/sessions/{sid}`，最后才用旧快照调 `orchResumeRunning()`，期间产生的新事件可能产生对账缺口。
- **解决方法**：
  - 后端新增 `GET /orchestrator/messages/{mid}/snapshot` 原子快照接口。
  - 前端调整顺序为「先拉权威历史 `/sessions/{sid}` → 再拉 `/orchestrator/running` → `orchResumeRunning()` 启动时拉取 `/messages/{mid}/snapshot` → 从 `from_index = snap.events.length` 续读 SSE」，实现零丢帧、零重复无缝衔接。

---

## 三、全面自动化测试验证结果

已新增自包含自动化测试脚本 `backend/tests/e2e_thinking_persist.py`（基于真实 `ConversationQueue` 与真实 `backend/web/index.html` 在 Playwright 无头浏览器中全链路验证），全部通过：

1. **后端队列单元测试（`test_backend_queue`）**：
   - 验证 >160 字符长提问在 `running_sessions()` 中完整保留不截断；
   - 验证长任务（>120s）运行期间 `_reap_stale()` 不会误杀 45s 排队消息，也不会在清理超长排队消息时误删运行中会话的 `_running` 标记；
   - 验证 `message_snapshot()` + `iter_events(from_index=len(snap))` 零丢帧零重复，且 `finish()` 唤醒耗时 `< 0.25s`。
2. **E2E 场景 A / C / D（主 Agent 实时思考切页 / 切会话 / 二次切走）**：
   - 发消息进入深度思考 → 切到 MCP 页再切回 → 思考块保持 `open` 展开态与 `.live` 脉冲态，无重复用户气泡、无「已中断」；
   - 再次切到另一会话再切回 → 无「任务已完成」误判 → 实时续读第二阶段思考与最终正文。
3. **E2E 场景 B（子 Agent 委托执行中途切页恢复）**：
   - 触发子 Agent 思考与工具调用 → 切到知识库页再切回 → `.deli` 委托标签与 `.sub-process` 子 Agent 步骤完整恢复，并实时续读工具结果、生成文件卡片与主 Agent 综合回复。
4. **E2E 场景 E（刷新页面 F5 全量历史持久化）**：
   - 刷新浏览器后自动定位回原会话，主 Agent 思考链、子 Agent 过程、委托徽章与生成文件卡片全部完整渲染，页面零 JS 报错。
