"""DSH 导出服务：把系统的插件与 Skill 导出为 DeepSeek Harness 可直接使用的形态。

- Skill：复制 skills/<name>/ 目录到 DSH_HOME/skills/<name>/（DSH 的 @deepseek-ai/dsh-skill-filesystem 会自动发现）
- 插件：生成 DSH bundle 目录（package.json + cordis.patch.yml + index.js + locale/*.json + icon.svg），
  可用 DSH 的「Plugins > Add plugin」选择本地目录安装，或放到 DSH_HOME/plugins/ 由 Loader 加载。

导出的 bundle 是标准 DSH Host 插件（export function apply(ctx, config)），
在 DSH 中注册一个服务/挂载点，其 role 由 we-system 插件清单的 extension_point 映射。
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from sqlalchemy.orm import Session

from ..models import Plugin, Skill
from . import artifact_store


def dsh_home() -> Path:
    """DSH_HOME 环境变量（DSH profile 启动的 shell 会设置）；未设置时给出本机默认路径。"""
    home = os.environ.get("DSH_HOME")
    if home:
        return Path(home)
    return Path(os.path.expanduser("~/.dsh"))


# ── Skill 导出 ──

def export_skill_to_dsh(name: str, target_root: Path | None = None) -> dict:
    """把 Skill 目录导出到 DSH_HOME/skills/<name>/。"""
    src = artifact_store.skill_dir(name)
    if not src.exists() or not (src / "SKILL.md").exists():
        raise FileNotFoundError(f"Skill 目录不存在或缺少 SKILL.md: {name}")
    dest_root = target_root or (dsh_home() / "skills")
    dest = dest_root / name
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dest)
    return {"type": "skill", "name": name, "path": str(dest),
            "files": [p.relative_to(dest).as_posix() for p in sorted(dest.rglob("*")) if p.is_file()]}


# ── 插件导出（DSH bundle）──

ROLE_MAP = {
    "MemoryProvider": "memory-provider",
    "ChunkingStrategy": "chunking-strategy",
    "IntentRecognizer": "intent-recognizer",
    "ContextCompressor": "context-compressor",
    "WorkflowNode": "workflow-node",
}


def _bundle_name(key: str) -> str:
    return f"@local/aws-{key}"


def build_dsh_bundle(plugin: Plugin, target_root: Path) -> dict:
    """生成符合 DSH Cordis 标准、安装后真正可运行的插件 bundle。"""
    from . import dsh_bundle

    bname = _bundle_name(plugin.key)
    d = target_root / plugin.key
    if d.exists():
        shutil.rmtree(d)
    (d / "locale").mkdir(parents=True, exist_ok=True)

    package = dsh_bundle.render_package_json(plugin)
    (d / "package.json").write_text(json.dumps(package, ensure_ascii=False, indent=2), encoding="utf-8")

    role = ROLE_MAP.get(plugin.extension_point, "workflow-node")
    # patch 行带 config：用户可在 DSH 的 cordis 配置中覆盖 behavior 参数
    cfg_in_patch = json.dumps(plugin.config or {}, ensure_ascii=False)
    patch = (f"- insert:\n    - id: aws-{plugin.key}\n      name: '{bname}'\n"
             f"      config: {cfg_in_patch}\n")
    (d / "cordis.patch.yml").write_text(patch, encoding="utf-8")

    # 真正的 DSH 插件源码：export function apply(ctx, config) + ctx.systemPrompt.section()
    index_js = dsh_bundle.render_index_js(plugin)
    (d / "index.js").write_text(index_js, encoding="utf-8")

    locale = {
        "meta": {
            "title": plugin.name or plugin.key,
            "description": plugin.description or "",
            "detail": "README.md",
        }
    }
    (d / "locale" / "en.json").write_text(json.dumps(locale, ensure_ascii=False, indent=2), encoding="utf-8")
    (d / "locale" / "zh.json").write_text(json.dumps({
        "meta": {"title": plugin.name or plugin.key, "description": plugin.description or ""}
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    behavior = (plugin.config or {}).get("behavior", "passthrough")
    dsl = dsh_bundle.BEHAVIOR_TO_DSL.get(behavior, "passthrough")
    readme = (f"# {plugin.name or plugin.key}\n\n{plugin.description or ''}\n\n"
              f"## 运行机制\n\n{plugin.mechanism or '通过 system-prompt section 注入提示词。'}\n\n"
              f"## DSH 集成\n\n- 扩展点：{plugin.extension_point}\n- behavior：{behavior}（{dsl}）\n"
              f"- 注入方式：`ctx.systemPrompt.section()`，order 9000\n\n"
              f"## 配置\n\n```json\n{json.dumps(plugin.config or {}, ensure_ascii=False, indent=2)}\n```\n"
              "安装后在 DSH 的 cordis.patch.yml 中可覆盖 config。\n")
    (d / "README.md").write_text(readme, encoding="utf-8")

    icon = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
            '<rect width="24" height="24" rx="5" fill="#5b8cff"/>'
            '<path d="M12 5v14M5 12h14" stroke="#fff" stroke-width="2"/></svg>')
    (d / "icon.svg").write_text(icon, encoding="utf-8")

    return {"key": plugin.key, "bundle": bname, "path": str(d), "role": role,
            "behavior": behavior, "dsh_extension": "ctx.systemPrompt.section"}


def export_plugin_to_dsh(db: Session, key: str, target_root: Path | None = None) -> dict:
    """导出插件为 DSH bundle（默认写到工作区的 dsh-exports/plugins/）。"""
    p = db.query(Plugin).filter(Plugin.key == key).first()
    if not p:
        raise FileNotFoundError(f"插件不存在: {key}")
    root = target_root or Path(os.environ.get("AWS_DSH_EXPORT_DIR", "dsh-exports/plugins")).resolve()
    root.mkdir(parents=True, exist_ok=True)
    # 以 plugin.json 为真相源：直接从文件读清单，不构造 ORM 对象
    manifest = artifact_store.read_plugin_manifest(key) or {}
    class _View:
        pass
    view = _View()
    for attr in ("key", "name", "description", "mechanism", "extension_point",
                 "slot_semantics", "version_str", "author", "source", "source_url",
                 "config", "config_schema", "builtin", "enabled"):
        setattr(view, attr, getattr(p, attr))
    for k, v in manifest.items():
        attr = {"version": "version_str"}.get(k, k)
        if hasattr(view, attr):
            setattr(view, attr, v)
    return build_dsh_bundle(view, root)


def export_all_to_dsh(db: Session, target_root: Path | None = None) -> dict:
    """批量导出所有插件与 Skill。"""
    root = target_root or Path(os.environ.get("AWS_DSH_EXPORT_DIR", "dsh-exports")).resolve()
    root.mkdir(parents=True, exist_ok=True)
    skills = []
    plugins = []
    for s in db.query(Skill).all():
        try:
            skills.append(export_skill_to_dsh(s.name, root / "skills"))
        except Exception as exc:
            skills.append({"name": s.name, "error": str(exc)})
    for p in db.query(Plugin).all():
        try:
            plugins.append(export_plugin_to_dsh(db, p.key, root / "plugins"))
        except Exception as exc:
            plugins.append({"key": p.key, "error": str(exc)})
    return {"root": str(root), "skills": skills, "plugins": plugins}
