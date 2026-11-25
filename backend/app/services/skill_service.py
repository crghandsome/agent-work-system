"""Skill 服务：解析校验（Anthropic 兼容性硬性规则）+ 注册。

对应设计文档 3.2 检查器的首期子集：frontmatter 必需字段与格式硬性校验。
"""
from __future__ import annotations

import re

from sqlalchemy.orm import Session

from ..models import Skill, new_id

NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
MAX_DESC_LEN = 1024
MAX_BODY_LINES = 500


class SkillValidationError(ValueError):
    """带规则名与修复建议的硬性校验失败（报告能定位规则）。"""

    def __init__(self, rule: str, message: str, suggestion: str):
        self.rule = rule
        self.message = message
        self.suggestion = suggestion
        super().__init__(f"[{rule}] {message} —— 修复建议: {suggestion}")


def validate_skill(name: str, description: str, instructions: str) -> list[dict]:
    """返回警告列表；硬性失败抛 SkillValidationError。"""
    warnings: list[dict] = []

    if not name or not name.strip():
        raise SkillValidationError("name-required", "name 不能为空", "在 frontmatter 中填写 name")
    if not NAME_RE.match(name):
        raise SkillValidationError(
            "name-format",
            f"name '{name}' 不符合规范（仅小写字母、数字、连字符，不得以连字符开头/结尾）",
            "改为如 'data-analysis' 的小写连字符形式",
        )
    if len(name) > 64:
        raise SkillValidationError("name-length", f"name 超过 64 字符（当前 {len(name)}）", "缩短名称")
    if not description or not description.strip():
        raise SkillValidationError("description-required", "description 不能为空", "说明这个 Skill 做什么、何时使用")
    if len(description) > MAX_DESC_LEN:
        raise SkillValidationError(
            "description-length", f"description 超过 {MAX_DESC_LEN} 字符（当前 {len(description)}）", "压缩描述"
        )
    if not instructions or not instructions.strip():
        raise SkillValidationError("body-required", "指令正文不能为空", "补充 SKILL.md 正文")
    body_lines = instructions.count("\n") + 1
    if body_lines > MAX_BODY_LINES:
        warnings.append(
            {"rule": "body-length", "message": f"正文 {body_lines} 行超过建议值 {MAX_BODY_LINES}", "severity": "warning"}
        )
    return warnings


def register_skill(
    db: Session, *, name: str, description: str, instructions: str, source: str
) -> tuple[Skill, list[dict]]:
    warnings = validate_skill(name, description, instructions)
    existing = db.query(Skill).filter(Skill.name == name).first()
    if existing:
        # 更新（阶段2简化为同版本覆盖；v1.1 设计的版本链后续扩展）
        existing.description = description
        existing.instructions = instructions
        existing.source = source
        existing.status = "published"
        skill = existing
    else:
        skill = Skill(
            id=new_id("skill"),
            name=name,
            description=description,
            instructions=instructions,
            status="published",
            source=source,
        )
        db.add(skill)
    db.flush()
    return skill, warnings
