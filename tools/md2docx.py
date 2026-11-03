# -*- coding: utf-8 -*-
"""把 docs 下的 Markdown 说明转成排好版的 Word 文档。

为什么需要：使用者是财税同行，不是程序员。给他们一个 .md 文件多半打不开，
Word 双击就能看。

只支持本项目说明文档里实际用到的语法子集：
    标题 #/##/###、引用 >、代码块 ```、表格 |...|、有序/无序列表、
    分隔线 ---、行内 **粗体** 和 `代码`

用法：
    python tools/md2docx.py docs/使用说明-给使用者.md 使用说明-v1.7.0.docx
"""
from __future__ import annotations

import os
import re
import sys

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor, Cm

BODY_FONT_CN = "微软雅黑"
BODY_FONT_EN = "Segoe UI"
CODE_FONT = "Consolas"
ACCENT = RGBColor(0x1F, 0x4E, 0x79)     # 深蓝：与程序界面主色接近
GREY = RGBColor(0x59, 0x59, 0x59)


def _set_cn_font(run, name_cn=BODY_FONT_CN, name_en=BODY_FONT_EN, size=None,
                 bold=None, color=None):
    """python-docx 不会自动设中文字体，必须显式写 w:eastAsia。"""
    run.font.name = name_en
    rpr = run._element.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = OxmlElement("w:rFonts")
        rpr.append(rfonts)
    rfonts.set(qn("w:eastAsia"), name_cn)
    rfonts.set(qn("w:ascii"), name_en)
    rfonts.set(qn("w:hAnsi"), name_en)
    if size is not None:
        run.font.size = Pt(size)
    if bold is not None:
        run.font.bold = bold
    if color is not None:
        run.font.color.rgb = color


def _shade(element, hex_fill):
    """给段落/单元格加底纹。"""
    pr = element.get_or_add_tcPr() if hasattr(element, "get_or_add_tcPr") \
        else element.get_or_add_pPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), hex_fill)
    pr.append(shd)


INLINE_RE = re.compile(r"(\*\*[^*]+\*\*|`[^`]+`)")


def _add_inline(par, text, size=10.5, color=None, base_bold=False):
    """处理 **粗体** 和 `代码` 两种行内标记，其余按普通文本。"""
    for seg in INLINE_RE.split(text):
        if not seg:
            continue
        if seg.startswith("**") and seg.endswith("**") and len(seg) > 4:
            r = par.add_run(seg[2:-2])
            _set_cn_font(r, size=size, bold=True, color=color or ACCENT)
        elif seg.startswith("`") and seg.endswith("`") and len(seg) > 2:
            r = par.add_run(seg[1:-1])
            _set_cn_font(r, name_cn=CODE_FONT, name_en=CODE_FONT,
                         size=size - 0.5, color=GREY)
            _shade(par._p, "F2F2F2")
        else:
            r = par.add_run(seg)
            _set_cn_font(r, size=size, bold=base_bold, color=color)


def _is_table_sep(line: str) -> bool:
    s = line.strip()
    return bool(s.startswith("|") and re.fullmatch(r"\|[\s:\-|]+\|", s))


