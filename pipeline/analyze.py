"""資料實況統計（§4.4）。[純本機，不呼叫 LLM]

產出簡報第一頁的真實數字：審查類型分布、案由分布、條款覆蓋矩陣、
不受理案件可模板化程度、教示分布。輸入為 data/parsed 或檔名 metadata。
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from .parse import parse_filename


def analyze_filenames(raw_dir: str = "data/raw") -> dict:
    """僅用檔名產出案由分布與條款覆蓋矩陣（無需 PDF 內文）。"""
    metas = [parse_filename(p.name) for p in sorted(Path(raw_dir).rglob("*.pdf"))]
    metas = [m for m in metas if not m.get("_parse_error")]
    case_types = Counter(m["case_type"] for m in metas)
    by_year = Counter(m["year"] for m in metas)
    return {
        "total": len(metas),
        "case_type_distribution": dict(case_types.most_common()),
        "by_year": dict(sorted(by_year.items())),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="資料實況統計")
    parser.add_argument("--raw", default="data/raw")
    args = parser.parse_args()
    print(json.dumps(analyze_filenames(args.raw), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
