#!/usr/bin/env python3
"""
文档解析层（V18 RAG 数据中台）—— 多格式解析 + 语义切分

支持格式：
  .pdf   PyMuPDF 解析：页眉/页脚剔除、字号层级识别章节、表格转 markdown、
         图片记录引用（caption 邻近文本保留）、脚注单独成块
  .md    标题层级感知（#/##/### → 章节路径）
  .txt   段落切分
  .json  原 FAQ 格式（items[].{source,title,text}）

统一产出 items：
  {source, title, text, meta:{page, section, kind}}
  kind ∈ text | table | image | footnote

语义切分 semantic_chunks()：
  - 先按章节路径分组（标题不跨块）
  - 句子边界切分（。！？!?…；）——绝不截断句中
  - 贪心打包到 ≤max_chars，块间重叠 = 上一块最后 1 句（衔接上下文）
"""
from __future__ import annotations

import re
import json
from pathlib import Path

# ----------------------------------------------------------------
# 通用：句子边界语义切分
# ----------------------------------------------------------------

_SENT_SPLIT_RE = re.compile(r"(?<=[。！？!?…；;])\s*|\n+")


def split_sentences(text: str) -> list[str]:
    """按中英句子标点 + 换行切句（保留标点）"""
    sents = [s.strip() for s in _SENT_SPLIT_RE.split(text) if s and s.strip()]
    return sents or ([text.strip()] if text.strip() else [])


def semantic_chunks(
    text: str,
    max_chars: int = 600,
    overlap_sentences: int = 1,
) -> list[str]:
    """句子边界贪心打包 + 尾句重叠 —— 绝不截断句中

    Args:
        text: 已清洗的一个 section 文本
        max_chars: 单块上限
        overlap_sentences: 跨块重叠句数（衔接上下文）
    """
    sents = split_sentences(text)
    if not sents:
        return []
    chunks: list[str] = []
    cur: list[str] = []
    cur_len = 0
    for s in sents:
        # 超长单句（如表格转的文本）硬切兜底
        pieces = [s] if len(s) <= max_chars else [
            s[i:i + max_chars] for i in range(0, len(s), max_chars)
        ]
        for piece in pieces:
            if cur_len + len(piece) > max_chars and cur:
                chunks.append("".join(cur))
                tail = cur[-overlap_sentences:] if overlap_sentences else []
                cur = list(tail)
                cur_len = sum(len(x) for x in cur)
            cur.append(piece)
            cur_len += len(piece)
    if cur:
        chunks.append("".join(cur))
    return chunks or [text.strip()]


# ----------------------------------------------------------------
# 清洗工具：页眉/页脚/页码 识别
# ----------------------------------------------------------------

_PAGE_NUM_RE = re.compile(r"^[\s\-—·]*(第?\s*\d+\s*页?|page\s*\d+[\s/of]*\d*)[\s\-—·]*$", re.IGNORECASE)


def _detect_headers_footers(pages_lines: list[list[str]]) -> tuple[set[str], set[str]]:
    """跨页重复出现的顶部/底部行 → 页眉/页脚（出现率 ≥40% 或匹配页码模式）"""
    from collections import Counter
    top_counter: Counter = Counter()
    bottom_counter: Counter = Counter()
    n = len(pages_lines)
    for lines in pages_lines:
        if not lines:
            continue
        if lines[0]:
            top_counter[lines[0].strip()] += 1
        if lines[-1]:
            bottom_counter[lines[-1].strip()] += 1
    threshold = max(2, int(n * 0.4))

    def _keep(line: str) -> bool:
        return bool(line) and (
            top_counter[line] >= threshold or bottom_counter[line] >= threshold
            or _PAGE_NUM_RE.match(line)
        )

    headers = {l for l in top_counter if _keep(l) and top_counter[l] >= threshold and l}
    footers = {l for l in bottom_counter if _keep(l) and (bottom_counter[l] >= threshold or _PAGE_NUM_RE.match(l)) and l}
    return headers, footers


# ----------------------------------------------------------------
# PDF 解析（PyMuPDF）
# ----------------------------------------------------------------

_CAPTION_RE = re.compile(r"^(图|表|Figure|Table|Fig\.?)[\s0-9\-]+")


