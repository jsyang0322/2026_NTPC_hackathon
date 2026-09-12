"""Lambda ⑤：草稿驗證 + 對抗式審查。

呼叫 core.verify.verify_draft（純規則）與 core.critic.adversarial_review（Claude）。
事件格式：{"bucket": ..., "key": "draft/xxx.json", "analysis_key": "analysis/xxx.json"}
輸出：result/xxx.json（草稿 + 檢核報告），回傳 {"bucket","key","next":"done"}
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.verify import verify_draft
from core.critic import adversarial_review
from core.bedrock_client import get_client
from lambda_handlers import _s3io


def handler(event, context=None):
    draft = _s3io.read_json(event["bucket"], event["key"])
    analysis = _s3io.read_json(event["bucket"], event["analysis_key"])
    fields = analysis.get("fields", {})
    client = get_client()
    report = verify_draft(draft, fields, analysis.get("recommended_laws", []), {}, analysis.get("defects", []))
    review = adversarial_review(draft, fields, client=client)
    result = {"draft": draft, "verification": report, "adversarial": review}
    out_key = event["key"].replace("draft/", "result/")
    _s3io.write_json(event["bucket"], out_key, result)
    return {"bucket": event["bucket"], "key": out_key, "next": "done"}


def _test():
    draft = {"main": "訴願駁回", "reasons": [], "route_key": "money_laundering"}
    report = verify_draft(draft, {}, [], {}, [])
    review = adversarial_review(draft, {}, client=get_client(dry_run=True))
    print(_s3io.dump({"verification": report, "adversarial": review}))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true")
    if ap.parse_args().test:
        _test()
