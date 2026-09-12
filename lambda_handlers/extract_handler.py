"""Lambda ①：擷取（前段/同學負責的 F1；此處為外殼示範）。

實務上擷取由前段完成後交 JSON。此 handler 保留供「若擷取也上雲」時使用，
呼叫 core.extract.extract_fields，不重寫業務邏輯。

事件格式：{"bucket": ..., "key": "input/xxx.json"}
輸出：fields/xxx.json，回傳 {"bucket","key","next":"analyze"}
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.extract import extract_fields
from core.bedrock_client import get_client
from lambda_handlers import _s3io


def handler(event, context=None):
    payload = _s3io.read_json(event["bucket"], event["key"])
    client = get_client()
    fields = extract_fields(payload.get("raw_text", {}), client=client)
    out_key = event["key"].replace("input/", "fields/")
    _s3io.write_json(event["bucket"], out_key, fields)
    return {"bucket": event["bucket"], "key": out_key, "next": "analyze"}


def _test():
    from core.bedrock_client import get_client as gc
    fields = extract_fields({"petition": "測試", "original_disposition_doc": "", "agency_reply_doc": ""},
                            client=gc(dry_run=True))
    print(_s3io.dump(fields))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true")
    if ap.parse_args().test:
        _test()