def parse_pdf(path: Path, extract_images: bool = False) -> list[dict]:
    """PDF → items（页眉页脚剔除 / 字号层级章节 / 表格 / 图片引用 / 脚注）"""
    import fitz

    doc = fitz.open(str(path))
    stem = path.stem
    items: list[dict] = []
    pages_lines: list[list[str]] = []

    # 1) 先扫一遍每页纯文本行，识别页眉/页脚
    for page in doc:
        pages_lines.append(
            [l.strip() for l in page.get_text("text").splitlines() if l.strip()]
        )
    headers, footers = _detect_headers_footers(pages_lines)

    def _is_noise(line: str) -> bool:
        return line in headers or line in footers

    # 2) 字号统计 → 推断正文基准字号（众数），标题 = 显著大于正文或加粗
    body_size = _body_font_size(doc)

    for pno, page in enumerate(doc, start=1):
        d = page.get_text("dict")
        # 章节栈：[(层级, 标题)]
        section_stack: list[tuple[int, str]] = []

        # 表格（PyMuPDF ≥1.23）
        tables_text: set[str] = set()
        try:
            for tidx, tab in enumerate(page.find_tables()):
                rows = tab.extract()
                if not rows:
                    continue
                lines = ["| " + " | ".join(
                    (str(c) if c is not None else "").replace("\n", " ")[:30]
                    for c in row) + " |" for row in rows]
                ttext = "\n".join(lines)
                tables_text.add(ttext)
                items.append({
                    "source": f"{stem}_p{pno}_t{tidx}",
                    "title": f"{stem} 第{pno}页 表格{tidx+1}",
                    "text": ttext,
                    "meta": {"page": pno, "section": _stack_path(section_stack), "kind": "table"},
                })
        except Exception:
            pass  # find_tables 不可用/无表格

        # 图片：记录引用（可选导出文件）；caption 邻近文本在正文块里自然保留
        img_count = len(page.get_images(full=True))
        if img_count and extract_images:
            img_dir = path.parent / "assets" / stem
            img_dir.mkdir(parents=True, exist_ok=True)
            for iidx, img in enumerate(page.get_images(full=True)):
                xref = img[0]
                try:
                    pix = fitz.Pixmap(doc, xref)
                    if pix.n - pix.alpha > 3:
                        pix = fitz.Pixmap(fitz.csRGB, pix)
                    out = img_dir / f"p{pno}_img{iidx+1}.png"
                    pix.save(str(out))
                    items.append({
                        "source": f"{stem}_p{pno}_img{iidx}",
                        "title": f"{stem} 第{pno}页 图片{iidx+1}",
                        "text": f"【图片引用】{out}（页 {pno}，章节：{_stack_path(section_stack)}）；相邻 caption 见正文块",
                        "meta": {"page": pno, "section": _stack_path(section_stack), "kind": "image"},
                    })
                except Exception:
                    continue

        # 正文块（dict 模式拿 span 字号/加粗）
        blocks = d.get("blocks", [])
        page_paras: list[str] = []
        for b in blocks:
            if b.get("type") != 0:
                continue  # 图片块
            for line in b.get("lines", []):
                spans = line.get("spans", [])
                if not spans:
                    continue
                text = "".join(sp.get("text", "") for sp in spans).strip()
                if not text or _is_noise(text):
                    continue
                page_paras.append(text)
                size = max(sp.get("size", 0) for sp in spans)
                bold = any(("bold" in sp.get("font", "").lower()) for sp in spans)
                # 标题判定：字号 > 正文*1.15 或 加粗且短
                if size > body_size * 1.15 or (bold and len(text) < 40 and not text.endswith("。")):
                    level = 1 if size > body_size * 1.35 else 2
                    section_stack = [(lv, t) for lv, t in section_stack if lv < level]
                    section_stack.append((level, text))

        # 页内脚注（注释行：①②/注： 开头，通常在页尾）
        footnotes = [
            t for t in page_paras
            if t and (t.startswith("注：") or t.startswith("注:") or re.match(r"^[①②③④⑤⑥⑦⑧⑨]", t))
        ]
        body_paras = [t for t in page_paras if t not in footnotes]

        # 聚合正文 items：按章节栈分组
        sec_path = _stack_path(section_stack)
        if body_paras:
            items.append({
                "source": f"{stem}_p{pno}",
                "title": f"{stem} 第{pno}页" + (f" {sec_path}" if sec_path else ""),
                "text": "\n\n".join(body_paras),
                "meta": {"page": pno, "section": sec_path, "kind": "text"},
            })
        for i, fn in enumerate(footnotes):
            items.append({
                "source": f"{stem}_p{pno}_fn{i}",
                "title": f"{stem} 第{pno}页 注释{i+1}",
                "text": fn,
                "meta": {"page": pno, "section": sec_path, "kind": "footnote"},
            })

    doc.close()
    return items


