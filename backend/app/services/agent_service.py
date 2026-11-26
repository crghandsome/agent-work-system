"""Agent 组装服务：依赖锁定 + 组装校验矩阵（对应设计文档 4.4）。"""
from __future__ import annotations

from sqlalchemy.orm import Session

from ..models import Agent, Plugin, Skill, new_id
from ..plugins.builtin import PLUGIN_REGISTRY, WorkflowRunner

# 槽位 → 扩展点（memory/intent 单槽位互斥；compress 与 workflow 支持多实例管道叠加）
SLOTS: dict[str, str] = {
    "memory": "MemoryProvider",
    "intent": "IntentRecognizer",
    "compress": "ContextCompressor",
    "workflow": "WorkflowNode",
}
PIPELINE_SLOTS = {"compress", "workflow"}


def base_slot(slot: str) -> str:
    """支持多实例槽位名，如 'workflow:audit' / 'compress:2' → 'workflow' / 'compress'。"""
    return (slot or "").split(":", 1)[0]


class AssemblyValidationError(ValueError):
    def __init__(self, rule: str, message: str):
        self.rule = rule
        self.message = message
        super().__init__(f"[{rule}] {message}")


def validate_assembly(db: Session, *, skill_keys: list[str], plugin_keys: dict[str, str]) -> list[dict]:
    """组装校验矩阵。全部通过返回警告列表；任一失败抛 AssemblyValidationError。"""
    warnings: list[dict] = []

    # 1. 槽位合法性：单槽位互斥；compress / workflow 允许用 'workflow:<id>' 形式多实例叠加
    seen_single: set[str] = set()
    for slot, key in plugin_keys.items():
        bslot = base_slot(slot)
        if bslot not in SLOTS:
            raise AssemblyValidationError("unknown-slot", f"未知槽位 '{slot}'，合法槽位: {sorted(SLOTS)}")
        if ":" in slot and bslot not in PIPELINE_SLOTS:
            raise AssemblyValidationError(
                "single-slot-multi",
                f"槽位 '{bslot}' 为单槽位，不支持多实例挂载 '{slot}'（仅 {sorted(PIPELINE_SLOTS)} 支持叠加）",
            )
        if bslot not in PIPELINE_SLOTS:
            if bslot in seen_single:
                raise AssemblyValidationError("single-slot-conflict", f"单槽位 '{bslot}' 不可重复挂载")
            seen_single.add(bslot)
        plugin = db.query(Plugin).filter(Plugin.key == key, Plugin.enabled.is_(True)).first()
        if plugin is None:
            raise AssemblyValidationError("dependency-missing", f"槽位 '{slot}' 引用的插件 '{key}' 不存在或已禁用")
        if plugin.extension_point != SLOTS[bslot]:
            raise AssemblyValidationError(
                "slot-mismatch",
                f"插件 '{key}' 的扩展点 {plugin.extension_point} 与槽位 '{slot}'({SLOTS[bslot]}) 不匹配",
            )

    # 2. 依赖锁定：skill_keys 必须全部存在且为 published
    for key in skill_keys:
        skill = db.query(Skill).filter(Skill.name == key, Skill.status == "published").first()
        if skill is None:
            raise AssemblyValidationError("dependency-locked", f"引用的 Skill '{key}' 不存在或未发布（依赖锁定失败）")

    return warnings


def create_agent(db: Session, *, name: str, description: str, system_prompt: str, model: str,
                 skill_keys: list[str], plugin_keys: dict[str, str], plugin_config: dict,
                 workflow: dict | None = None, eval_set: list[dict] | None = None,
                 forked_from: str = "", is_orchestrator: bool = False,
                 sub_agents: list[str] | None = None) -> tuple[Agent, list[dict]]:
    warnings = validate_assembly(db, skill_keys=skill_keys, plugin_keys=plugin_keys)
    # 编排型校验：允许零子 Agent（纯通用主 Agent——自答一切，仅在匹配时委托）；子不能是编排型（一层编排，防递归）
    sub_agents = sub_agents or []
    for sub in sub_agents:
        sub_agent = (db.query(Agent).filter(Agent.name == sub, Agent.status == "published")
                     .order_by(Agent.version.desc()).first())
        if sub_agent is None:
            raise AssemblyValidationError("sub-agent-missing", f"子 Agent '{sub}' 不存在或未发布")
        if sub_agent.is_orchestrator:
            raise AssemblyValidationError("sub-agent-orchestrator", "子 Agent 不能是编排型（不支持嵌套编排）")
    # 工作流 DSL 校验（4.2：声明式，不开放任意代码）
    wf_errors = WorkflowRunner.validate(workflow or {})
    if wf_errors:
        raise AssemblyValidationError("workflow-invalid", "; ".join(wf_errors))
    # 依赖锁定快照：validate_assembly 已校验插件在库且启用；此处额外确认非内置插件有可用实现
    # 内置插件走 PLUGIN_REGISTRY；用户安装的插件按配置驱动（无代码也能以配置化能力运行）
    for key in plugin_keys.values():
        if key in PLUGIN_REGISTRY:
            continue
        p = db.query(Plugin).filter(Plugin.key == key, Plugin.enabled.is_(True)).first()
        if p is None:
            raise AssemblyValidationError("dependency-locked", f"插件 '{key}' 不存在或已禁用")
    # 评测集最低要求（4.5：首期 2 条，字段含 input）
    eval_set = eval_set or []
    for case in eval_set:
        if not isinstance(case, dict) or "input" not in case:
            raise AssemblyValidationError("eval-invalid", f"评测用例缺少 input 字段: {case}")

    latest = (
        db.query(Agent)
        .filter(Agent.name == name)
        .order_by(Agent.version.desc())
        .first()
    )
    version = (latest.version + 1) if latest else 1
    agent = Agent(
        id=new_id("agent"),  # 每个版本行独立 id，Agent 身份由 name + version 标识
        name=name,
        version=version,
        description=description,
        system_prompt=system_prompt,
        model=model,
        skill_keys=skill_keys,
        plugin_keys=plugin_keys,
        plugin_config=plugin_config,
        workflow=workflow or {},
        eval_set=eval_set,
        forked_from=forked_from,
        is_orchestrator=is_orchestrator,
        sub_agents=sub_agents,
        status="published",
    )
    db.add(agent)
    db.flush()
    return agent, warnings
