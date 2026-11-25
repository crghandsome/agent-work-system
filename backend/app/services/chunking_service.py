"""文档结构解析（IR）+ 体裁驱动分块策略。

分层：
1. parse_file / detect_type —— 按扩展名把文件读成文本（保持原有职责）
2. parse_structure —— 从文本提取结构信号，产出 IR（标题/条款/页/表格/问答对等）
3. chunk_by_genre —— 按体裁把 IR 切成带 locator 的块
4. chunk_by_strategy —— 兼容旧接口（按策略名分块）

体裁：contract/legal/academic/faq/meeting/manual/report/code/article/log/custom
自定义：custom + config{chunk_size, overlap}
"""
from __future__ import annotations

import io
import json
import re
from dataclasses import dataclass, field

SUPPORTED_EXT = {".txt", ".md", ".markdown", ".pdf", ".docx", ".xlsx", ".csv", ".json", ".log"}


def detect_type(filename: str) -> str:
    ext = ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ".txt"
    return ext if ext in SUPPORTED_EXT else ".txt"


def _decode_text(data: bytes) -> str:
    """中文文件编码探测：UTF-8(BOM/无BOM) → GB18030(兼容GBK) →  latin-1 兜底。

    中文 Windows 记事本默认用 GBK 保存，直接 utf-8 解码会乱码，这里按顺序严格尝试。
    """
    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig", errors="replace")
    for enc in ("utf-8", "gb18030"):
        try:
            return data.decode(enc)  # 严格模式：失败立即换下一个编码
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def parse_file(filename: str, data: bytes) -> tuple[str, str]:
    """按扩展名解析为纯文本，返回 (文本, 类型)。"""
    ext = detect_type(filename)
    if ext in (".txt", ".md", ".markdown", ".csv", ".json", ".log"):
        return _decode_text(data), ext
    if ext == ".docx":
        from docx import Document as DocxDocument

        doc = DocxDocument(io.BytesIO(data))
        parts = []
        for p in doc.paragraphs:
            if not p.text.strip():
                continue
            style = (p.style.name or "").lower()
            prefix = "# " if "heading 1" in style else ("## " if "heading 2" in style else "")
            parts.append(prefix + p.text)
        for t in doc.tables:
            for row in t.rows:
                parts.append(" | ".join(c.text for c in row.cells))
        return "\n".join(parts), ext
    if ext == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        pages = []
        for i, page in enumerate(reader.pages):
            txt = page.extract_text() or ""
            if txt.strip():
                pages.append(f"[第{i+1}页]\n{txt}")
        return "\n\n".join(pages), ext
    if ext == ".xlsx":
        from openpyxl import load_workbook

        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        lines = []
        for ws in wb.worksheets:
            lines.append(f"[工作表: {ws.title}]")
            for row in ws.iter_rows(values_only=True):
                vals = [str(v) for v in row if v is not None]
                if vals:
                    lines.append(" | ".join(vals))
        return "\n".join(lines), ext
    return data.decode("utf-8", errors="replace"), ext


# ── 结构 IR ──

@dataclass
class Section:
    """一个语义完整的单元：标题路径 + 正文 + 定位信息。"""
    text: str
    heading_path: list[str] = field(default_factory=list)
    locator: dict = field(default_factory=dict)
    kind: str = "paragraph"       # heading/clause/qa/row/page/logline/function/article


def parse_structure(text: str, genre: str, filename: str = "") -> list[Section]:
    """按体裁把文本解析为语义单元列表。"""
    if genre == "contract":
        return _split_by_clause(text, "contract")
    if genre == "legal":
        return _split_by_clause(text, "legal")
    if genre == "faq":
        return _split_qa(text)
    if genre == "report":
        return _split_rows(text)
    if genre == "code":
        return _split_code(text)
    if genre == "log":
        return _split_log(text)
    if genre in ("manual", "academic", "meeting"):
        return _split_by_heading(text)
    if genre == "article":
        return _split_article(text)
    return _split_paragraph(text)  # custom / article 兜底的其它情况


CLAUSE_RE = re.compile(r"^\s*第\s*([零一二三四五六七八九十百千\d]+)\s*条\s*(.*)$", re.M)


