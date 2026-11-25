# Agent Work System

基于《架构设计方案 v1.1》的全量实现（阶段2核心 + 市场准入 + A2A + 计量审计 + Web 控制台）。

## 技术栈

- 后端：Python 3.11 + FastAPI + SQLAlchemy 2.x（独立 MySQL 8.0 容器，新库 `agent_work_system`）
- 向量库：Qdrant 独立容器（collection `aws_chunks`）
- 向量/重排模型：本地 ONNX 模型（bge-m3 向量 1024 维 + bge-reranker-large 重排），经 `model-service/`（Transformers.js）暴露 HTTP，容器通过 `host.docker.internal:8090` 调用；可改回 `hashing` 零依赖占位模式
- 主 LLM：OpenAI 兼容网关（`.env` 中的 `ANTHROPIC_BASE_URL` 形如 `https://host/v1`）+ fallback 链 + 服务不可用时诚实降级
- 对话任务后台化：任务在后台线程执行并落库，切换会话/页面不中断；会话列表执行中转圈、完成后绿点；重连从事件缓冲续读
- 插件驱动：按 extension_point 自动挂载到 Runtime 生命周期（装了即生效，不依赖手写 workflow；workflow 仅作高级手动编排）
- 独立智能体：所有 Agent（含内置设计 Agent）统一走 ReAct 工作循环。设计 Agent 的 system_prompt 是正常对话 prompt（与内部生成清单的 DESIGNER_PROMPT 分离），问身份正常答、要设计时才产出制品，不再无条件强制设计。
- MCP 市场：粘贴标准 mcpServers JSON 配置（Claude Desktop/Cursor 格式）批量接入，支持 headers 鉴权；配置落项目 `mcp/<name>/mcp.json`（文件优先，MySQL 只做索引，启动重建）；自动握手发现工具并注册为 Agent 可调用的 `mcp_<server>_<tool>` 工具；卡片展示功能介绍、分类与工具清单，支持刷新/启停/删除

## 启动

```bash
# 一条命令启动全栈（含本地模型服务 model-service 容器，自动加载 D:/EmbedingModel 的 bge 模型）
docker compose up -d --build
# 控制台: http://localhost:8900/console   API 文档: http://localhost:8900/docs
# 模型服务随 compose 自启并带健康检查；重启平台后向量索引自动从 DB 文档重建
# 想改回零依赖 hashing 模式：.env 里 EMBEDDING_PROVIDER=hashing / EMBEDDING_DIM=256 / RERANK_PROVIDER=hashing，并在 compose 移除 api 对 model-service 的 depends_on
# 项目目录 skills/ 与 plugins/ 直接映射到容器：用户创建/安装的 Skill 与插件就在项目里，可直接查看修改
# DSH_HOME 映射：容器内的 /dsh-home 指向本机 DSH 目录（默认 E:/DevCache/dsh，可用 .env 的 DSH_HOME 覆盖）
```

## 功能清单

