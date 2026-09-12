#!/usr/bin/env python3
"""把純文字檔轉成含中文的 PDF（模擬真實使用者上傳的訴願書/原處分書）。

用系統中文字體嵌入，確保 pypdf 抽字時能正確取回中文（Streamlit 上傳走的正是
pypdf 抽字路徑）。純為產生 Demo 測試檔用，非執行管線的一部分。

用法：
    python -m scripts.txt_to_pdf data/samples/訴願書_範例.txt
    python -m scripts.txt_to_pdf data/samples/*.txt        # shell 展開多檔
"""

from __future__ import annotations

import sys
from pathlib import Path

import pymupdf  # PyMuPDF

# pymupdf 內建繁體中文 CJK 字型代號：映射乾淨、自動子集化，不依賴系統字體。
_CJK_FONT = "china-t"

# A4 版面參數（點）
_PAGE_W, _PAGE_H = 595, 842
_MARGIN = 60
_FONT_SIZE = 12
_LINE_H = 20
_MAX_CHARS = 40  # 每行最多字數（中文），超過自動折行


def txt_to_pdf(txt_path: Path) -> Path:
    text = txt_path.read_text(encoding="utf-8")
    pdf_path = txt_path.with_suffix(".pdf")

    doc = pymupdf.open()

    def new_page():
        return doc.new_page(width=_PAGE_W, height=_PAGE_H)

    page = new_page()
    y = _MARGIN

    for raw_line in text.split("\n"):
        chunks = [raw_line[i:i + _MAX_CHARS] for i in range(0, len(raw_line), _MAX_CHARS)] or [""]
        for chunk in chunks:
            if y > _PAGE_H - _MARGIN:
                page = new_page()
                y = _MARGIN
            page.insert_text((_MARGIN, y), chunk, fontname=_CJK_FONT, fontsize=_FONT_SIZE)
            y += _LINE_H

    # subset_fonts：只嵌入用到的字形，PDF 檔案大幅縮小
    doc.subset_fonts()
    doc.save(pdf_path, garbage=4, deflate=True)
    doc.close()
    return pdf_path


def main(argv: list[str]) -> int:
    if not argv:
        print("用法：python -m scripts.txt_to_pdf <檔案.txt> [更多.txt ...]")
        return 2
    for arg in argv:
        src = Path(arg)
        if not src.exists():
            print(f"跳過（不存在）：{src}")
            continue
        out = txt_to_pdf(src)
        print(f"已產生：{out}（{out.stat().st_size} bytes）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
