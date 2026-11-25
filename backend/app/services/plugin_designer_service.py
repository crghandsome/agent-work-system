"""插件设计子 Agent（在智能对话里完成插件开发）。

定位：与 Skill 设计子 Agent 对称的内置专业子 Agent。主 Agent 在智能对话中识别出
「开发/设计插件」的意图后，经 A2A 内部任务调用本服务；本服务根据自然语言需求
生成插件清单（plugin.json）+ 可选实现代码，落盘为插件目录并注册到插件市场。

设计要点：
- 插件 = 清单 + 配置（当前为配置驱动 behavior，代码入口预留 entry 字段待沙箱就绪）
- key 必须是合法 slug；extension_point 决定装入 Agent 的哪个槽位
- 生成后立即可被 Agent 组装选用
"""
from __future__ import annotations

import json
import re

from sqlalchemy.orm import Session

from ..models import Agent, Plugin, new_id
from . import plugin_service
from .audit_service import audit

DESIGNER_KEY = "plugin-designer"

# 允许的扩展点（与 Plugin 市场一致）
EXTENSION_POINTS = {
    "MemoryProvider": "记忆：为 Agent 提供可读写的会话记忆",
    "ChunkingStrategy": "分块：决定 RAG 入库时文档如何切分",
    "IntentRecognizer": "意图：识别用户意图并影响后续流程",
    "ContextCompressor": "压缩：压缩进入上下文的检索片段",
    "WorkflowNode": "工作流：在流程图中作为节点参与编排",
}

# 配置驱动行为及其必填参数
BEHAVIORS = {
    "intent_keywords": ("rules", "意图识别：按关键词判定意图，命中 search_knowledge 类会触发检索"),
    "memory_echo": (None, "记忆：把本轮输入整理为记忆条目注入上下文"),
    "compress_truncate": ("max_chars", "压缩：按字符上限截断检索片段"),
    "inject_system": ("prompt", "注入：把一段文字追加进系统指令，直接改变模型行为"),
    "passthrough": (None, "透传：仅记录执行轨迹"),
}

# 对话用 system prompt：设计 Agent 作为独立个体在 ReAct 循环中的身份与职责。
# 与 DESIGNER_PROMPT（内部生成 JSON 清单用）分离，避免裸跑时把条文当答案输出。
AGENT_SYSTEM_PROMPT = """你是插件设计专家，能与用户正常对话，也能自主使用工具。
你的职责：帮用户理解、设计和开发平台插件。
- 用户问身份、能力、闲聊时，正常用中文回答，简洁友好。
- 用户要设计插件时，先说明会怎么设计（扩展点、行为、配置），再确认关键需求。
- 回答用简洁中文，不用 Markdown 符号和 emoji。
不要输出 JSON 模板或设计规则条文给用户，那些是内部生成清单用的。"""


DESIGNER_PROMPT = """你是插件设计专家。你根据用户需求设计平台插件的清单（plugin.json）。

可用扩展点：
{points}

可用配置行为（behavior）与参数：
{behaviors}

设计规则：
- key：小写英文加连字符，全局唯一，能表达插件用途（如 refund-intent）
- name：中文显示名
- description：一句话说明这个插件能做什么
- extension_point：从上面选一个
- config.behavior：从上面选一个，并按需提供参数（如 intent_keywords 需 rules 字典，compress_truncate 需 max_chars 整数，inject_system 需 prompt 字符串）
- mechanism：说明这个插件如何介入运行时并影响模型（两三句中文）

严格输出 JSON，不要 markdown 代码块，格式：
{{"key":"...","name":"...","description":"...","extension_point":"...","mechanism":"...","config":{{"behavior":"...","...":...}}}}"""


