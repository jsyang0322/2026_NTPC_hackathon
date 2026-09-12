"""Bedrock KB 上傳前處理（路線 A，§4.3）。[離線，不呼叫 LLM]

對每份 PDF：檔名正規化 → 內文正規化 → 案由正規化 → 產 metadata JSON。
產出 data/kb_source/（正規化文字 + 隨附 .metadata.json），供上傳 Bedrock KB。

Bedrock KB 支援每份文件隨附 metadata 檔（同名 .metadata.json），檢索時可用 metadata 過濾。
metadata 欄位須與 core.kb._build_filter 的過濾鍵一致：case_type、doc_type。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .parse import parse_filename, normalize_text, extract_pdf_text

# 中文案由 → route_key（與 schemas.ROUTE_KEYS 對齊）
CASE_TYPE_TO_ROUTE = {
    "違反洗錢防制法事件": "money_laundering",
    "違反廢棄物清理法事件": "waste",
    "空氣污染防制法事件": "air_pollution",
    "違反建築法事件": "building",
    "違反噪音管制法事件": "noise",
}

# 資料夾名 → doc_type（供 KB metadata 過濾）
FOLDER_TO_DOCTYPE = {
    "歷史訴願決定書": "decision",
    "相關法規": "law",
    "行政函釋": "interpretation",
    "司法院釋字及行政判解": "judgment",
}

# 共通法規（跨案由，metadata 標 common_law 供 OR 過濾）
COMMON_LAWS = {"訴願法", "行政程序法", "行政罰法", "行政執行法", "民法",
               "行政院及各級行政機關訴願審議委員會審議規則"}


def route_key_for(meta: dict) -> str:
    return CASE_TYPE_TO_ROUTE.get(meta.get("case_type", ""), "general")


def doc_type_for(pdf_path: Path) -> str:
    for folder, dt in FOLDER_TO_DOCTYPE.items():
        if folder in str(pdf_path):
            return dt
    return "unknown"


def build_metadata(pdf_path: Path) -> dict:
    """產生 Bedrock KB 用的 metadata（§4.3 第 4 步）。"""
    meta = parse_filename(pdf_path.name)
    doc_type = doc_type_for(pdf_path)
    stem = pdf_path.stem
    is_common = doc_type == "law" and any(cl in stem for cl in COMMON_LAWS)
    return {
        "metadataAttributes": {
            "doc_type": "common_law" if is_common else doc_type,
            "case_type": route_key_for(meta) if not meta.get("_parse_error") else "general",
            "year": meta.get("year", 0) or 0,
            "source_name": stem,
        }
    }


def ingest_one(pdf_path: Path, out_dir: Path) -> None:
    text = extract_pdf_text(pdf_path)               # 已含內文正規化（parse.normalize_text）
    base = out_dir / pdf_path.stem
    base.with_suffix(".txt").write_text(text, encoding="utf-8")
    base.with_suffix(".txt.metadata.json").write_text(
        json.dumps(build_metadata(pdf_path), ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="KB 上傳前處理 → data/kb_source/")
    parser.add_argument("--raw", default="data/raw")
    parser.add_argument("--out", default="data/kb_source")
    parser.add_argument("--metadata-only", action="store_true",
                        help="只產 metadata（不抽 PDF 內文，無需 PyMuPDF）")
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    pdfs = sorted(Path(args.raw).rglob("*.pdf"))
    print(f"找到 {len(pdfs)} 個 PDF")

    for pdf in pdfs:
        if args.metadata_only:
            base = out_dir / pdf.stem
            base.with_suffix(".txt.metadata.json").write_text(
                json.dumps(build_metadata(pdf), ensure_ascii=False, indent=2), encoding="utf-8")
        else:
            ingest_one(pdf, out_dir)
    print(f"完成，輸出於 {out_dir}")


if __name__ == "__main__":
    main()