def _body_font_size(doc) -> float:
    """正文字号 = 全文档 span 字号众数（简化：取出现次数最多的四舍五入值）"""
    from collections import Counter
    sizes: Counter = Counter()
    for page in doc:
        for b in page.get_text("dict").get("blocks", []):
            if b.get("type") != 0:
                continue
            for line in b.get("lines", []):
                for sp in line.get("spans", []):
                    if sp.get("text", "").strip():
                        sizes[round(sp.get("size", 0), 1)] += len(sp.get("text", ""))
    if not sizes:
        return 10.0
    return sizes.most_common(1)[0][0] or 10.0


def _stack_path(stack: list[tuple[int, str]]) -> str:
    return " > ".join(t for _, t in stack) if stack else ""


# ----------------------------------------------------------------
# Markdown / TXT / JSON
# ----------------------------------------------------------------

_MD_HEADING_RE = re.compile(r"^(#{1,4})\s+(.*)$")


def parse_markdown(path: Path) -> list[dict]:
    """标题层级感知：每个标题下的小节为一个 item（带章节路径）"""
    lines = path.read_text(encoding="utf-8").splitlines()
    items: list[dict] = []
    stack: list[tuple[int, str]] = []
    buf: list[str] = []

    def _flush():
        if buf and any(x.strip() for x in buf):
            items.append({
                "source": f"{path.stem}_sec{len(items)+1}",
                "title": f"{path.stem} {_stack_path(stack)}".strip(),
                "text": "\n".join(buf).strip(),
                "meta": {"page": None, "section": _stack_path(stack), "kind": "text"},
            })
        buf.clear()

    for line in lines:
        m = _MD_HEADING_RE.match(line)
        if m:
            _flush()
            level = len(m.group(1))
            stack = [(lv, t) for lv, t in stack if lv < level]
            stack.append((level, m.group(2).strip()))
        else:
            buf.append(line)
    _flush()
    return items


def parse_txt(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8")
    return [{
        "source": path.stem,
        "title": path.stem,
        "text": text,
        "meta": {"page": None, "section": "", "kind": "text"},
    }]


def parse_json_kb(path: Path) -> list[dict]:
    """原 FAQ 格式：{items: [{source,title,text}]} 或 [{...}]"""
    data = json.loads(path.read_text(encoding="utf-8"))
    items = data if isinstance(data, list) else data.get("items") or []
    out = []
    for i, it in enumerate(items):
        if not isinstance(it, dict):
            continue
        out.append({
            "source": it.get("source") or f"{path.stem}_{i}",
            "title": it.get("title", ""),
            "text": it.get("text", ""),
            "meta": {"page": None, "section": it.get("category", ""), "kind": "text"},
        })
    return out


def parse_file(path: Path, extract_images: bool = False) -> tuple[str, list[dict]]:
    """统一入口 → (category, items)；不支持的格式返回 ([], [])"""
    suffix = path.suffix.lower()
    if suffix == ".json":
        raw = json.loads(path.read_text(encoding="utf-8"))
        category = raw.get("category", path.stem) if isinstance(raw, dict) else path.stem
        items = parse_json_kb(path)
        return category, items
    if suffix == ".pdf":
        return path.stem, parse_pdf(path, extract_images=extract_images)
    if suffix in (".md", ".markdown"):
        return path.stem, parse_markdown(path)
    if suffix == ".txt":
        return path.stem, parse_txt(path)
    return path.stem, []
