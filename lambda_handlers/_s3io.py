"""Lambda handler 共用的 S3 讀寫工具。

各 handler 從 S3 讀輸入 JSON、寫輸出 JSON，串成 pipeline。
本機測試模式（--test）改用本地檔案，不碰 S3。
"""

from __future__ import annotations

import json
import os

BUCKET = os.environ.get("PIPELINE_BUCKET", "")


def read_json(bucket: str, key: str) -> dict:
    import boto3
    s3 = boto3.client("s3")
    obj = s3.get_object(Bucket=bucket, Key=key)
    return json.loads(obj["Body"].read().decode("utf-8"))


def write_json(bucket: str, key: str, data: dict) -> None:
    import boto3
    s3 = boto3.client("s3")
    s3.put_object(
        Bucket=bucket, Key=key,
        Body=json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"),
        ContentType="application/json",
    )


def read_local(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def dump(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)