| 模块 | 能力 | 主要端点 |
|---|---|---|
| Skill 市场 | 两种创作方式（对话设计子Agent / 上传 zip·md）、按 description 自动分类导航、卡片 hover 简介、点击查看/编辑包内文件、删除 Skill | `GET /skills/categories` `GET /skills/{name}/detail` `GET /skills/{name}/file` `PUT /skills/{name}/files` `DELETE /skills/{name}` |
| Skill 准入 | ZIP 安全扫描（路径穿越/符号链接/压缩炸弹/条目数/可执行文件）+ 密钥/危险命令扫描 + 规范校验，审核记录留痕 | `GET /skills/{name}/reviews` |
| 插件体系 | 插件市场（对齐 DSH 思想）：功能简介 + 运行机制说明、来源标记、市场/GitHub/本地安装、启停、所有插件可修改/删除、配置驱动执行（behavior）、设计指南 | `GET /plugins/managed` `GET /plugins/behaviors` `POST /plugins/install` `POST /plugins/install-local` `PUT /plugins/{key}` `DELETE /plugins/{key}` |
| Agent 市场 | 组装发布（依赖锁定）、动态选用所有已安装插件（含用户新装）、评测集运行、Fork 血缘、Playground 调试 | `POST /agents` `POST /agents/{id}/eval` `/fork` `/playground` |
| A2A | 内部任务由主 Agent 在智能对话中自动发起；调用链查询（按 trace 看父子任务、状态、耗时）；对外暴露地址配置（`AWS_A2A_PUBLIC_BASE_URL`）；外部 Peer 简介/自动分类/按分类展示、白名单管理与出站脱敏 | `GET /a2a/trace/{trace_id}` `GET /a2a/traces` `GET /a2a/public-url` `POST /a2a/peers` `DELETE /a2a/peers/{id}` |
| 模型网关 | 模型目录 fallback 链、预算硬限、token 计量 | 内部 |
| 计量审计 | 按主体累计 run/a2a/llm token；发布/运行/A2A/Peer 全动作审计 | `GET /usage` `GET /audit` |
| 知识库 | 上传→体裁识别（11类+自定义，规则+LLM，仅建议）→手动指定优先→分块预览→确认入库；体裁驱动分块（合同按条款、FAQ按问答对、代码按函数、自定义块长/重叠）；混合检索（向量+BM25，RRF，阈值0.5，top_k5）；完整内容查看、删除同步清向量 | `POST /rag/documents/preview` `POST /rag/documents/upload` `GET /rag/documents/{id}` `DELETE /rag/documents/{id}` `/rag/search` |
| 存储 | 文件优先：Skill 以目录（SKILL.md + references/ + scripts/）落 artifacts/skills/，插件以 plugin.json 落 artifacts/plugins/；MySQL 只存可重建索引，目录是真相源 | `GET /skills/{name}/file` `PUT /skills/{name}/files` |
| DSH 导出 | Skill 直接导出到 DSH_HOME/skills；插件导出为 DSH bundle（package.json + cordis.patch.yml + index.js + locale/ + icon） | `POST /dsh/export/skills/{name}` `POST /dsh/export/plugins/{key}` |

## 项目目录结构

```
agent-wokplat/
├── skills/       ← 所有 Skill 的目录（SKILL.md + references/ + scripts/），直接可编辑
├── plugins/      ← 所有插件的目录（plugin.json），直接可编辑
├── backend/      ← 后端源码 + 测试
├── agents/           ← 所有 Agent（agent.json）与团队（teams/），直接可编辑
├── mcp/             ← 所有 MCP Server 配置（<name>/mcp.json），直接可编辑
├── dsh-exports/  ← 点「导出 DSH」生成的插件包在此，拿去 DSH 的 Plugins → Add plugin 安装
├── docker-compose.yml
└── README.md
```

## 端口

| 服务 | 端口 |
|---|---|
| api（控制台+API） | 8900 |
| mysql（独立实例） | 23307 |
| qdrant（独立实例） | 26334 |

## 验证

