"""制品存储：文件优先，目录即真相源。

设计（对应需求 1）：
- Skill 落 <ARTIFACTS_ROOT>/skills/<name>/ 目录：SKILL.md + references/ + scripts/ + assets/
  （与 Anthropic Skill 规范及 DSH skills 目录形态一致，可直接被 DSH 读取）
- 插件落 <ARTIFACTS_ROOT>/plugins/<name>/ 目录：plugin.json 为唯一真相源（附 index.js / locale 供 DSH 导出）
- MCP Server 落 <ARTIFACTS_ROOT>/mcp/<name>/mcp.json：配置真相源
- 数据库只保留可重建的索引；索引丢失时从目录扫描重建

ARTIFACTS_ROOT 默认 /srv/data/artifacts，可用 AWS_ARTIFACTS_DIR 覆盖。
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import zipfile
from pathlib import Path

_SAFE_NAME = set("abcdefghijklmnopqrstuvwxyz0123456789-_")


def artifacts_root() -> Path:
    root = Path(os.environ.get("AWS_ARTIFACTS_DIR", "/srv/data/artifacts"))
    root.mkdir(parents=True, exist_ok=True)
    return root


def skills_root() -> Path:
    d = artifacts_root() / "skills"
    d.mkdir(parents=True, exist_ok=True)
    return d


def plugins_root() -> Path:
    d = artifacts_root() / "plugins"
    d.mkdir(parents=True, exist_ok=True)
    return d


def mcp_root() -> Path:
    """MCP Server 配置根目录（项目可见：./mcp/）。"""
    d = artifacts_root() / "mcp"
    d.mkdir(parents=True, exist_ok=True)
    return d


def mcp_dir(name: str) -> Path:
    return mcp_root() / name


def save_mcp_config(name: str, config: dict) -> str:
    """把 MCP Server 配置写成 <name>/mcp.json（目录是真相源）。"""
    d = mcp_dir(name)
    d.mkdir(parents=True, exist_ok=True)
    (d / "mcp.json").write_text(
        json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    return f"mcp/{name}"


def read_mcp_config(name: str) -> dict | None:
    p = mcp_dir(name) / "mcp.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def delete_mcp_config(name: str) -> bool:
    d = mcp_dir(name)
    if not d.exists():
        return False
    shutil.rmtree(d)
    return True


def scan_mcp_configs() -> list[dict]:
    """扫描 mcp/ 目录下所有 mcp.json。"""
    out = []
    for d in sorted(mcp_root().iterdir()):
        if not d.is_dir():
            continue
        cfg = read_mcp_config(d.name)
        if cfg:
            out.append(cfg)
    return out


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_of_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ── Skill：目录形态 ──

def skill_dir(name: str) -> Path:
    return skills_root() / name


def store_skill_dir(name: str, files: dict[str, bytes]) -> dict:
    """把 Skill 文件写入目录形态。files: {相对路径: 内容}。返回 {sha256, path, files}。

    目录内容是唯一真相源；同内容重复写入幂等。
    """
    d = skill_dir(name)
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True, exist_ok=True)
    for rel, content in files.items():
        target = d / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    return {"sha256": skill_digest(name), "path": f"skills/{name}", "files": list_skill_files(name)}


def skill_digest(name: str) -> str:
    """目录内容的稳定摘要（按路径排序的文件哈希串联）。"""
    d = skill_dir(name)
    if not d.exists():
        return ""
    parts = []
    for f in sorted(d.rglob("*")):
        if f.is_file():
            parts.append(f"{f.relative_to(d).as_posix()}:{sha256_of_file(f)}")
    return sha256_of("\n".join(parts).encode("utf-8"))


def list_skill_files(name: str) -> list[dict]:
    d = skill_dir(name)
    if not d.exists():
        return []
    out = []
    for f in sorted(d.rglob("*")):
        if f.is_file():
            out.append({"path": f.relative_to(d).as_posix(), "bytes": f.stat().st_size})
    return out


def read_skill_file(name: str, rel_path: str) -> str | None:
    d = skill_dir(name)
    target = (d / rel_path).resolve()
    # 防路径穿越
    if not str(target).startswith(str(d.resolve())):
        return None
    if not target.is_file():
        return None
    return target.read_text(encoding="utf-8", errors="replace")


def write_skill_file(name: str, rel_path: str, content: str) -> bool:
    d = skill_dir(name)
    target = (d / rel_path).resolve()
    if not str(target).startswith(str(d.resolve())):
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return True


def delete_skill_file(name: str, rel_path: str) -> bool:
    d = skill_dir(name)
    target = (d / rel_path).resolve()
    if not str(target).startswith(str(d.resolve())) or not target.is_file():
        return False
    target.unlink()
    return True


def delete_skill_dir(name: str) -> bool:
    """删除整个 Skill 目录（含安全校验，防止误删根目录）。"""
    if not name or name in (".", "..") or "/" in name or "\\" in name:
        return False
    d = skill_dir(name)
    if not d.exists() or not d.is_dir():
        return False
    shutil.rmtree(d)
    return True


def unpack_zip_to_skill_dir(name: str, data: bytes) -> dict:
    """把上传的 ZIP 解包成 Skill 目录。"""
    d = skill_dir(name)
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            rel = info.filename
            # 防路径穿越
            target = (d / rel).resolve()
            if not str(target).startswith(str(d.resolve())):
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(zf.read(info))
    return {"sha256": skill_digest(name), "path": f"skills/{name}", "files": list_skill_files(name)}


def build_skill_dir(name: str, description: str, instructions: str) -> dict:
    """把纯指令内容写成标准 Skill 目录（SKILL.md）。"""
    import re as _re

    if _re.match(r"^---\s*\n.*?\n---\s*\n", instructions or "", _re.S):
        md = instructions.strip()
    else:
        md = f"---\nname: {name}\ndescription: {description}\n---\n\n{(instructions or '').strip()}\n"
    return store_skill_dir(name, {"SKILL.md": md.encode("utf-8")})


def load_skill_bundle(name: str, sha256: str = "") -> dict | None:
    """运行时加载：读取 Skill 目录并分类文件。

    兼容两种目录形态：
    - 标准：skills/<name>/SKILL.md + references/ + scripts/
    - 多套一层（用户 zip 上传常见）：skills/<name>/<name>/SKILL.md + ...
    """
    d = skill_dir(name)
    if not d.exists():
        return None
    if sha256 and skill_digest(name) != sha256:
        return None  # 内容被改动：拒绝加载（可用索引里的旧哈希校验）

    # 探测真实根：若顶层无 SKILL.md，但存在唯一子目录含 SKILL.md，下移一层
    if not (d / "SKILL.md").exists():
        subdirs = [p for p in d.iterdir() if p.is_dir()]
        for sub in subdirs:
            if (sub / "SKILL.md").exists():
                d = sub
                break

    instructions, references, scripts, assets = "", {}, [], []
    for f in sorted(d.rglob("*")):
        if not f.is_file():
            continue
        rel = f.relative_to(d).as_posix()
        parts = rel.split("/")
        top = parts[0] if len(parts) > 1 else ""
        sub = "/".join(parts[1:]) if len(parts) > 1 else rel
        if rel == "SKILL.md":
            raw = f.read_text(encoding="utf-8", errors="replace")
            import re

            m = re.match(r"^---\s*\n.*?\n---\s*\n(.*)$", raw, re.S)
            instructions = (m.group(1) if m else raw).strip()
        elif top == "references" and f.suffix.lower() in (".md", ".txt"):
            references[sub] = f.read_text(encoding="utf-8", errors="replace")[:8000]
        elif top == "scripts":
            scripts.append(sub)
        elif top == "assets":
            assets.append(sub)
    return {"instructions": instructions, "references": references, "scripts": scripts, "assets": assets}


# ── 插件：目录形态（plugin.json 为真相源）──

def plugin_dir(name: str) -> Path:
    return plugins_root() / name


def store_plugin_dir(name: str, manifest: dict, extra_files: dict[str, str] | None = None) -> dict:
    """写入插件目录：plugin.json + 可选附加文件（index.js / locale/ 等）。"""
    d = plugin_dir(name)
    d.mkdir(parents=True, exist_ok=True)
    (d / "plugin.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    for rel, content in (extra_files or {}).items():
        target = d / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return {"path": f"plugins/{name}", "files": list_plugin_files(name)}


def read_plugin_manifest(name: str) -> dict | None:
    p = plugin_dir(name) / "plugin.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def write_plugin_manifest(name: str, manifest: dict) -> bool:
    d = plugin_dir(name)
    if not d.exists():
        return False
    (d / "plugin.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return True


def list_plugin_files(name: str) -> list[dict]:
    d = plugin_dir(name)
    if not d.exists():
        return []
    return [{"path": f.relative_to(d).as_posix(), "bytes": f.stat().st_size}
            for f in sorted(d.rglob("*")) if f.is_file()]


def delete_plugin_dir(name: str) -> bool:
    d = plugin_dir(name)
    if not d.exists():
        return False
    shutil.rmtree(d)
    return True


def scan_skill_dirs() -> list[dict]:
    """扫描 skills 目录，返回每个 Skill 的元信息（用于重建索引）。"""
    out = []
    for d in sorted(skills_root().iterdir()):
        if not d.is_dir():
            continue
        md = d / "SKILL.md"
        if not md.exists():
            continue
        raw = md.read_text(encoding="utf-8", errors="replace")
        import re

        m = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", raw, re.S)
        fm, body = (m.group(1), m.group(2)) if m else ("", raw)
        nm = re.search(r"^name:\s*(.+)$", fm, re.M)
        dm = re.search(r"^description:\s*(.+)$", fm, re.M)
        out.append({
            "name": (nm.group(1).strip() if nm else d.name),
            "description": (dm.group(1).strip() if dm else ""),
            "instructions": raw.strip(),
            "sha256": skill_digest(d.name),
            "files": list_skill_files(d.name),
        })
    return out


def scan_plugin_dirs() -> list[dict]:
    """扫描 plugins 目录，返回每个插件的清单（用于重建索引）。"""
    out = []
    for d in sorted(plugins_root().iterdir()):
        if not d.is_dir():
            continue
        mf = read_plugin_manifest(d.name)
        if not mf:
            continue
        out.append({**mf, "key": mf.get("key", d.name), "files": list_plugin_files(d.name)})
    return out
