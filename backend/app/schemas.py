"""Pydantic API 模型。"""
from __future__ import annotations

from pydantic import BaseModel, Field


# ── Skill ──
class SkillCreate(BaseModel):
    name: str = Field(..., description="Skill 名称，与 Anthropic 规范一致：小写字母/数字/连字符")
    description: str = Field(..., description="用途描述（frontmatter.description）")
    instructions: str = Field(..., description="Skill 指令正文（SKILL.md body）")
    source: str = Field("template", description="agent_dialog|template|zip")


class SkillOut(BaseModel):
    id: str
    name: str
    description: str
    instructions: str
    status: str
    source: str

    model_config = {"from_attributes": True}


# ── Plugin ──
class PluginOut(BaseModel):
    id: str
    key: str
    name: str
    description: str = ""
    mechanism: str = ""
    extension_point: str
    slot_semantics: str
    config_schema: dict = {}
    config: dict = {}
    builtin: bool
    enabled: bool
    source: str = "builtin"
    source_url: str = ""
    version: str = "1.0.0"
    author: str = ""
    user_editable: bool = False
    install_path: str = ""

    from pydantic import field_validator

    @field_validator("config_schema", "config", mode="before")
    @classmethod
    def _dict(cls, v): return v if isinstance(v, dict) else {}

    model_config = {"from_attributes": True}


# ── Agent ──
class AgentCreate(BaseModel):
    name: str
    description: str = ""
    system_prompt: str = ""
    model: str = ""
    skill_keys: list[str] = Field(default_factory=list)
    plugin_keys: dict[str, str] = Field(default_factory=dict, description='槽位→插件key，如 {"memory": "memory_simple"}')
    plugin_config: dict = Field(default_factory=dict)


class AgentOut(BaseModel):
    id: str
    name: str
    version: int
    description: str
    system_prompt: str
    model: str
    skill_keys: list
    plugin_keys: dict
    plugin_config: dict
    status: str

    model_config = {"from_attributes": True}


# ── Run ──
class RunCreate(BaseModel):
    input: str
    use_rag: bool = True


class RunOut(BaseModel):
    id: str
    agent_id: str
    status: str
    input_text: str
    output_text: str
    error: str
    steps: list
    token_usage: dict

    model_config = {"from_attributes": True}


# ── RAG ──
class DocumentCreate(BaseModel):
    title: str = ""
    content: str


class SearchHit(BaseModel):
    text: str
    score: float
    document_id: str
    title: str


class SearchOut(BaseModel):
    query: str
    hits: list[SearchHit]
    reranked: bool