```bash
# 后端（容器内）
docker exec agent-wokplat-api python tests/e2e.py          # 基础闭环
docker exec agent-wokplat-api python tests/e2e_full.py     # 全量功能
docker exec agent-wokplat-api python tests/e2e_orch.py     # 编排对话
docker exec agent-wokplat-api python tests/e2e_llm.py      # LLM/降级
docker exec agent-wokplat-api python tests/e2e_stream.py   # SSE 流式
docker exec agent-wokplat-api python tests/e2e_filefirst.py  # 文件优先存储 + DSH 导出
docker exec agent-wokplat-api python tests/e2e_auto_mount.py # 插件按扩展点自动挂载执行
docker exec agent-wokplat-api python tests/e2e_agentmarket.py # Agent 市场：落盘/推荐/团队/删改
docker exec agent-wokplat-api python tests/e2e_chatv2.py   # 智能对话v2：会话/队列/中断/持久化
docker exec agent-wokplat-api python tests/e2e_skillmarket.py  # Skill市场后端：分类/文件树/MD上传/设计子Agent
docker exec agent-wokplat-api python tests/e2e_pluginmarket.py # 插件市场后端：安装/启停/修改/卸载
docker exec agent-wokplat-api python tests/e2e_plugin_in_agent.py # 用户插件在 Agent 中组装与运行

# 前端（宿主机，需 pip install playwright && playwright install chromium）
python backend/tests/e2e_frontend.py    # 智能对话：发消息 + 回复渲染
python backend/tests/e2e_frontend2.py   # 主题切换/导航/流式/思考块
python backend/tests/e2e_frontend3.py   # Skill 市场（两种创作入口/分类/文件结构）
python backend/tests/e2e_frontend4.py   # Agent 市场（组装/评测/Fork/Playground）
python backend/tests/e2e_frontend5.py   # A2A/知识库/计量审计
python backend/tests/e2e_kb_upload.py  # 知识库上传/自动分块/完整内容详情
python backend/tests/e2e_skill_file_tree.py # Skill 子目录下文件可查看（路径修复）
python backend/tests/e2e_hybrid_ui.py  # 检索测试展示混合检索信息
python backend/tests/e2e_genre_chunking.py  # 后端：体裁识别/分块边界/手动优先/自定义参数
python backend/tests/e2e_preview_encoding.py # 分块预览中文不乱码（UTF-8/GBK）
python backend/tests/e2e_file_formats.py # docx/xlsx 等二进制文件解析预览
python backend/tests/e2e_upload_loading.py # 入库中转圈加载符
# 注意：所有 e2e 知识库测试用快照 diff 只清理自己新增的文档，不会删除用户上传的文档
# 后端：docker exec -e PYTHONPATH=/srv/app agent-wokplat-api python tests/e2e_hybrid_search.py
# 本地模型语义检索：docker exec -e PYTHONPATH=/srv/app agent-wokplat-api python tests/e2e_local_model.py
# 主Agent能力清单：docker exec -e PYTHONPATH=/srv/app agent-wokplat-api python tests/e2e_orch_capability.py
# 独立智能体ReAct循环：docker exec -e PYTHONPATH=/srv/app agent-wokplat-api python tests/e2e_agent_loop.py
# 插件+自主能力叠加：docker exec -e PYTHONPATH=/srv/app agent-wokplat-api python tests/e2e_plugin_plus_react.py
# 体裁分块：docker exec -e PYTHONPATH=/srv/app agent-wokplat-api python tests/e2e_genre_chunking.py
python backend/tests/e2e_frontend_chatv2.py  # 智能对话v2：会话侧栏/切换/持久化/中断/队列
python backend/tests/e2e_queue.py     # 待发消息队列（编辑/删除/自动消费）
python backend/tests/e2e_skillmarket_ui.py  # Skill市场：分类/hover简介/文件结构
python backend/tests/e2e_skill_design_ui.py # 智能对话中委托 Skill 设计子Agent
python backend/tests/e2e_plugin_design_ui.py # 智能对话中开发插件
python backend/tests/e2e_skill_files.py # Skill 文件查看/编辑、删除 Skill、箭头上传
python backend/tests/e2e_pluginmarket_ui.py # 插件市场：简介/启停/安装/修改/卸载
python backend/tests/e2e_delete_session.py  # 会话删除缓存一致性
python backend/tests/e2e_session_persist_task.py # 切页面不中断任务+状态图标
python backend/tests/e2e_session_status.py  # 绿点仅未读显示、回到会话后消失
python backend/tests/e2e_resume_progress.py # 切回执行中会话重建进度
python backend/tests/e2e_no_welcome_rebirth.py # 发消息后立刻切走切回欢迎面板不重现
python backend/tests/e2e_mcp_ui.py     # MCP 市场前端（JSON 配置框+卡片工具清单）
python backend/tests/e2e_mcp_json_ui.py # MCP JSON 配置粘贴接入
python backend/tests/e2e_mcp_switch.py # MCP 卡片 switch 启停（无手动表单）
python backend/tests/e2e_mcp_refresh_ux.py # MCP 刷新工具按钮转圈交互
# 后端：docker exec -e PYTHONPATH=/srv/app agent-wokplat-api python tests/e2e_mcp_market.py
# 后端：docker exec -e PYTHONPATH=/srv/app agent-wokplat-api python tests/e2e_mcp_json_import.py
docker exec -e PYTHONPATH=/srv/app agent-wokplat-api python tests/e2e_mcp_filefirst.py # MCP 文件优先落盘/重建
python backend/tests/e2e_agent_plugin_picker.py # Agent 组装动态选用已安装插件
python backend/tests/e2e_dsh_export_ui.py   # Skill/插件导出到 DSH 按钮
python backend/tests/e2e_auto_mount_ui.py  # 装插件即自动执行（浏览器端）
python backend/tests/e2e_view_persist.py   # 刷新后保留当前页面
python backend/tests/e2e_agentmarket_ui.py # Agent 市场：独立菜单/推荐/团队/卡片/删改
python backend/tests/e2e_agent_layout.py   # Agent 列表与表单对齐、成员选择带简介
python backend/tests/e2e_list_scroll.py    # Agent/团队列表固定窗口滚动
python backend/tests/e2e_team_edit_ui.py   # 已建团队编辑（增删成员/改信息）
python backend/tests/e2e_msg_deadline.py   # 提示自动消失/删除后清除、取消编辑按钮
python backend/tests/e2e_playground_keys.py # Playground 回车发送/Shift+Enter 换行
python backend/tests/e2e_playground_spin.py # Playground 测试 Agent 时显示思考转圈
# 后端：docker exec -e PYTHONPATH=/srv/app agent-wokplat-api python tests/e2e_designer_react.py
# （设计 Agent 走普通 ReAct，问身份不再强制设计制品）
python backend/tests/e2e_playground_persist.py # Playground 对话按 Agent 保留（切页面不丢）
python backend/tests/e2e_playground_height.py # Playground 聊天框占满页面高度
python backend/tests/e2e_a2a_panel.py  # A2A 页面：调用链查询、Peer 说明
python backend/tests/e2e_peer_category.py # 外部 Peer 简介/分类/分组展示/删除
python backend/tests/e2e_a2a_public.py # A2A 对外暴露地址展示
python backend/tests/e2e_card_placement.py # Agent 卡片内覆盖查看 Card、Agent URL 规则（内部不依赖 URL）
python backend/tests/e2e_card_overlay.py # Agent Card 覆盖层与信息卡同尺寸、点×退出
# 后端：docker exec agent-wokplat-api python tests/e2e_a2a_trace.py
# 后端：docker exec agent-wokplat-api python tests/e2e_team_edit.py
```

每个功能点均有真实浏览器（Playwright）端到端测试；后端网关 503 时会快速熔断并诚实降级，测试对真实模型/降级两种形态都能通过。

## LLM 说明

主模型经 OpenAI 兼容 `/v1/chat/completions` 调用（模型目录 MiniMax-M2.5/M2.7/M3）。网关 503 或服务级故障时快速熔断并给诚实降级提示；单模型失效时按目录 fallback 链切换。切换本地 bge 模型：`.env` 中 `EMBEDDING_PROVIDER=tei`、`EMBEDDING_API_BASE`、`RERANK_PROVIDER=tei`、`RERANK_API_BASE`。

智能对话具备主 Agent + 两个内置设计子 Agent（`skill-designer` / `plugin-designer`）：识别「设计 Skill / 开发插件」意图后经 A2A 委托对应子 Agent 产出并发布；处理中可继续提问，消息进入前端待发队列（可编辑/删除），当前任务完成后自动顺序发送。

设计文档见 `E:\Codexworkspace\agent-work-system\架构设计方案.md`。
