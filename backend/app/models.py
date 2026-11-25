"""ORM 模型（阶段2核心实体）。

对应设计文档 6.1 核心实体的首期子集：Skill/Plugin/Agent 三类资产 + 运行记录 + 文档。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class Base(DeclarativeBase):
    pass


class Skill(Base):
    """Skill 资产：元数据在库，制品（ZIP 原包）按 SHA256 落盘不可变。"""

    __tablename__ = "skills"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)  # 与 SKILL.md frontmatter.name 一致
    description: Mapped[str] = mapped_column(Text)
    instructions: Mapped[str] = mapped_column(Text)  # SKILL.md 正文（模型可见指令，加载缓存）
    status: Mapped[str] = mapped_column(String(16), default="published")  # draft|published|disabled
    source: Mapped[str] = mapped_column(String(16), default="template")  # agent_dialog|template|zip
    artifact_path: Mapped[str] = mapped_column(String(256), default="")  # 制品相对路径 artifacts/skills/<name>/<sha>.zip
    artifact_sha256: Mapped[str] = mapped_column(String(64), default="")  # 制品哈希（审核内容=运行内容的依据）
    files: Mapped[list] = mapped_column(JSON, default=list)  # 包内文件清单 [{path, bytes}]
    category: Mapped[str] = mapped_column(String(48), default="其他")  # 分类（由 description 归类，可人工覆盖）
    summary: Mapped[str] = mapped_column(Text, default="")  # 简短介绍（hover 浮层用）
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Plugin(Base):
    """插件注册行：对齐 DSH 插件思想——来源/简介/启停/卸载/可编辑。"""

    __tablename__ = "plugins"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    key: Mapped[str] = mapped_column(String(64), unique=True)  # 唯一标识
    name: Mapped[str] = mapped_column(String(128))  # 显示名（对应 DSH meta.title）
    description: Mapped[str] = mapped_column(Text, default="")  # 功能简介（对应 DSH meta.description）
    mechanism: Mapped[str] = mapped_column(Text, default="")  # 运行机制：如何介入 Runtime 与驱动 LLM
    extension_point: Mapped[str] = mapped_column(String(64))  # MemoryProvider|ChunkingStrategy|IntentRecognizer|ContextCompressor|WorkflowNode
    slot_semantics: Mapped[str] = mapped_column(String(16))  # single|pipeline|graph
    config_schema: Mapped[dict] = mapped_column(JSON, default=dict)
    config: Mapped[dict] = mapped_column(JSON, default=dict)  # 当前配置（用户可编辑）
    builtin: Mapped[bool] = mapped_column(Boolean, default=False)  # 平台内置（不可卸载）
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)  # 启用/停用
    source: Mapped[str] = mapped_column(String(16), default="builtin")  # builtin|market|github|local
    source_url: Mapped[str] = mapped_column(String(256), default="")  # 来源地址（GitHub/市场）
    version_str: Mapped[str] = mapped_column(String(32), default="1.0.0")
    author: Mapped[str] = mapped_column(String(64), default="")
    user_editable: Mapped[bool] = mapped_column(Boolean, default=False)  # 用户自建/可修改
    install_path: Mapped[str] = mapped_column(String(256), default="")  # 本地安装路径
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Agent(Base):
    """Agent 定义（组装产物）。依赖以固定 key 列表锁定（依赖锁定校验的对象）。"""

    __tablename__ = "agents"
    __table_args__ = (UniqueConstraint("name", "version", name="uq_agent_name_version"),)

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer, default=1)
    description: Mapped[str] = mapped_column(Text, default="")
    system_prompt: Mapped[str] = mapped_column(Text, default="")
    model: Mapped[str] = mapped_column(String(64), default="")
    skill_keys: Mapped[list] = mapped_column(JSON, default=list)  # 依赖锁定的 Skill key 列表
    plugin_keys: Mapped[dict] = mapped_column(JSON, default=dict)  # {"memory": "memory_simple", ...} 槽位→插件
    plugin_config: Mapped[dict] = mapped_column(JSON, default=dict)  # 插件配置
    workflow: Mapped[dict] = mapped_column(JSON, default=dict)  # WorkflowNode DSL（图引用）
    eval_set: Mapped[list] = mapped_column(JSON, default=list)  # 评测用例 [{input, contains_any}]
    forked_from: Mapped[str] = mapped_column(String(80), default="")  # 源 AgentVersion (name@version)
    is_orchestrator: Mapped[bool] = mapped_column(Boolean, default=False)  # True=主 Agent（编排调度）
    sub_agents: Mapped[list] = mapped_column(JSON, default=list)  # 主 Agent 的子 Agent 名单
    status: Mapped[str] = mapped_column(String(16), default="published")  # draft|published|disabled
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AgentRun(Base):
    """一次 Agent 运行。"""

    __tablename__ = "agent_runs"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    agent_id: Mapped[str] = mapped_column(String(40), index=True)
    agent_version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="running")  # running|succeeded|failed
    input_text: Mapped[str] = mapped_column(Text)
    output_text: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str] = mapped_column(Text, default="")
    steps: Mapped[list] = mapped_column(JSON, default=list)  # 步骤轨迹（trace 的首期形态）
    token_usage: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Document(Base):
    """RAG 知识文档（租户=单机版首期省略，后续加 project_id）。"""

    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    title: Mapped[str] = mapped_column(String(256), default="")
    content: Mapped[str] = mapped_column(Text)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    source_filename: Mapped[str] = mapped_column(String(256), default="")  # 上传时的原始文件名
    file_type: Mapped[str] = mapped_column(String(32), default=".txt")  # 文件类型
    chunk_strategy: Mapped[str] = mapped_column(String(48), default="paragraph")  # 使用的分块策略
    genre: Mapped[str] = mapped_column(String(32), default="custom")  # 内容体裁（识别或用户指定）
    genre_confidence: Mapped[float] = mapped_column(default=0.0)  # 体裁识别置信度
    detect_reason: Mapped[str] = mapped_column(Text, default="")  # 识别依据
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ── 全量扩展实体 ──


class SkillReview(Base):
    """Skill 审核记录（3.2 必审流水线的各阶段产物）。"""

    __tablename__ = "skill_reviews"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    skill_name: Mapped[str] = mapped_column(String(64), index=True)
    stage: Mapped[str] = mapped_column(String(32))  # spec_check|security_scan|sandbox_test|semantic|manual
    result: Mapped[str] = mapped_column(String(16))  # passed|failed|warning
    report: Mapped[dict] = mapped_column(JSON, default=dict)  # 规则版本/行号/失败详情/修复建议
    reviewer: Mapped[str] = mapped_column(String(64), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class EvalRun(Base):
    """Agent 仿真评测运行（4.5）。"""

    __tablename__ = "eval_runs"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    agent_id: Mapped[str] = mapped_column(String(40), index=True)
    agent_version: Mapped[int] = mapped_column(Integer)
    total: Mapped[int] = mapped_column(Integer, default=0)
    passed: Mapped[int] = mapped_column(Integer, default=0)
    results: Mapped[list] = mapped_column(JSON, default=list)  # 逐条 {input, expected, output, pass}
    status: Mapped[str] = mapped_column(String(16), default="done")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class A2APeer(Base):
    """外部 A2A 对等端（5.3/5.4）。"""

    __tablename__ = "a2a_peers"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")  # 这个外部 Agent 能干什么
    category: Mapped[str] = mapped_column(String(48), default="其他")  # 按描述自动分类
    base_url: Mapped[str] = mapped_column(String(256))
    auth_type: Mapped[str] = mapped_column(String(16), default="bearer")  # bearer|none
    auth_token: Mapped[str] = mapped_column(String(256), default="")
    direction: Mapped[str] = mapped_column(String(16), default="outbound")  # outbound|inbound
    data_policy: Mapped[dict] = mapped_column(JSON, default=dict)  # 出站脱敏策略 {strip_keys: [...]}
    rate_limit_rpm: Mapped[int] = mapped_column(Integer, default=60)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class A2ATask(Base):
    """A2A 任务（内部与外部统一，5.2/5.3）。"""

    __tablename__ = "a2a_tasks"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)  # 幂等键
    direction: Mapped[str] = mapped_column(String(16))  # internal|inbound|outbound
    agent_name: Mapped[str] = mapped_column(String(64), default="")  # 内部目标 Agent
    peer_name: Mapped[str] = mapped_column(String(64), default="")  # 外部 Peer
    parent_task_id: Mapped[str] = mapped_column(String(40), default="")  # 委托链（深度/循环检测）
    depth: Mapped[int] = mapped_column(Integer, default=0)
    trace_id: Mapped[str] = mapped_column(String(64), default="")
    root_task_id: Mapped[str] = mapped_column(String(40), default="")
    state: Mapped[str] = mapped_column(String(16), default="submitted")  # submitted|working|completed|failed|canceled
    message: Mapped[str] = mapped_column(Text, default="")  # 输入
    result: Mapped[str] = mapped_column(Text, default="")  # 输出/Artifact 引用
    error: Mapped[str] = mapped_column(Text, default="")
    run_id: Mapped[str] = mapped_column(String(40), default="")  # 关联 AgentRun
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class MCPServer(Base):
    """用户接入的 MCP Server（配置 + 发现到的工具缓存）。"""

    __tablename__ = "mcp_servers"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)  # 用户给的名字
    description: Mapped[str] = mapped_column(Text, default="")  # 功能介绍
    category: Mapped[str] = mapped_column(String(48), default="其他")  # 分类
    transport: Mapped[str] = mapped_column(String(16), default="http")  # http|sse|stdio
    endpoint: Mapped[str] = mapped_column(String(512), default="")  # HTTP/SSE 地址
    command: Mapped[str] = mapped_column(String(512), default="")  # stdio 命令
    env_json: Mapped[dict] = mapped_column(JSON, default=dict)  # stdio 环境变量
    headers_json: Mapped[dict] = mapped_column(JSON, default=dict)  # HTTP 自定义请求头（鉴权等）
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(16), default="disconnected")  # connected|disconnected|error
    server_name: Mapped[str] = mapped_column(String(128), default="")  # MCP 自报名称
    server_version: Mapped[str] = mapped_column(String(64), default="")
    protocol_version: Mapped[str] = mapped_column(String(32), default="")
    tools: Mapped[list] = mapped_column(JSON, default=list)  # 发现到的工具清单
    error: Mapped[str] = mapped_column(Text, default="")
    last_connected_at: Mapped[str] = mapped_column(String(40), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class UsageCounter(Base):
    """计量（8.2）：按主体累计调用次数与 token。"""

    __tablename__ = "usage_counters"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    subject: Mapped[str] = mapped_column(String(80), index=True)  # agent:name@ver | peer:name | llm
    kind: Mapped[str] = mapped_column(String(24))  # run|a2a_call|llm_token
    count: Mapped[int] = mapped_column(Integer, default=0)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class AuditEvent(Base):
    """审计事件（8.1）：发布/撤回/授权/出站等动作，不可篡改追加。"""

    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    action: Mapped[str] = mapped_column(String(64))  # skill.publish|agent.publish|agent.run|a2a.task|peer.create|...
    actor: Mapped[str] = mapped_column(String(64), default="system")
    target: Mapped[str] = mapped_column(String(128), default="")
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SkillDialog(Base):
    """对话式 Skill 创作（3.1 入口一）：会话状态与草稿。"""

    __tablename__ = "skill_dialogs"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    state: Mapped[str] = mapped_column(String(16), default="clarify")  # clarify|draft|review|submitted
    history: Mapped[list] = mapped_column(JSON, default=list)  # [{role, content}]
    draft_name: Mapped[str] = mapped_column(String(64), default="")
    draft_description: Mapped[str] = mapped_column(Text, default="")
    draft_instructions: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class OrchestratorSession(Base):
    """主 Agent 编排会话：多轮历史与委托记录。"""

    __tablename__ = "orchestrator_sessions"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    title: Mapped[str] = mapped_column(String(128), default="新对话")
    agent_id: Mapped[str] = mapped_column(String(40), default="")  # 对话的主 Agent（编排型 AgentVersion）
    history: Mapped[list] = mapped_column(JSON, default=list)  # [{role, content, delegations}]
    trace_id: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
