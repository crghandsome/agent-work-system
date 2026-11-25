"""Skill 三入口与审核流水线（设计文档 3.1/3.2/3.3）。

- template / agent_dialog：直接进入校验
- zip：安全扫描（路径穿越/符号链接/压缩炸弹/条目数/解压大小）→ 解析 SKILL.md → 同一校验
- 审核记录写 SkillReview；对话式创作走 skill_dialog_service
"""
from __future__ import annotations

import io
import json
import re
import zipfile
from pathlib import PurePosixPath

from sqlalchemy.orm import Session

from ..models import Skill, SkillReview, new_id
from .artifact_store import build_skill_dir, unpack_zip_to_skill_dir
from .audit_service import audit
from .skill_catalog import make_summary, skill_category, split_frontmatter
from .skill_service import SkillValidationError, validate_skill

RULES_VERSION = "1.1.0"
MAX_ENTRIES = 200
MAX_UNCOMPRESSED = 20 * 1024 * 1024  # 20MB
MAX_RATIO = 100  # 压缩比上限（压缩炸弹）
FORBIDDEN_PATTERNS = [
    (re.compile(r"(sk-[a-zA-Z0-9]{20,})", re.I), "hardcoded-api-key"),
    (re.compile(r"(AKIA[0-9A-Z]{16})", re.I), "hardcoded-aws-key"),
    (re.compile(r"(-----BEGIN (RSA|EC|OPENSSH) PRIVATE KEY-----)"), "private-key"),
    (re.compile(r"(?i)(rm\s+-rf\s+/|curl\s+[^|]*\|\s*(ba)?sh)"), "dangerous-command"),
]
EXECUTABLE_EXT = {".exe", ".dll", ".so", ".bat", ".cmd", ".ps1", ".sh", ".msi", ".jar"}


def _record_review(db: Session, skill_name: str, stage: str, result: str, report: dict) -> None:
    db.add(SkillReview(id=new_id("review"), skill_name=skill_name, stage=stage,
                       result=result, report={"rules_version": RULES_VERSION, **report}))


def scan_zip(data: bytes) -> dict:
    """ZIP 安全扫描。硬性失败抛 SkillValidationError；返回报告。"""
    report: dict = {"entries": 0, "skipped": []}
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise SkillValidationError("zip-invalid", f"不是有效的 ZIP 文件: {exc}", "上传标准 .zip 压缩包")

    names = zf.namelist()
    if len(names) > MAX_ENTRIES:
        raise SkillValidationError("zip-entries", f"条目数 {len(names)} 超过上限 {MAX_ENTRIES}", "精简包内容")
    report["entries"] = len(names)

    total = 0
    for info in zf.infolist():
        name = info.filename
        p = PurePosixPath(name)
        # 路径穿越
        if name.startswith("/") or ".." in p.parts or p.drive:
            raise SkillValidationError("zip-traversal", f"条目 '{name}' 存在路径穿越", "包内路径必须相对且不含 ..")
        # 绝对符号链接/特殊文件
        if (info.external_attr >> 16) & 0o170000 == 0o120000:
            raise SkillValidationError("zip-symlink", f"条目 '{name}' 是符号链接", "移除符号链接")
        # 可执行文件
        ext = p.suffix.lower()
        if ext in EXECUTABLE_EXT:
            raise SkillValidationError("zip-executable", f"条目 '{name}' 是可执行文件 ({ext})", "Skill 包只允许文本资源")
        # 大小/压缩比
        if info.compress_size > 0 and info.file_size / info.compress_size > MAX_RATIO:
            raise SkillValidationError("zip-bomb", f"条目 '{name}' 压缩比异常 ({info.file_size}/{info.compress_size})", "重新打包")
        total += info.file_size
        if total > MAX_UNCOMPRESSED:
            raise SkillValidationError("zip-size", f"解压总量超过 {MAX_UNCOMPRESSED // 1024 // 1024}MB", "精简资源")

    report["uncompressed_bytes"] = total
    return report


def parse_skill_md(data: bytes) -> tuple[str, str, str]:
    """从 ZIP 中提取并解析 SKILL.md → (name, description, body)。"""
    zf = zipfile.ZipFile(io.BytesIO(data))
    candidates = [n for n in zf.namelist() if PurePosixPath(n).name == "SKILL.md"]
    if not candidates:
        raise SkillValidationError("skill-md-missing", "包内未找到 SKILL.md", "将 SKILL.md 放在包根目录")
    path = sorted(candidates, key=lambda n: n.count("/"))[0]  # 取最浅的
    raw = zf.read(path).decode("utf-8", errors="replace")
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", raw, re.S)
    if not m:
        raise SkillValidationError("frontmatter-missing", "SKILL.md 缺少 YAML frontmatter", "添加 --- 包裹的 name/description")
    fm_text, body = m.group(1), m.group(2)
    name_m = re.search(r"^name:\s*(.+)$", fm_text, re.M)
    desc_m = re.search(r"^description:\s*(.+)$", fm_text, re.M)
    name = name_m.group(1).strip().strip("\"'") if name_m else ""
    desc = desc_m.group(1).strip().strip("\"'") if desc_m else ""
    return name, desc, body.strip()


