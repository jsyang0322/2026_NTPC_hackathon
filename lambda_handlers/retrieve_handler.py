"""Lambda ③：KB 檢索（法規推薦 + 相似案例）。

analyze_handler 已含檢索；此 handler 供「檢索獨立成一步」的 Step Functions 用，
呼叫 core.recommend_laws / core.similar_cases（皆走 Bedrock KB）。
事件格式：{"bucket": ..., "key": "analysis/xxx.json"}
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.recommend_laws import recommend_laws
from core.similar_cases import find_similar
from core.bedrock_client import get_client
from lambda_handlers import _s3io


def handler(event, context=None):
    analysis = _s3io.read_json(event["bucket"], event["key"])
    route_key = analysis.get("route_key", "general")
    q = analysis.get("_query_text", "") or _rebuild_query(analysis)
    client = get_client()
    analysis["recommended_laws"] = recommend_laws(route_key, q, client=client)
    analysis["similar_cases"] = find_similar(route_key, q, client=client)
    _s3io.write_json(event["bucket"], event["key"], analysis)
    return {"bucket": event["bucket"], "key": event["key"], "next": "draft"}


def _rebuild_query(analysis: dict) -> str:
    fields = analysis.get("fields", {})
    return " ".join(c.get("summary", "") for c in fields.get("claims", []))


def _test():
    print(_s3io.dump({
        "laws": recommend_laws("money_laundering", "測試", client=get_client(dry_run=True)),
        "cases": find_similar("money_laundering", "測試", client=get_client(dry_run=True)),
    }))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true")
    if ap.parse_args().test:
        _test()
