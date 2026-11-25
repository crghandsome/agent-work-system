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
    "memory_simple": "短期会话记忆插件：按用户与会话隔离保存记忆条目，供 Agent 在对话中回忆历史上下文。",
    "chunking_fixed": "固定窗口文档分块插件：将长文档切分为固定大小、带重叠的片段，供 RAG 入库使用。",
    "intent_keyword": "关键词意图识别插件：根据输入中的关键词判断用户意图并输出置信度与结构化参数。",
    "compress_truncate": "上下文压缩插件：对检索片段按字符上限截断，降低上下文长度与 token 消耗。",
}

# 运行机制：插件如何介入 Runtime、如何影响 LLM 的系统指令与生成
BUILTIN_MECHANISMS = {
    "memory_simple": (
        "在 Runtime 的 memory 步骤执行：将本轮用户输入与 RAG 命中标题整理成记忆条目，"
        "写入会话上下文。系统指令组装阶段（_build_system）把记忆作为「会话记忆」段落注入，"
        "LLM 因此能在生成回复时参考本会话的历史信息。"
    ),
    "chunking_fixed": (
        "在 RAG 文档入库阶段执行：把长文档按 chunk_size 与 overlap 切分成片段，逐片向量化后写入 Qdrant。"
        "运行时不直接驱动 LLM，而是决定检索时能召回的知识粒度，间接影响注入系统指令的参考资料质量。"
    ),
    "intent_keyword": (
        "在 Runtime 的 intent 步骤执行：扫描用户输入中的关键词，输出 intent 与 confidence。"
        "若识别出 search_knowledge 类意图，会触发/强化 RAG 检索；结果也写入执行轨迹供调试。"
    ),
    "compress_truncate": (
        "在 Runtime 的 compress 步骤执行：遍历已检索到的片段，按 max_chars 截断文本，"
        "压缩后的片段进入系统指令的知识库段落，从而减少输入 token、降低上下文超限风险。"
    ),
}


def behavior_guide() -> dict:
    """配置驱动插件的 behavior 说明（用户设计插件时的参考）。"""
    return {
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
