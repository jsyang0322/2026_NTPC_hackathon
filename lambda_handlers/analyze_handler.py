"""Lambda ②：分析（路由 + KB 檢索 + 建議主文）。

呼叫 core.pipeline.analyze_case（內含 router、recommend_laws、similar_cases）。
事件格式：{"bucket": ..., "key": "input/xxx.json"}
輸出：analysis/xxx.json，回傳 {"bucket","key","next":"await_confirm"}
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.pipeline import analyze_case
from core.bedrock_client import get_client
from lambda_handlers import _s3io


def handler(event, context=None):
    payload = _s3io.read_json(event["bucket"], event["key"])
    analysis = analyze_case(payload, client=get_client())
    out_key = event["key"].replace("input/", "analysis/")
    _s3io.write_json(event["bucket"], out_key, analysis)
    # 若程序不受理，直接結束；否則等待承辦人確認主文
    nxt = "done" if analysis.get("inadmissible") else "await_confirm"
    return {"bucket": event["bucket"], "key": out_key, "next": nxt,
            "route_key": analysis.get("route_key")}


def _test():
    from core.pipeline import _demo_payload
    analysis = analyze_case(_demo_payload(), client=get_client(dry_run=True))
    print(_s3io.dump(analysis))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true")
    if ap.parse_args().test:
        _test()