def content_scan(text: str) -> list[dict]:
    """密钥/危险命令扫描，返回命中列表。"""
    hits = []
    for pat, label in FORBIDDEN_PATTERNS:
        m = pat.search(text)
        if m:
            line = text[: m.start()].count("\n") + 1
            hits.append({"rule": label, "line": line})
    return hits


def _read_skill_md_raw(data: bytes) -> str:
    """从 ZIP 中读取 SKILL.md 原文（含完整 frontmatter）。"""
    import io as _io
    import zipfile as _zf
    from pathlib import PurePosixPath as _P

    try:
        zf = _zf.ZipFile(_io.BytesIO(data))
        cands = [n for n in zf.namelist() if _P(n).name == "SKILL.md"]
        if not cands:
            return ""
        path = sorted(cands, key=lambda n: n.count("/"))[0]
        return zf.read(path).decode("utf-8", errors="replace")
    except Exception:
        return ""


def publish_skill_zip(db: Session, data: bytes) -> tuple[Skill, list[dict]]:
    """ZIP 入口：安全扫描 → 解析 → 内容扫描 → 校验 → 制品落盘 → 发布 + 审核记录。"""
    zip_report = scan_zip(data)
    name, desc, body = parse_skill_md(data)

    sec_hits = content_scan(f"{desc}\n{body}")
    if sec_hits:
        _record_review(db, name or "(unknown)", "security_scan", "failed", {"hits": sec_hits})
        raise SkillValidationError("security-scan", f"发现敏感内容: {sec_hits}", "移除密钥/危险命令后重新上传")
    _record_review(db, name, "security_scan", "passed", {"zip": zip_report})

    warnings = validate_skill(name, desc, body)
    _record_review(db, name, "spec_check", "passed" if not warnings else "warning",
                   {"warnings": warnings, "body_lines": body.count(chr(10)) + 1})

    artifact = unpack_zip_to_skill_dir(name, data)  # 解包为 Skill 目录（含 references/scripts/assets）
    # instructions 存包内 SKILL.md 原文（保留完整 frontmatter），便于详情页展示
    raw_md = _read_skill_md_raw(data)
    full_md = raw_md.strip() if raw_md else f"---\nname: {name}\ndescription: {desc}\n---\n\n{body.strip()}"
    skill, warnings2 = _upsert(db, name=name, description=desc, instructions=full_md, source="zip", artifact=artifact)
    audit(db, "skill.publish", target=f"skill:{name}",
          detail={"source": "zip", "sha256": artifact["sha256"][:16], "warnings": warnings2})
    return skill, warnings + warnings2


def publish_skill_manual(db: Session, *, name: str, description: str, instructions: str, source: str) -> tuple[Skill, list[dict]]:
    """template / agent_dialog / upload_md 入口。

    instructions 传入完整 SKILL.md 内容（含 frontmatter）或纯正文；
    统一规范化为「含 frontmatter 的完整 SKILL.md」存储，详情页可展示 frontmatter 与正文。
    """
    sec_hits = content_scan(f"{description}\n{instructions}")
    if sec_hits:
        _record_review(db, name, "security_scan", "failed", {"hits": sec_hits})
        raise SkillValidationError("security-scan", f"发现敏感内容: {sec_hits}", "移除密钥/危险命令")
    fm, body = split_frontmatter(instructions)
    if not fm:
        body = instructions.strip()
        fm = f"name: {name}\ndescription: {description}"
    warnings = validate_skill(name, description, body)
    _record_review(db, name, "spec_check", "passed" if not warnings else "warning", {"warnings": warnings})
    full_md = f"---\n{fm.strip()}\n---\n\n{body.strip()}"
    artifact = build_skill_dir(name, description, full_md)
    skill, warnings2 = _upsert(db, name=name, description=description, instructions=full_md,
                               source=source, artifact=artifact)
    audit(db, "skill.publish", target=f"skill:{name}",
          detail={"source": source, "sha256": artifact["sha256"][:16], "warnings": warnings2})
    return skill, warnings + warnings2


def _upsert(db: Session, *, name: str, description: str, instructions: str, source: str,
            artifact: dict) -> tuple[Skill, list[dict]]:
    category = skill_category(name, description, instructions)
    summary = make_summary(description, instructions)
    existing = db.query(Skill).filter(Skill.name == name).first()
    if existing:
        existing.description = description
        existing.instructions = instructions
        existing.source = source
        existing.status = "published"
        existing.artifact_path = artifact["path"]
        existing.artifact_sha256 = artifact["sha256"]
        existing.files = artifact["files"]
        existing.category = category
        existing.summary = summary
        return existing, []
    skill = Skill(
        id=new_id("skill"), name=name, description=description, instructions=instructions,
        status="published", source=source,
        artifact_path=artifact["path"], artifact_sha256=artifact["sha256"], files=artifact["files"],
        category=category, summary=summary,
    )
    db.add(skill)
    db.flush()
    return skill, []


def list_reviews(db: Session, skill_name: str) -> list[dict]:
    rows = (db.query(SkillReview).filter(SkillReview.skill_name == skill_name)
            .order_by(SkillReview.created_at.desc()).limit(20).all())
    return [{"id": r.id, "stage": r.stage, "result": r.result, "report": r.report, "created_at": str(r.created_at)} for r in rows]