def _split_row(line: str):
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def convert(md_path: str, out_path: str) -> dict:
    lines = open(md_path, encoding="utf-8").read().replace("\r\n", "\n").split("\n")

    doc = Document()
    # 页边距收窄一点，表格更好放
    for s in doc.sections:
        s.top_margin = Cm(2.0)
        s.bottom_margin = Cm(2.0)
        s.left_margin = Cm(2.2)
        s.right_margin = Cm(2.2)

    # 正文默认样式
    normal = doc.styles["Normal"]
    normal.font.name = BODY_FONT_EN
    normal.font.size = Pt(10.5)
    normal.element.rPr.rFonts.set(qn("w:eastAsia"), BODY_FONT_CN)

    stats = {"h": 0, "tables": 0, "code": 0, "para": 0}
    i = 0
    n = len(lines)
    first_h1_done = False
    # 有序列表每组从 1 重新编号（Word 的 List Number 是全局连续，会串号）
    # order_indent 记录当前有序序列的缩进层级，用来判断「同组」还是「新组」
    order_no = 0
    order_indent = -1

    while i < n:
        raw = lines[i]
        line = raw.rstrip()
        stripped = line.strip()

        if not stripped:
            i += 1
            continue

        # 代码块
        if stripped.startswith("```"):
            i += 1
            buf = []
            while i < n and not lines[i].strip().startswith("```"):
                buf.append(lines[i].rstrip())
                i += 1
            i += 1
            p = doc.add_paragraph()
            p.paragraph_format.left_indent = Cm(0.5)
            p.paragraph_format.space_before = Pt(4)
            p.paragraph_format.space_after = Pt(8)
            for k, b in enumerate(buf):
                r = p.add_run(b)
                _set_cn_font(r, name_cn=CODE_FONT, name_en=CODE_FONT, size=9.5)
                if k < len(buf) - 1:
                    r.add_break()
            _shade(p._p, "F7F7F7")
            stats["code"] += 1
            continue

        # 表格
        if stripped.startswith("|") and i + 1 < n and _is_table_sep(lines[i + 1]):
            header = _split_row(line)
            i += 2
            rows = []
            while i < n and lines[i].strip().startswith("|"):
                rows.append(_split_row(lines[i]))
                i += 1
            cols = len(header)
            table = doc.add_table(rows=1, cols=cols)
            table.style = "Table Grid"
            table.alignment = WD_TABLE_ALIGNMENT.CENTER
            hdr = table.rows[0].cells
            for c, txt in enumerate(header[:cols]):
                hdr[c].text = ""
                par = hdr[c].paragraphs[0]
                _add_inline(par, txt, size=10, color=ACCENT, base_bold=True)
                _shade(hdr[c]._tc, "DCE6F1")
            for row in rows:
                cells = table.add_row().cells
                for c in range(cols):
                    txt = row[c] if c < len(row) else ""
                    par = cells[c].paragraphs[0]
                    _add_inline(par, txt, size=10)
            doc.add_paragraph().paragraph_format.space_after = Pt(2)
            stats["tables"] += 1
            continue

        # 分隔线
        if re.fullmatch(r"-{3,}", stripped):
            p = doc.add_paragraph()
            p.paragraph_format.space_before = Pt(2)
            p.paragraph_format.space_after = Pt(2)
            pPr = p._p.get_or_add_pPr()
            bdr = OxmlElement("w:pBdr")
            bottom = OxmlElement("w:bottom")
            bottom.set(qn("w:val"), "single")
            bottom.set(qn("w:sz"), "6")
            bottom.set(qn("w:color"), "BFBFBF")
            bdr.append(bottom)
            pPr.append(bdr)
            i += 1
            continue

        # 标题
        m = re.match(r"^(#{1,4})\s+(.*)$", stripped)
        if m:
            level = len(m.group(1))
            text = m.group(2).strip()
            if level == 1 and not first_h1_done:
                # 文档大标题
                p = doc.add_paragraph()
                p.alignment = WD_ALIGN_PARAGRAPH.CENTER
                p.paragraph_format.space_after = Pt(6)
                r = p.add_run(text)
                _set_cn_font(r, size=20, bold=True, color=ACCENT)
                first_h1_done = True
            else:
                p = doc.add_paragraph()
                p.paragraph_format.space_before = Pt(12 if level <= 2 else 8)
                p.paragraph_format.space_after = Pt(4)
                sizes = {1: 16, 2: 14, 3: 12, 4: 11}
                r = p.add_run(text)
                _set_cn_font(r, size=sizes.get(level, 11), bold=True,
                             color=ACCENT if level <= 2 else GREY)
            stats["h"] += 1
            i += 1
            continue

        # 引用：只加左侧竖线 + 灰字，**不加整块底纹**。
        # 早先加了 F5F7FA 填充，渲染出来一堆灰块，视觉过重。
        if stripped.startswith(">"):
            p = doc.add_paragraph()
            p.paragraph_format.left_indent = Cm(0.6)
            p.paragraph_format.space_before = Pt(3)
            p.paragraph_format.space_after = Pt(6)
            pPr = p._p.get_or_add_pPr()
            bdr = OxmlElement("w:pBdr")
            left = OxmlElement("w:left")
            left.set(qn("w:val"), "single")
            left.set(qn("w:sz"), "18")
            left.set(qn("w:space"), "8")
            left.set(qn("w:color"), "9DB7D5")
            bdr.append(left)
            pPr.append(bdr)
            _add_inline(p, stripped.lstrip("> ").strip(), size=10, color=GREY)
            i += 1
            continue

        # 列表
        m = re.match(r"^(\s*)([-*]|\d+\.)\s+(.*)$", line)
        if m:
            indent = len(m.group(1))
            marker = m.group(2)
            text = m.group(3)
            ordered = bool(re.match(r"\d+\.", marker))
            if ordered:
                # 有序列表**不能用 List Number 样式**：Word 里它是全局连续编号，
                # 上一组结束在 3，下一组就从 4 开始（实测渲染成 4.5.6.）。
                # 改为手工写编号 + 悬挂缩进，每组从 1 重新开始。
                #
                # 「同组」按缩进层级判断：夹在中间的子项 bullet（缩进更深）
                # 不打断父级编号，否则会出现 1. 2. 1. 这种断号（实测过）。
                if order_no > 0 and indent == order_indent:
                    order_no += 1
                else:
                    order_no = 1
                    order_indent = indent
                p = doc.add_paragraph()
                p.paragraph_format.left_indent = Cm(0.95 + 0.55 * (indent // 2))
                p.paragraph_format.first_line_indent = Cm(-0.45)
                p.paragraph_format.space_after = Pt(2)
                r = p.add_run("%d. " % order_no)
                _set_cn_font(r, size=10.5, color=ACCENT, bold=True)
                _add_inline(p, text, size=10.5)
            else:
                # 无序项：缩进不大于当前有序层级时，才算真的结束了有序序列
                if order_indent >= 0 and indent <= order_indent:
                    order_no = 0
                    order_indent = -1
                p = doc.add_paragraph(style="List Bullet")
                p.paragraph_format.left_indent = Cm(0.75 + 0.55 * (indent // 2))
                p.paragraph_format.space_after = Pt(2)
                _add_inline(p, text, size=10.5)
            i += 1
            continue

        # 非列表行（段落/标题等）：有序序列结束
        order_no = 0
        order_indent = -1

        # 普通段落
        p = doc.add_paragraph()
        p.paragraph_format.space_after = Pt(5)
        p.paragraph_format.line_spacing = 1.25
        _add_inline(p, stripped, size=10.5)
        stats["para"] += 1
        i += 1

    doc.save(out_path)
    stats["bytes"] = os.path.getsize(out_path)
    return stats


if __name__ == "__main__":
    src = sys.argv[1] if len(sys.argv) > 1 else "docs/使用说明-给使用者.md"
    dst = sys.argv[2] if len(sys.argv) > 2 else "使用说明.docx"
    st = convert(src, dst)
    print("已生成: %s  (%.1f KB)" % (dst, st["bytes"] / 1024))
    print("  标题 %d · 表格 %d · 代码块 %d · 段落 %d"
          % (st["h"], st["tables"], st["code"], st["para"]))
