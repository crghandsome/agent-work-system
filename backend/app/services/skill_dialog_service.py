"""对话式 Skill 创作（3.1 入口一）：平台内置创作 Agent 的对话状态机。

状态: clarify → draft → review → submitted
每轮调用 LLM 推进；无 LLM 时用规则引擎推进（保证离线可用）。
"""
from __future__ import annotations

import json

from sqlalchemy.orm import Session

from ..models import SkillDialog, new_id
from ..providers.llm import get_llm_provider
from . import skill_pipeline

CLARIFY_PROMPT = """你是 Skill 创作助手。请向用户提出 2-3 个澄清问题以便设计 Skill：
1) 这个 Skill 帮助谁完成什么任务？2) 触发条件/典型输入是什么？3) 输出与约束是什么？
简要提问，不要编造 Skill 内容。"""

DRAFT_SYSTEM = """你是 Skill 设计专家，按 Anthropic Agent Skills 规范输出草稿。
严格输出 JSON（不要 markdown 代码块）：{"name": "小写连字符名", "description": "一句话用途与触发时机", "instructions": "模型可执行的步骤指令，200字以内"}"""


def start_dialog(db: Session) -> SkillDialog:
    dialog = SkillDialog(id=new_id("dlg"), state="clarify", history=[])
    system_reply = _llm_text(CLARIFY_PROMPT) or (
        "请说明：1) 这个 Skill 帮助谁完成什么任务？2) 典型输入是什么？3) 期望输出与约束？"
    )
    dialog.history = [{"role": "assistant", "content": system_reply}]
    db.add(dialog)
    db.flush()
    return dialog


def reply(db: Session, dialog: SkillDialog, user_text: str) -> SkillDialog:
    dialog.history = [*dialog.history, {"role": "user", "content": user_text}]

    if dialog.state == "clarify":
        # 澄清信息足够（>=1 轮用户输入）即生成草稿
        draft = _generate_draft(dialog.history)
        dialog.draft_name = draft["name"]
        dialog.draft_description = draft["description"]
        dialog.draft_instructions = draft["instructions"]
        dialog.state = "draft"
        dialog.history = [*dialog.history, {
            "role": "assistant",
            "content": f"草稿已生成：\n- name: {draft['name']}\n- description: {draft['description']}\n\n指令:\n{draft['instructions']}\n\n回复「确认」提交审核，或提出修改意见。",
        }]
    elif dialog.state == "draft":
        if any(kw in user_text for kw in ("确认", "提交", "ok", "OK", "好")):
            dialog.state = "submitted"
            dialog.history = [*dialog.history, {"role": "assistant", "content": "已提交审核。"}]
        else:
            draft = _generate_draft(dialog.history + [{"role": "system", "content": f"用户修改意见: {user_text}"}])
            dialog.draft_name = draft["name"]
            dialog.draft_description = draft["description"]
            dialog.draft_instructions = draft["instructions"]
            dialog.history = [*dialog.history, {
                "role": "assistant",
                "content": f"已按意见修改草稿：\n- name: {draft['name']}\n- description: {draft['description']}\n\n指令:\n{draft['instructions']}\n\n回复「确认」提交审核。",
            }]
    # submitted 状态不再推进
    db.flush()
    return dialog


def submit(db: Session, dialog: SkillDialog) -> tuple[object, list[dict]]:
    """把草稿送入统一审核流水线（作者 Agent 不能自审——此处走与 ZIP 相同的流水线）。"""
    dialog.state = "submitted"
    db.flush()
    return skill_pipeline.publish_skill_manual(
        db, name=dialog.draft_name, description=dialog.draft_description,
        instructions=dialog.draft_instructions, source="agent_dialog",
    )


def _generate_draft(history: list[dict]) -> dict:
    transcript = "\n".join(f"{h['role']}: {h['content'][:300]}" for h in history[-6:])
    text = _llm_text(f"根据以下对话生成 Skill 草稿。\n{transcript}", system=DRAFT_SYSTEM)
    if text:
        try:
            start, end = text.find("{"), text.rfind("}")
            if start >= 0 and end > start:
                draft = json.loads(text[start : end + 1])
                if {"name", "description", "instructions"} <= set(draft):
                    draft["name"] = _safe_name(draft["name"])
                    return draft
        except (json.JSONDecodeError, TypeError):
            pass
    # 规则引擎回退
    user_texts = [h["content"] for h in history if h["role"] == "user"]
    topic = (user_texts[0][:20] if user_texts else "helper").strip()
    slug = "".join(c if c.isascii() and (c.isalnum() or c == " ") else "" for c in topic.lower()).strip().replace(" ", "-") or "custom-helper"
    return {
        "name": slug[:40],
        "description": f"辅助完成: {topic}",
        "instructions": f"1) 理解用户关于「{topic}」的请求；2) 按最佳实践分步处理；3) 输出结构化结果并说明依据。",
    }


def _safe_name(raw: str) -> str:
    """把模型生成的 name 规范为合规的 slug（小写字母/数字/连字符）。"""
    import re
    import uuid

    s = str(raw or "").lower().strip()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s[:40] if s else f"skill-{uuid.uuid4().hex[:6]}"


def _llm_text(prompt: str, system: str = "你是有用的助手。") -> str:
    try:
        text, _ = get_llm_provider().complete(system, [{"role": "user", "content": prompt}])
        if "[mock-llm]" in text:
            return ""
        return text
    except Exception:
        return ""
