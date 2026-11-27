"""Builds the PDF (reportlab): landscape A4, the header row repeated on each page, page numbers.

Safety: reportlab's Paragraph reads <, > and & as markup, so every value is escaped
(xml.sax.saxutils.escape) and prints as literal text. The built-in Helvetica covers Western
text (µ, ®, accents); characters it has no glyph for (Chinese, Greek, Cyrillic, ...) are set in
a subset of Noto Sans SC (fonts/, SIL Open Font License 1.1; scripts/subset_font.py), embedding
only the characters used. Control characters are dropped; a character neither font has (seen
in PROD: ő, ű, ᵀ, ᴹ, ⁰, ∙) prints as its closest plain form (o, T, 0, ·) or "?", never as a box.
The Excel file keeps the original text.
"""
from __future__ import annotations

import io
import re
import unicodedata
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (CondPageBreak, Paragraph, SimpleDocTemplate, Spacer, Table,
                                TableStyle)

from query_layer.summary import excerpt

from .document import Column, Document

MEDIA_TYPE = "application/pdf"
FALLBACK_FONT = "NotoSansSC"
FONT_FILE = Path(__file__).with_name("fonts") / "NotoSansSC-Regular-subset.ttf"
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")    # C0 (not tab/newline) and C1
_NEAREST = {"\u2219": "\u00b7", "\u02da": "\u00b0", "\u2010": "-", "\u2033": '"'}   # ∙ ˚ ‐ ″
PAGE = landscape(A4)
MARGIN = 12 * mm
# reportlab cannot split a table row across pages: a cell taller than a page stops the build.
# Columns with their own `cut` use it; every other text value is cut here, as a safeguard.
CELL_MAX = 600

_INK = colors.HexColor("#1f2933")
_MUTED = colors.HexColor("#52606d")
_LINE = colors.HexColor("#cbd2d9")
_HEAD = colors.HexColor("#e6eef2")
TITLE = ParagraphStyle("title", fontName="Helvetica-Bold", fontSize=15, leading=19,
                       textColor=_INK, spaceAfter=6)
H2 = ParagraphStyle("h2", fontName="Helvetica-Bold", fontSize=11, leading=14, textColor=_INK,
                    spaceBefore=8, spaceAfter=4, keepWithNext=1)    # never alone at a page end
BODY = ParagraphStyle("body", fontName="Helvetica", fontSize=8.5, leading=11, textColor=_INK)
NOTE = ParagraphStyle("note", parent=BODY, textColor=_MUTED)
TABLE_TITLE = ParagraphStyle("table_title", parent=H2, keepWithNext=0)
CELL = ParagraphStyle("cell", fontName="Helvetica", fontSize=7.5, leading=9.2, textColor=_INK)
HEAD = ParagraphStyle("head", parent=CELL, fontName="Helvetica-Bold")

_fallback_chars: set[int] | None = None


def _fallback_font() -> str:
    """Registers the Noto Sans SC subset on first use (only loaded when a value needs it)."""
    _fallback_has("a")
    return FALLBACK_FONT


def _fallback_has(ch: str) -> bool:
    """Whether the fallback font has a glyph for ch (registers the font on first use)."""
    global _fallback_chars                               # pylint: disable=global-statement
    if _fallback_chars is None:
        from reportlab.pdfbase import pdfmetrics           # pylint: disable=import-outside-toplevel
        from reportlab.pdfbase.ttfonts import TTFont      # pylint: disable=import-outside-toplevel
        font = TTFont(FALLBACK_FONT, str(FONT_FILE))
        pdfmetrics.registerFont(font)
        _fallback_chars = set(font.face.charToGlyph)
    return ord(ch) in _fallback_chars


def _printable(ch: str) -> bool:
    return ch == "\n" or _in_helvetica(ch) or _fallback_has(ch)


def _nearest(ch: str) -> str:
    """A character no font has → its closest plain form: a fixed choice, its compatibility form
    (ᵀ → T, ⁰ → 0), its letter without accents (ő → o), or "?"."""
    if ch in _NEAREST:
        return _NEAREST[ch]
    if unicodedata.category(ch) == "Mn":                 # a lone combining accent
        return ""
    for form in (unicodedata.normalize("NFKC", ch),
                 "".join(c for c in unicodedata.normalize("NFKD", ch)
                         if unicodedata.category(c) != "Mn")):
        if form and form != ch and all(_printable(c) for c in form):
            return form
    return "?"