def _split_by_clause(text: str, genre: str) -> list[Section]:
    matches = list(CLAUSE_RE.finditer(text))
    if not matches:
        return _split_by_heading(text)
    sections = []
    preamble = text[:matches[0].start()].strip()
    if preamble:
        sections.append(Section(preamble, locator={"part": "前言"}, kind="paragraph"))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[m.start():end].strip()
        num = m.group(1)
        # 条款内再按 "（一）/1." 分款（不跨款合并由 chunk_by_genre 保证）
        sections.append(Section(body,
            heading_path=[f"第{num}条"],
            locator={"clause": f"第{num}条"},
            kind="clause"))
    return sections


QA_RE = re.compile(r"^\s*(问\s*[:：]|Q\s*[:：]|\d+[\.、]\s*)", re.M)
ANS_RE = re.compile(r"^\s*(答\s*[:：]|A\s*[:：])", re.M)


def _split_qa(text: str) -> list[Section]:
    # 按"问"行切，每个块是一组完整问答
    parts = re.split(r"(?=^\s*(?:问\s*[:：]|Q\s*[:：]|\d+[\.、]\s*))", text, flags=re.M)
    out = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        out.append(Section(p, locator={"qa": p[:24]}, kind="qa"))
    if not out:
        return _split_paragraph(text)
    return out


def _split_rows(text: str) -> list[Section]:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    header, rows = "", []
    for ln in lines:
        if ln.startswith("[工作表:") or ln.startswith("[第"):
            header = ln
            continue
        if "|" in ln:
            if not header or "---" in ln:
                header = ln
                continue
            rows.append(Section(f"{header}\n{ln}", locator={"row": ln[:30], "table": header}, kind="row"))
        else:
            rows.append(Section(ln, locator={"line": ln[:30]}, kind="line"))
    return rows


def _split_code(text: str) -> list[Section]:
    # 按 top-level def/class/function 切；imports 作公共前缀
    lines = text.splitlines()
    imports, sections, buf, current = [], [], [], ""
    for ln in lines:
        if re.match(r"^\s*(import |from \w+ import|#include|using )", ln):
            imports.append(ln)
            continue
        if re.match(r"^(def |class |async def |function |public class |export function )", ln):
            if buf:
                sections.append(Section("\n".join(buf), locator={"symbol": current}, kind="function"))
            current = ln.strip()[:60]
            buf = [ln]
        else:
            buf.append(ln)
    if buf:
        sections.append(Section("\n".join(buf), locator={"symbol": current}, kind="function"))
    prefix = "\n".join(imports) + "\n\n" if imports else ""
    return [Section(prefix + s.text, s.heading_path, s.locator, s.kind) for s in sections]


LOG_RE = re.compile(r"(?=^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})", re.M)


def _split_log(text: str) -> list[Section]:
    parts = [p.strip() for p in LOG_RE.split(text) if p and p.strip()]
    n = len(parts)
    return [Section(p, locator={"line": i + 1, "total": n}, kind="logline") for i, p in enumerate(parts)]


def _split_by_heading(text: str) -> list[Section]:
    parts = re.split(r"(?=^#{1,6}\s+.+$)", text, flags=re.M)
    sections, stack = [], []
    for p in parts:
        if not p.strip():
            continue
        m = re.match(r"^(#{1,6})\s+(.+)$", p.strip(), re.M)
        if m:
            level = len(m.group(1))
            stack = stack[:level - 1] + [m.group(2).strip()]
        sections.append(Section(p.strip(), heading_path=list(stack),
                                locator={"heading": " > ".join(stack)}, kind="heading"))
    return sections


def _split_article(text: str) -> list[Section]:
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    return [Section(p, locator={"para": i + 1}, kind="paragraph") for i, p in enumerate(paras)]


def _split_paragraph(text: str) -> list[Section]:
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if not paras:
        paras = [text.strip()] if text.strip() else []
    return [Section(p, locator={"para": i + 1}, kind="paragraph") for i, p in enumerate(paras)]


# ── 分块（带 locator 元数据）──

def _context_prefix(doc_title: str, s: Section) -> str:
    bits = []
    if doc_title:
        bits.append(doc_title)
    if s.locator.get("clause"):
        bits.append(s.locator["clause"])
    elif s.heading_path:
        bits.append(" > ".join(s.heading_path))
    elif s.locator.get("table"):
        bits.append(s.locator["table"])
    return " | ".join(bits)


