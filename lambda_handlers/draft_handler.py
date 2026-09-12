"""Lambda ④：撰寫草稿（承辦人確認主文後觸發）。

呼叫 core.draft.generate_draft（一次 Bedrock converse 呼叫 Claude）。
事件格式：{"bucket": ..., "key": "analysis/xxx.json", "confirmed_disposition": "訴願駁回"}
輸出：draft/xxx.json，回傳 {"bucket","key","next":"verify"}
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.draft import generate_draft
from core.bedrock_client import get_client
from lambda_handlers import _s3io


def handler(event, context=None):
    analysis = _s3io.read_json(event["bucket"], event["key"])
    draft = generate_draft(
        analysis.get("route_key", "general"),
        event["confirmed_disposition"],
        analysis.get("fields", {}),
        analysis.get("recommended_laws", []),
        analysis.get("similar_cases", []),
        analysis.get("defects", []),
        client=get_client(),
    )
    out_key = event["key"].replace("analysis/", "draft/")
    _s3io.write_json(event["bucket"], out_key, draft)
    return {"bucket": event["bucket"], "key": out_key, "next": "verify"}


def _test():
    draft = generate_draft(
        "money_laundering", "訴願駁回",
        {"case_type": "money_laundering", "claims": [{"id": "C1", "summary": "原處分認定事實有誤"}]},
        [], [], [], client=get_client(dry_run=True),
    )
    print(_s3io.dump(draft))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true")
    if ap.parse_args().test:
        _test()
