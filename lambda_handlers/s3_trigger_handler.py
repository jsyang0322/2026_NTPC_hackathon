"""S3 事件觸發 Lambda：上傳案卷到 S3 → 啟動 Step Functions 狀態機。

綁定 S3「PUT 物件」事件通知（前綴 input/、後綴 .json）。
收到事件後，以該物件為輸入啟動 pipeline 狀態機。
事件格式：標準 S3 Event Notification。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

STATE_MACHINE_ARN = os.environ.get("STATE_MACHINE_ARN", "")


def handler(event, context=None):
    import boto3
    sfn = boto3.client("stepfunctions")
    started = []
    for rec in event.get("Records", []):
        bucket = rec["s3"]["bucket"]["name"]
        key = rec["s3"]["object"]["key"]
        resp = sfn.start_execution(
            stateMachineArn=STATE_MACHINE_ARN,
            input=json.dumps({"bucket": bucket, "key": key}),
        )
        started.append(resp["executionArn"])
    return {"started": started}


def _test():
    fake_event = {"Records": [{"s3": {"bucket": {"name": "demo-bucket"},
                                       "object": {"key": "input/sim-114-001.json"}}}]}
    print("模擬 S3 事件解析：")
    for rec in fake_event["Records"]:
        print("  bucket:", rec["s3"]["bucket"]["name"], "key:", rec["s3"]["object"]["key"])
    print("（本機測試不實際啟動 Step Functions；部署後由 STATE_MACHINE_ARN 觸發）")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true")
    if ap.parse_args().test:
        _test()