def ensure_designer(db: Session) -> Agent:
    """确保内置的插件设计子 Agent 存在（幂等）。"""
    a = (db.query(Agent).filter(Agent.name == DESIGNER_KEY)
         .order_by(Agent.version.desc()).first())
    if a:
        # 迁移：旧版本把内部 DESIGNER_PROMPT 当对话 prompt，换成对话专用 prompt
        if a.system_prompt != AGENT_SYSTEM_PROMPT:
            a.system_prompt = AGENT_SYSTEM_PROMPT
            db.flush()
        return a
    a = Agent(
        id="agent_plugin_designer",
        name=DESIGNER_KEY,
        version=1,
        description="插件设计专家：根据需求生成可安装的插件清单与配置",
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


def _extract_keywords(text: str, limit: int = 6) -> list[str]:
    """从中文需求里提取候选关键词（用于规则回退）。

    取「退款/意图/识别」这类业务词：按标点与常见虚词切分，取长度 2-6 的实词片段。
    """
    stop = {"帮我", "开发", "一个", "设计", "创建", "生成", "制作", "写个", "做个", "插件",
            "用于", "可以", "能够", "如果", "当用户", "用户", "的", "了", "和", "与", "在", "是"}
    parts = re.split(r"[，,。.；;：:、\s（）()\[\]【】]+", text or "")
    words = []
    for p in parts:
        p = p.strip()
        if not p or p in stop:
            continue
        if 2 <= len(p) <= 8:
            words.append(p)
        if len(words) >= limit:
            break
    return words


def _fallback_manifest(requirement: str) -> dict:
    """模型不可用时的规则回退：从需求里提取关键词生成清单。"""
    text = (requirement or "").strip()
    kws = _extract_keywords(text)
    # 优先用业务词构造 slug（无 ASCII 时用序号 + 语义后缀）
    ascii_words = [w.lower() for w in kws if re.match(r"^[a-zA-Z0-9]+$", w)]
    if ascii_words:
        slug = "-".join(ascii_words[:3])[:40]
    else:
        slug = "custom-intent-plugin"
    if not re.match(r"^[a-z0-9][a-z0-9-]{1,63}$", slug):
        slug = "custom-intent-plugin"
    name = kws[0][:20] if kws else "自定义插件"
    return {
        "key": slug,
        "name": name,
        "description": f"根据需求自动生成的插件：{text[:60]}",
        "extension_point": "IntentRecognizer",
        "mechanism": "识别输入中的关键词判断意图，命中检索类意图时触发知识库检索，并把结果注入上下文。",
        "config": {"behavior": "intent_keywords",
                   "rules": {"detect": kws} if kws else {"general": ["请", "帮", "如何", "什么"]}},
    }


def _llm_manifest(requirement: str) -> dict | None:
    """调用 LLM 生成清单；失败返回 None。"""
    from .model_gateway import ModelGateway

    points = "\n".join(f"- {k}：{v}" for k, v in EXTENSION_POINTS.items())
    behaviors = "\n".join(
        f"- {k}：{v}{'（必填参数 ' + p + '）' if p else ''}" for k, (p, v) in BEHAVIORS.items()
    )
    system = DESIGNER_PROMPT.replace("{points}", points).replace("{behaviors}", behaviors)
    try:
        raw, _, _used = ModelGateway(None).complete(system, [{"role": "user", "content": requirement}])
        if "[mock-llm]" in raw:
            return None
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end <= start:
            return None
        data = json.loads(raw[start : end + 1])
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _validate(manifest: dict) -> tuple[bool, str]:
    """校验清单合法性，返回 (是否通过, 错误原因)。"""
    key = str(manifest.get("key", "")).strip().lower()
    if not re.match(r"^[a-z0-9][a-z0-9-]{1,63}$", key):
        return False, "key 必须是小写字母、数字、连字符（2-64 字符）"
    ep = manifest.get("extension_point")
    if ep not in EXTENSION_POINTS:
        return False, f"extension_point 必须是 {sorted(EXTENSION_POINTS)} 之一"
    cfg = manifest.get("config") or {}
    behavior = cfg.get("behavior", "passthrough")
    if behavior not in BEHAVIORS:
        return False, f"behavior 必须是 {sorted(BEHAVIORS)} 之一"
    required, _ = BEHAVIORS[behavior]
    if required and required not in cfg:
        return False, f"behavior '{behavior}' 需要参数 '{required}'"
    return True, ""


def _write_bundle(key: str, manifest: dict) -> str:
    """把插件清单落盘为插件目录（artifacts/plugins/<key>/plugin.json）。"""
    from .artifact_store import artifacts_root

    d = artifacts_root() / "plugins" / key
    d.mkdir(parents=True, exist_ok=True)
    bundle = {
        "key": manifest["key"],
        "name": manifest.get("name", ""),
        "description": manifest.get("description", ""),
        "extension_point": manifest["extension_point"],
        "slot_semantics": manifest.get("slot_semantics", "single"),
        "mechanism": manifest.get("mechanism", ""),
        "version": manifest.get("version", "1.0.0"),
        "author": manifest.get("author", ""),
        "config": manifest.get("config", {}),
        "config_schema": manifest.get("config_schema", {}),
    }
    (d / "plugin.json").write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(d)


def design(db: Session, requirement: str) -> dict:
    """根据需求设计插件并注册到插件市场。"""
    ensure_designer(db)
    manifest = _llm_manifest(requirement) or _fallback_manifest(requirement)

    ok, err = _validate(manifest)
    if not ok:
        return {"ok": False, "reply": f"生成的插件清单未通过校验：{err}。请补充描述后重试。"}

    key = manifest["key"]
    if db.query(Plugin).filter(Plugin.key == key).first():
        return {
            "ok": False,
            "reply": f"插件 key「{key}」已存在。请换一个名称，或先在插件市场卸载它。",
        }

    install_path = _write_bundle(key, manifest)
    p = plugin_service.install_plugin(
        db,
        key=key,
        name=manifest.get("name", key),
        description=manifest.get("description", ""),
        extension_point=manifest["extension_point"],
        slot_semantics=manifest.get("slot_semantics", "single"),
        config=manifest.get("config", {}),
        config_schema=manifest.get("config_schema", {}),
        source="agent_dialog",
        source_url="",
        version_str=manifest.get("version", "1.0.0"),
        author=manifest.get("author", ""),
        user_editable=True,
        install_path=install_path,
    )
    p.mechanism = manifest.get("mechanism", "")
    db.commit()
    audit(db, "plugin.design", target=f"plugin:{key}", detail={"requirement": requirement[:120]})

    return {
        "ok": True,
        "reply": (
            f"已设计并安装插件「{p.name}」。\n"
            f"key：{key}（唯一标识，组装 Agent 时用）\n"
            f"能做什么：{p.description}\n"
            f"运行机制：{p.mechanism}\n"
            f"扩展点：{p.extension_point} · 配置：{json.dumps(p.config, ensure_ascii=False)}\n"
            f"现在可以在 Agent 市场组装时勾选它，或在插件市场继续修改它的配置。"
        ),
        "plugin_key": key,
        "name": p.name,
        "extension_point": p.extension_point,
        "config": p.config,
    }


def list_designed(db: Session) -> list[dict]:
    """该子 Agent 设计过的插件。"""
    rows = db.query(Plugin).filter(Plugin.source == "agent_dialog").all()
    return [{"key": p.key, "name": p.name, "description": p.description} for p in rows]