def printable_text(text: str) -> str:
    """Text the PDF can print: control characters dropped, missing glyphs replaced (_nearest)."""
    text = _CONTROL.sub("", text)
    if all(_in_helvetica(c) or c == "\n" for c in text):
        return text                                      # the common case: no font lookups
    return "".join(c if _printable(c) else _nearest(c) for c in text)


def _in_helvetica(ch: str) -> bool:
    """The built-in fonts use the WinAnsi (cp1252) encoding: anything outside it has no glyph."""
    try:
        ch.encode("cp1252")
        return True
    except UnicodeEncodeError:
        return False


def markup(value) -> str:
    """A value as safe Paragraph markup: escaped, with characters Helvetica lacks in the
    fallback font, and line breaks kept."""
    text = printable_text("" if value is None else str(value))
    out, run, plain = [], "", True
    for ch in text:
        ok = _in_helvetica(ch) or ch == "\n"
        if ok != plain and run:
            out.append(escape(run) if plain else f'<font name="{_fallback_font()}">{escape(run)}</font>')
            run = ""
        plain = ok
        run += ch
    if run:
        out.append(escape(run) if plain else f'<font name="{_fallback_font()}">{escape(run)}</font>')
    return "".join(out).replace("\n", "<br/>")


def _value(row: dict, col: Column):
    value = row.get(col.key)
    if value is None:
        return ""
    if col.kind == "int":
        return f"{int(value):,}"
    if col.kind == "date":
        return str(value)[:10]
    text = str(value)
    limit = col.cut or CELL_MAX
    return excerpt(text, len(text), limit) if len(text) > limit else text


def _table(doc_table) -> Table:
    cols = [c for c in doc_table.columns if c.pdf_mm]
    data = [[Paragraph(markup(c.title), HEAD) for c in cols]]
    data += [[Paragraph(markup(_value(r, c)), CELL) for c in cols] for r in doc_table.rows]
    t = Table(data, colWidths=[c.pdf_mm * mm for c in cols], repeatRows=1)
    t.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.4, _LINE),
        ("BACKGROUND", (0, 0), (-1, 0), _HEAD),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 3), ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2)]))
    return t


def _key_values(pairs: list[tuple[str, str]]) -> Table:
    t = Table([[Paragraph(markup(k), HEAD), Paragraph(markup(v), CELL)] for k, v in pairs],
              colWidths=[45 * mm, PAGE[0] - 2 * MARGIN - 45 * mm])
    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                           ("LEFTPADDING", (0, 0), (-1, -1), 0),
                           ("TOPPADDING", (0, 0), (-1, -1), 1),
                           ("BOTTOMPADDING", (0, 0), (-1, -1), 1)]))
    return t


def build(doc: Document) -> bytes:
    out = io.BytesIO()
    title = doc.title

    def footer(canvas, pdf):
        canvas.saveState()
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(_MUTED)
        canvas.drawString(MARGIN, 7 * mm, "Vigil MD · " + doc.made_at.strftime("%Y-%m-%d"))
        canvas.drawRightString(PAGE[0] - MARGIN, 7 * mm, f"Page {pdf.page}")
        canvas.restoreState()

    story = [Paragraph(markup(title), TITLE), _key_values(doc.header), Spacer(1, 4),
             Paragraph("Filters", H2), _key_values(doc.filters)]
    if doc.facts:                           # a device page's totals
        story += [Paragraph("Summary", H2), _key_values(doc.facts)]
    story += [Paragraph("Notes", H2)] + [Paragraph(markup(n), NOTE) for n in doc.notes]
    for table in doc.tables:
        if table.name == "Summary":
            continue                        # already shown above as the facts list
        # A table splits across pages; its heading moves on only when the header row and a few
        # rows no longer fit (keepWithNext would push the whole table to the next page).
        story.append(CondPageBreak(45 * mm))
        story.append(Paragraph(markup(table.title), TABLE_TITLE))
        story.append(_table(table) if table.rows else Paragraph("None.", BODY))
    pdf = SimpleDocTemplate(out, pagesize=PAGE, leftMargin=MARGIN, rightMargin=MARGIN,
                            topMargin=MARGIN, bottomMargin=14 * mm, title=title,
                            author="Vigil MD")
    pdf.build(story, onFirstPage=footer, onLaterPages=footer)
    return out.getvalue()
