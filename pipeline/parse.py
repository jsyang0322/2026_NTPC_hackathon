"""PDF 解析與結構切分（§4.1–§4.3）。

- PyMuPDF（fitz）抽文字，資料集 141 份全具文字層，不需 OCR、不用 Textract。
- 檔名正規化：移除「 的副本」與重複副檔名。
- 檔名 metadata：兩種格式（含/不含「理由」段），以切段解析。
- 內容正規化：移除跨行數字與不規則空白。
- 段落角色標註：law_basis / application / conclusion；事實欄再切三段。

此腳本為離線前處理，產出 data/parsed/*.json。檔名解析與正規化已可用；
PDF 內容切分標為 TODO，待實際跑資料後校準關鍵字規則。
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


# ---------- 檔名處理（§4.2，已可用）----------
CASE_TYPE_ALIASES = {
    "空氣汙染防制法": "空氣污染防制法",
    "空氣汙染管制法": "空氣污染防制法",
    "空氣污染管制法": "空氣污染防制法",
}


def normalize_case_type(raw: str) -> str:
    """合併汙／污、防制／管制等寫法差異（§4.2）。"""
    return CASE_TYPE_ALIASES.get(raw, raw)


def strip_copy_suffix(raw_name: str) -> str:
    """移除資料集特有的「 的副本」與重複副檔名。"""
    name = re.sub(r"\.pdf 的副本\.pdf$", "", raw_name)
    name = re.sub(r"\.pdf$", "", name)
    return name


def parse_filename(raw_name: str) -> dict:
    """解析檔名為 metadata。支援兩種格式：
       序號.年度-案由-條款-理由-主文  /  序號.年度-案由-條款-主文（缺理由，如 114#21）。
    """
    name = strip_copy_suffix(raw_name)
    m = re.match(r"^(\d+)\.(\d{3})年-(.+)$", name)
    if not m:
        return {"_parse_error": True, "raw": raw_name}
    serial, year, rest = m.group(1), int(m.group(2)), m.group(3)
    parts = rest.split("-")
    case_type = parts[0]
    basis = parts[1] if len(parts) > 1 else None
    if len(parts) == 3:            # 案由-條款-主文
        reason, disposition = None, parts[2]
    elif len(parts) >= 4:          # 案由-條款-理由-主文
        reason, disposition = parts[2], "-".join(parts[3:])
    else:
        reason, disposition = None, None
    return {
        "serial": serial,
        "year": year,
        "case_type": normalize_case_type(case_type),
        "case_type_raw": case_type,
        "basis": basis,
        "reason": reason,
        "disposition": disposition,
    }


# ---------- 內容正規化（§4.1）----------
def normalize_text(text: str) -> str:
    """移除跨行數字與不規則空白（否則日期與條號抽取必定失敗）。"""
    text = text.replace("\r\n", "\n")
    # 跨行數字：「1\n13 年」→「113 年」
    text = re.sub(r"(?<=\d)\s*\n\s*(?=\d)", "", text)
    # 條號中的不規則空白：「第 22 條第 1  項」
    text = re.sub(r"[ \t\u3000]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ---------- PDF 抽文字（§4.1）----------
def extract_pdf_text(pdf_path: Path) -> str:
    """以 PyMuPDF 抽取文字並正規化。"""
    import fitz  # PyMuPDF

    doc = fitz.open(pdf_path)
    pages = [page.get_text() for page in doc]
    doc.close()
    return normalize_text("\n".join(pages))


# ---------- 結構切分（§4.3，TODO：待實際資料校準）----------
def split_sections(text: str, meta: dict) -> dict:
    """依角色關鍵字切分：law_basis(按…規定)、application(卷查/經查/惟查)、
       conclusion(綜上論結)；事實欄再切 narrative / petitioner_claim / agency_reply。
    """
    # TODO(§4.3): 以開頭關鍵字規則切段，實測 101 件後校準
    return {"main": None, "facts": {}, "reasons": [], "raw_text": text, "_stub": True}


def parse_one(pdf_path: Path) -> dict:
    meta = parse_filename(pdf_path.name)
    text = extract_pdf_text(pdf_path)
    doc_id = f"{meta.get('year')}#{meta.get('serial')}"
    return {"doc_id": doc_id, "meta": meta, "sections": split_sections(text, meta)}


def main() -> None:
    parser = argparse.ArgumentParser(description="解析 PDF → data/parsed/*.json")
    parser.add_argument("--raw", default="data/raw", help="原始 PDF 目錄")
    parser.add_argument("--out", default="data/parsed", help="輸出 JSON 目錄")
    parser.add_argument("--names-only", action="store_true",
                        help="只解析檔名 metadata，不抽 PDF 內文（無需 PyMuPDF）")
    args = parser.parse_args()

    raw_dir, out_dir = Path(args.raw), Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    pdfs = sorted(raw_dir.rglob("*.pdf"))
    print(f"找到 {len(pdfs)} 個 PDF")

    ok, err = 0, 0
    for pdf in pdfs:
        meta = parse_filename(pdf.name)
        if meta.get("_parse_error"):
            err += 1
            print(f"[檔名解析失敗] {pdf.name}")
            continue
        if not args.names_only:
            try:
                record = parse_one(pdf)
                (out_dir / f"{record['doc_id']}.json").write_text(
                    json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception as e:  # noqa: BLE001
                err += 1
                print(f"[解析失敗] {pdf.name}: {e}")
                continue
        ok += 1
    print(f"完成：成功 {ok}，失敗 {err}")


if __name__ == "__main__":
    main()
