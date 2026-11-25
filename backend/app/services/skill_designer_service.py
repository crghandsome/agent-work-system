"""Skill 设计子 Agent（创作模式一）。

定位：平台内置的专业子 Agent。主 Agent 在智能对话中识别出「设计 Skill」的意图后，
通过 A2A 内部任务调用本服务；本服务复用 skill_dialog_service 的设计流程
（澄清 → 草稿 → 规范校验 → 发布），产出可用的 Skill 制品。

返回结构与 A2A Task 结果一致（文本），额外带结构化字段供上层展示。
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from ..models import Agent, Skill
from . import skill_dialog_service, skill_pipeline

DESIGNER_KEY = "skill-designer"

# 对话用 system prompt（与内部生成草稿用的 DESIGNER_PROMPT 分离）
AGENT_SYSTEM_PROMPT = """你是 Skill 设计专家，能与用户正常对话，也能自主使用工具。
你的职责：帮用户理解、设计和开发符合规范的 Skill。
- 用户问身份、能力、闲聊时，正常用中文回答，简洁友好。
- 用户要设计 Skill 时，先说明会怎么设计，再确认关键需求。
- 回答用简洁中文，不用 Markdown 符号和 emoji。
不要输出内部设计规则条文给用户。"""


DESIGNER_PROMPT = """你是 Skill 设计专家，专长是设计符合 Anthropic Agent Skills 规范的 Skill。

设计要点：
- SKILL.md 必须有 YAML frontmatter，含 name（小写字母/数字/连字符）与 description（用途与触发时机）
- 正文是模型可执行的步骤指令，简洁明确，必要时引用 references/ 下的资料
- name 用小写英文连字符，含义清晰（如 faq-helper、code-reviewer）

输出要求：简洁自然的中文，不使用 Markdown 符号和 emoji。"""


def ensure_designer(db: Session) -> Agent:
    """确保内置的 Skill 设计子 Agent 存在（幂等）。"""
    a = (db.query(Agent).filter(Agent.name == DESIGNER_KEY)
         .order_by(Agent.version.desc()).first())
    if a:
        if a.system_prompt != AGENT_SYSTEM_PROMPT:
            a.system_prompt = AGENT_SYSTEM_PROMPT
            db.flush()
        return a
    a = Agent(
        id="agent_skill_designer",
        name=DESIGNER_KEY,
        version=1,
        description="Skill 设计专家：根据需求设计符合 Anthropic 规范的 Skill 包",
        system_prompt=AGENT_SYSTEM_PROMPT,
        model="",
        skill_keys=[],
        plugin_keys={},
        plugin_config={},
        workflow={"steps": ["llm"]},
        status="published",
    )
    db.add(a)
    db.flush()
    return a


def design(db: Session, requirement: str) -> dict:
    """设计一个 Skill 并发布。

    流程：新建设计会话 → 首轮生成草稿 → 规范校验 → 发布到市场。
    若校验失败，返回失败原因（主 Agent 会如实告知用户）。
    """
    ensure_designer(db)
    dialog = skill_dialog_service.start_dialog(db)
    dialog = skill_dialog_service.reply(db, dialog, requirement)
    if dialog.state != "draft" or not dialog.draft_name:
        return {
            "ok": False,
            "reply": f"需求还不足以生成草稿，请补充说明。当前状态：{dialog.state}。",
            "draft_name": dialog.draft_name,
        }
    try:
        skill, warnings = skill_pipeline.publish_skill_manual(
            db, name=dialog.draft_name, description=dialog.draft_description,
            instructions=dialog.draft_instructions, source="agent_dialog",
        )
    except skill_pipeline.SkillValidationError as exc:
        return {
            "ok": False,
            "reply": f"生成的草稿未通过规范校验（{exc.rule}）：{exc.message}。{exc.suggestion}",
            "draft_name": dialog.draft_name,
        }
    db.commit()
    from .skill_catalog import split_frontmatter

    _, body = split_frontmatter(skill.instructions or "")
    return {
        "ok": True,
        "reply": (
            f"已设计并发布 Skill「{skill.name}」。\n"
            f"用途：{skill.description}\n"
            f"分类：{skill.category}\n"
            f"指令正文：{body[:300]}"
            + (f"\n（校验提示：{warnings[0].get('message')}）" if warnings else "")
        ),
        "skill_name": skill.name,
        "category": skill.category,
        "description": skill.description,
        "warnings": warnings,
    }


def list_designed(db: Session) -> list[dict]:
    """该子 Agent 设计过的 Skill（source=agent_dialog）。"""
    rows = db.query(Skill).filter(Skill.source == "agent_dialog").all()
    return [{"name": s.name, "category": s.category, "description": s.description} for s in rows]
