"""Builds export/fonts/NotoSansSC-Regular-subset.ttf, the PDF's fallback font, from the full
Noto Sans SC Regular (SIL Open Font License 1.1; the subset stays under the same licence).

Kept: the 6,763 common Chinese characters of GB2312 and the blocks below (Latin extended, Greek,
Cyrillic, punctuation, super/subscripts, symbols, arrows, math, CJK punctuation, Bopomofo, full-width
forms), as far as the font has them. On 2026-11-27 this covered every character of vigil_md_prod
(recalls, enforcement, search entries) that the full font covers; 2.57 MB instead of 10.6 MB.
A character outside the subset prints as its closest plain form or "?" (pdf.py), never a box;
the Excel file always has the original text.

Usage (from Model-Development/chatbot/; fontTools comes with matplotlib in requirements.txt):
    python scripts/subset_font.py <full NotoSansSC-Regular.ttf> export/fonts/NotoSansSC-Regular-subset.ttf
"""
from __future__ import annotations

import sys

from fontTools import subset
from fontTools.ttLib import TTFont

BLOCKS = [(0x0100, 0x024F), (0x0250, 0x02FF), (0x0300, 0x036F), (0x0370, 0x03FF),
          (0x0400, 0x04FF), (0x1D00, 0x1DBF), (0x1E00, 0x1EFF), (0x2000, 0x206F),
          (0x2070, 0x209F), (0x20A0, 0x20CF), (0x2100, 0x214F), (0x2150, 0x218F),
          (0x2190, 0x21FF), (0x2200, 0x22FF), (0x2300, 0x23FF), (0x2460, 0x24FF),
          (0x2500, 0x257F), (0x25A0, 0x25FF), (0x2600, 0x26FF), (0x3000, 0x303F),
          (0x3040, 0x30FF), (0x3100, 0x312F), (0xFF00, 0xFFEF)]


def gb2312() -> set[int]:
    """The Chinese characters of GB2312 (rows 0xB0-0xF7)."""
    out = set()
    for b1 in range(0xB0, 0xF8):
        for b2 in range(0xA1, 0xFF):
            try:
                out.add(ord(bytes([b1, b2]).decode("gb2312")))
            except UnicodeDecodeError:
                pass
    return out


def main(src: str, dst: str) -> int:
    cmap = TTFont(src).getBestCmap()
    codes = {u for a, b in BLOCKS for u in range(a, b + 1)} | gb2312()
    codes = sorted(u for u in codes if u in cmap)
    opts = subset.Options()
    opts.layout_features, opts.name_IDs, opts.notdef_outline = ["*"], ["*"], True
    font = TTFont(src)
    sub = subset.Subsetter(opts)
    sub.populate(unicodes=codes)
    sub.subset(font)
    font.save(dst)
    print(f"{dst}: {len(codes):,} characters")
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:3]))