def chunk_document(text: str, genre: str, config: dict | None = None,
                   doc_title: str = "") -> list[dict]:
    """按体裁分块，返回带元数据的块列表。

    返回 [{text, embedding_text, locator, kind, chunk_index, strategy}]
    """
    config = config or {}
    size = int(config.get("chunk_size", 500))
    overlap = int(config.get("overlap", 50))
    sections = parse_structure(text, genre, doc_title)
    out = []
    for s in sections:
        prefix = _context_prefix(doc_title, s)
        t = s.text.strip()
        if not t:
            continue
        # 原子单位：条款/问答对/代码块/日志行/表格行不跨单元合并
        atomic = s.kind in ("clause", "qa", "function", "logline", "row")
        if atomic and len(t) > size:
            pieces = _fixed_window(t, size, overlap)
        elif len(t) > size:
            # 非原子单元（段落/章节）超长也按窗口切，保持上下文前缀
            pieces = _fixed_window(t, size, overlap)
        else:
            pieces = [t]
        for piece in pieces:
            out.append({
                "text": piece,
                "embedding_text": f"{prefix}\n{piece}" if prefix else piece,
                "locator": s.locator,
                "kind": s.kind,
                "chunk_index": len(out),
            })
    # 非原子单元（章节/文章/段落）按 size 合并；custom 不合并——用户要精确控制每块长度
    if genre != "custom" and not any(x["kind"] in ("clause", "qa", "function", "logline", "row") for x in out):
        merged = _merge_units(out, size)
        out = merged
    return out


def _merge_units(units: list[dict], size: int) -> list[dict]:
    """把连续小块合并到 size（只在同 kind 且非原子时）。"""
    atomic = {"clause", "qa", "function", "logline", "row"}
    merged, buf = [], None
    for u in units:
        if u["kind"] in atomic:
            if buf:
                merged.append(buf); buf = None
            merged.append(u)
            continue
        if buf is None:
            buf = dict(u)
        elif len(buf["text"]) + len(u["text"]) + 1 <= size:
            buf["text"] += "\n" + u["text"]
            buf["embedding_text"] += "\n" + u["embedding_text"]
        else:
            merged.append(buf); buf = dict(u)
    if buf:
        merged.append(buf)
    for i, m in enumerate(merged):
        m["chunk_index"] = i
    return merged


def _fixed_window(text: str, size: int, overlap: int) -> list[str]:
    if len(text) <= size:
        return [text] if text.strip() else []
    step = max(size - overlap, 1)
    return [text[i:i + size] for i in range(0, len(text), step)]


# ── 兼容旧接口 ──

STRATEGY_LABELS = {
    "markdown-heading": "按标题层级分块", "page-aware": "按页/段落分块",
    "paragraph": "按段落分块", "row-aware": "按行/表分块",
    "line-aware": "按行分块", "fixed-window": "固定窗口分块",
}


def choose_strategy(filename: str, text: str = "", genre: str = "") -> str:
    """兼容旧调用：优先按 genre 映射到策略名。"""
    if genre:
        return {"contract": "clause", "legal": "clause", "faq": "qa-pair",
                "report": "row-aware", "code": "function", "log": "line-aware",
                "custom": "fixed-window"}.get(genre, "paragraph")
    ext = detect_type(filename)
    return {".md": "markdown-heading", ".markdown": "markdown-heading",
            ".pdf": "page-aware", ".docx": "paragraph", ".xlsx": "row-aware",
            ".csv": "row-aware", ".json": "fixed-window", ".log": "line-aware",
            ".txt": "paragraph"}.get(ext, "paragraph")


def chunk_by_strategy(text: str, strategy: str, config: dict | None = None) -> tuple[list[str], str]:
    """兼容旧接口：返回 (块文本列表, 策略标签)。"""
    config = config or {}
    size = int(config.get("chunk_size", 500))
    overlap = int(config.get("overlap", 50))
    genre_map = {"clause": "contract", "qa-pair": "faq", "paragraph": "custom",
                 "row-aware": "report", "function": "code", "line-aware": "log",
                 "markdown-heading": "manual", "page-aware": "manual", "fixed-window": "custom"}
    genre = genre_map.get(strategy, "custom")
    chunks = chunk_document(text, genre, config)
    return [c["text"] for c in chunks], STRATEGY_LABELS.get(strategy, strategy)
