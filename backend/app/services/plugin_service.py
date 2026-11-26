"""插件管理服务：对齐 DSH 插件思想。

- 市场内置插件：平台预置，可停用/启用，不可卸载
- GitHub / 本地目录安装：按 bundle 清单（package.json + manifest）安装
- 用户自建插件：可修改配置与元信息
- 所有非内置插件可自由卸载
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from sqlalchemy.orm import Session

from ..models import Plugin, new_id
from .audit_service import audit

# 内置插件的功能简介（对齐 DSH meta.description）
BUILTIN_DESCRIPTIONS = {
    "memory_simple": "跨轮持久化记忆插件：按 Agent 隔离落盘保存用户事实、偏好与对话摘要，下一轮自动按相关度与新鲜度召回。",
    "intent_keyword": "关键词意图路由插件：识别闲聊/检索/总结/任务执行等意图，闲聊自动跳过无效 RAG，检索自动扩大召回并注入策略指引。",
    "compress_truncate": "相关性上下文压缩插件：对 RAG 召回片段去重，按查询关键词优先抽取高相关句子并按总字符预算精准压缩。",
    "pii_guard": "合规审核与隐私脱敏插件：在四阶段钩子中自动对手机号、身份证号等敏感信息脱敏，并支持配置敏感词与危险工具拦截。",
}

# 运行机制：插件如何介入 Runtime、如何影响 LLM 的系统指令与生成
BUILTIN_MECHANISMS = {
    "memory_simple": (
        "双阶段生效：对话前（before_llm）从 memory/<agent>.json 按查询词相关度与时间新鲜度召回 Top-K 历史记忆注入系统提示词；"
        "对话后（after_llm）自动提取用户偏好/事实声明及本轮摘要持久化落盘，真正实现跨轮与跨重启记忆。"
    ),
    "intent_keyword": (
        "在 Runtime 的 intent 阶段执行：支持默认与自定义 rules，识别出 chitchat（短句闲聊）时自动设置 skip_rag 跳过冗余检索提速；"
        "识别出 search_knowledge 时自动扩大 RAG top_k 并向系统提示词注入对应回答策略指引。"
    ),
    "compress_truncate": (
        "在 RAG 检索之后的 compress 阶段执行：先对命中片段去重，再按用户提问词项对每个句子相关性打分，"
        "在 max_chars 与 max_total_chars 总预算内按句末边界保留最核心信息，显著降低输入 token 消耗。"
    ),
    "pii_guard": (
        "贯穿 before_llm / before_tool / after_tool / after_llm 四个生命周期钩子：自动按正则对工具结果与最终回答中的手机号、身份证号脱敏；"
        "可在插件配置或单 Agent 覆盖参数中配置 block_keywords（敏感词拦截）与 block_tools（禁止调用的工具名）。"
    ),
}


def behavior_guide() -> dict:
    """配置驱动插件的 behavior 说明（用户设计插件时的参考）。"""
    return {
        "http_webhook": "HTTP POST 调用外部微服务（配置 url / hooks / headers / timeout / fail_open），支持在 before_llm / before_tool / after_tool / after_llm 四个钩子阶段改写上下文、改写参数/结果或直接拦截。",
        "content_filter": "配置型合规审核与脱敏（配置 hooks / block_keywords / block_tools / redact_patterns / block_message），支持全 4 个生命周期钩子。",
        "intent_keywords": "扫描用户输入的关键词，输出 intent/confidence；命中 search_knowledge 时触发 RAG 检索。",
        "memory_echo": "把本轮用户输入整理为记忆条目，注入系统指令的「会话记忆」段落。",
        "compress_truncate": "按 max_chars 截断 RAG 检索片段，减少进入系统指令的 token 量。",
        "inject_system": "把 config.prompt 作为附加段落注入系统指令，直接改变 LLM 的角色/风格/约束。",
        "passthrough": "仅记录执行轨迹，不改变上下文（默认安全行为）。",
    }


def manifest_of(p: Plugin) -> dict:
    """把 DB 插件行转成 plugin.json 清单（文件真相源）。"""
    return {
        "key": p.key, "name": p.name, "description": p.description,
        "mechanism": p.mechanism, "extension_point": p.extension_point,
        "slot_semantics": p.slot_semantics, "version": p.version_str,
        "author": p.author, "source": p.source, "source_url": p.source_url,
        "config": p.config or {}, "config_schema": p.config_schema or {},
        "builtin": bool(p.builtin), "enabled": bool(p.enabled),
    }


def sync_plugin_to_disk(db: Session, p: Plugin) -> str:
    """把插件行写回 plugins/<key>/plugin.json（文件优先）。"""
    from .artifact_store import store_plugin_dir

    info = store_plugin_dir(p.key, manifest_of(p))
    p.install_path = f"plugins/{p.key}"
    return info["path"]


def sync_all_plugins_to_disk(db: Session) -> None:
    """把 DB 中所有插件同步到目录（用于迁移已有数据）。"""
    for p in db.query(Plugin).all():
        sync_plugin_to_disk(db, p)
    db.commit()


def rebuild_index_from_disk(db: Session) -> int:
    """从 plugins 目录重建 DB 索引（DB 丢失时自动恢复）。

    目录里有、DB 没有 → 插入；两边都有 → 以文件为准更新元数据。
    """
    from .artifact_store import scan_plugin_dirs

    scanned = scan_plugin_dirs()
    created = 0
    for mf in scanned:
        key = mf.get("key")
        if not key:
            continue
        p = db.query(Plugin).filter(Plugin.key == key).first()
        if p is None:
            p = Plugin(id=new_id("plugin"), key=key)
            db.add(p)
            created += 1
        p.name = mf.get("name") or key
        p.description = mf.get("description") or ""
        p.mechanism = mf.get("mechanism") or ""
        p.extension_point = mf.get("extension_point") or "WorkflowNode"
        p.slot_semantics = mf.get("slot_semantics") or "single"
        p.version_str = mf.get("version") or "1.0.0"
        p.author = mf.get("author") or ""
        p.source = mf.get("source") or "local"
        p.source_url = mf.get("source_url") or ""
        p.config = mf.get("config") or {}
        p.config_schema = mf.get("config_schema") or {}
        p.builtin = bool(mf.get("builtin", False))
        p.enabled = bool(mf.get("enabled", True))
        p.install_path = f"plugins/{key}"
    db.commit()
    return created


def ensure_descriptions(db: Session) -> None:
    """为存量内置插件回填简介（幂等）。"""
    changed = False
    for p in db.query(Plugin).all():
        if p.builtin and not p.description:
            p.description = BUILTIN_DESCRIPTIONS.get(p.key, f"平台内置插件：{p.key}")
            p.source = p.source or "builtin"
            changed = True
        if p.builtin and not p.mechanism:
            p.mechanism = BUILTIN_MECHANISMS.get(p.key, "平台内置插件，按 Runtime 标准步骤执行。")
            changed = True
        if p.config is None:
            p.config = {}
            changed = True
        if p.config_schema is None:
            p.config_schema = {}
            changed = True
    if changed:
        db.commit()


def _safe_key(key: str) -> str:
    if not re.match(r"^[a-zA-Z0-9_-]{2,64}$", key or ""):
        raise ValueError("插件 key 只能包含字母、数字、下划线、连字符（2-64 字符）")
    return key


def install_plugin(db: Session, *, key: str, name: str, description: str,
                   extension_point: str, slot_semantics: str = "single",
                   source: str = "market", source_url: str = "",
                   config: dict | None = None, config_schema: dict | None = None,
                   version_str: str = "1.0.0", author: str = "",
                   user_editable: bool = True, install_path: str = "") -> Plugin:
    """安装插件（市场/GitHub/本地）：已存在则拒绝，提示先卸载。"""
    key = _safe_key(key)
    if db.query(Plugin).filter(Plugin.key == key).first():
        raise ValueError(f"插件 '{key}' 已安装，请先卸载")
    p = Plugin(
        id=new_id("plugin"), key=key, name=name or key, description=description or "",
        extension_point=extension_point, slot_semantics=slot_semantics or "single",
        config_schema=config_schema or {}, config=config or {}, builtin=False, enabled=True,
        source=source, source_url=source_url, version_str=version_str, author=author,
        user_editable=user_editable, install_path=install_path,
    )
    db.add(p)
    db.flush()
    audit(db, "plugin.install", target=f"plugin:{key}",
          detail={"source": source, "source_url": source_url, "extension_point": extension_point})
    sync_plugin_to_disk(db, p)  # 目录是真相源
    db.commit()
    return p


def install_from_manifest(db: Session, manifest: dict, *, source: str, source_url: str = "") -> Plugin:
    """按 bundle 清单安装（GitHub/本地目录解析出的 manifest）。

    清单字段：key/name/description/extension_point/slot_semantics/config_schema/version/author
    """
    return install_plugin(
        db,
        key=manifest.get("key") or manifest.get("name", ""),
        name=manifest.get("name", ""),
        description=manifest.get("description", ""),
        extension_point=manifest.get("extension_point", "WorkflowNode"),
        slot_semantics=manifest.get("slot_semantics", "single"),
        config_schema=manifest.get("config_schema", {}),
        config=manifest.get("config", {}),
        source=source, source_url=source_url,
        version_str=manifest.get("version", "1.0.0"), author=manifest.get("author", ""),
        user_editable=True, install_path=manifest.get("install_path", ""),
    )


def parse_bundle_manifest(directory: str | Path) -> dict:
    """从本地插件目录解析 manifest（package.json 或 plugin.json）。

    对齐 DSH：package.json 的 name/description/version + dsh.bundle.patch 可选；
    我们额外要求 extension_point 等运行字段（plugin.json 或 package.json.plugin）。
    """
    d = Path(directory)
    if not d.is_dir():
        raise ValueError(f"插件目录不存在: {directory}")
    pkg_path = d / "package.json"
    plugin_path = d / "plugin.json"
    if plugin_path.exists():
        data = json.loads(plugin_path.read_text(encoding="utf-8"))
    elif pkg_path.exists():
        data = json.loads(pkg_path.read_text(encoding="utf-8"))
        plugin = data.get("plugin", {})
        data = {**data, **plugin}
    else:
        raise ValueError("插件目录缺少 package.json 或 plugin.json")
    return {
        "key": data.get("key") or str(data.get("name", "")).lstrip("@").replace("/", "-"),
        "name": data.get("title") or data.get("name", ""),
        "description": data.get("description", ""),
        "extension_point": data.get("extension_point", "WorkflowNode"),
        "slot_semantics": data.get("slot_semantics", "single"),
        "config_schema": data.get("config_schema", {}),
        "version": data.get("version", "1.0.0"),
        "author": data.get("author", ""),
        "install_path": str(d),
    }


def set_enabled(db: Session, key: str, enabled: bool) -> dict:
    p = db.query(Plugin).filter(Plugin.key == key).first()
    if not p:
        raise ValueError(f"插件 '{key}' 不存在")
    p.enabled = bool(enabled)
    audit(db, "plugin.enable" if enabled else "plugin.disable", target=f"plugin:{key}")
    sync_plugin_to_disk(db, p)
    db.commit()
    return {"key": key, "enabled": p.enabled}


def update_plugin(db: Session, key: str, *, name: str = "", description: str = "",
                  mechanism: str = "", config: dict | None = None,
                  config_schema: dict | None = None) -> dict:
    """修改插件的显示名/简介/运行机制/配置。所有插件（含内置）均可修改。"""
    p = db.query(Plugin).filter(Plugin.key == key).first()
    if not p:
        raise ValueError(f"插件 '{key}' 不存在")
    if name:
        p.name = name
    if description:
        p.description = description
    if mechanism:
        p.mechanism = mechanism
    if config is not None:
        p.config = config
    if config_schema is not None:
        p.config_schema = config_schema
    audit(db, "plugin.update", target=f"plugin:{key}")
    sync_plugin_to_disk(db, p)
    db.commit()
    return {"key": key, "name": p.name, "description": p.description,
            "mechanism": p.mechanism, "config": p.config}


def uninstall_plugin(db: Session, key: str) -> bool:
    """卸载插件（所有插件均可卸载/删除）。"""
    p = db.query(Plugin).filter(Plugin.key == key).first()
    if not p:
        return False
    from .artifact_store import delete_plugin_dir

    db.delete(p)
    audit(db, "plugin.uninstall", target=f"plugin:{key}", detail={"source": p.source})
    delete_plugin_dir(key)
    db.commit()
    return True


def to_dict(p: Plugin) -> dict:
    return {
        "id": p.id, "key": p.key, "name": p.name, "description": p.description,
        "mechanism": p.mechanism,
        "extension_point": p.extension_point, "slot_semantics": p.slot_semantics,
        "config_schema": p.config_schema or {}, "config": p.config or {},
        "builtin": bool(p.builtin), "enabled": bool(p.enabled),
        "source": p.source, "source_url": p.source_url,
        "version": p.version_str, "author": p.author,
        "user_editable": bool(p.user_editable), "install_path": p.install_path,
    }
