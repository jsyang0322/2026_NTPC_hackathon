"""Bedrock Knowledge Bases 檢索封裝（路線 A，§4.4）。

RAG 的向量化/索引/檢索全交 Bedrock KB 託管；本模組只負責「呼叫 Retrieve API +
帶 metadata 過濾」。透過限流保護（KB Retrieve 亦計入 Bedrock 請求，§5）。

metadata 過濾策略（§4.2 方式 B）：
  - 法規/函釋：案由 OR 共通法規（common_law）
  - 歷史決定書：不硬過濾案由（共池 + 加權，§9.2），過濾只用 doc_type 與審查類型

dry_run 或未設 KB_ID 時回 mock，確保骨架可離線跑。
"""

from __future__ import annotations

import os

from .bedrock_client import BedrockClient, get_client

KB_ID = os.environ.get("BEDROCK_KB_ID", "")
AWS_REGION = os.environ.get("AWS_REGION", "us-west-2")


def _build_filter(route_key: str, doc_types: list[str], include_common_law: bool) -> dict:
    """組 Bedrock KB 的 metadata 過濾條件（§4.4）。"""
    ors: list[dict] = [{"equals": {"key": "case_type", "value": route_key}}]
    if include_common_law:
        ors.append({"equals": {"key": "doc_type", "value": "common_law"}})
    return {"orAll": ors} if len(ors) > 1 else ors[0]


def retrieve(
    query_text: str,
    route_key: str,
    doc_types: list[str] | None = None,
    include_common_law: bool = True,
    num_results: int = 5,
    client: BedrockClient | None = None,
) -> list[dict]:
    """呼叫 Bedrock KB Retrieve，回傳相關段落清單。

    回傳: [{"text": str, "score": float, "metadata": {...}, "source": str}]
    """
    client = client or get_client()
    doc_types = doc_types or ["law", "interpretation", "judgment", "decision"]

    if client.dry_run or not KB_ID:
        return [{
            "text": f"[DRY_RUN::KB] route={route_key} 的檢索結果（mock）",
            "score": 0.0, "metadata": {"case_type": route_key}, "source": "mock",
        }]

    # 真實呼叫：Bedrock Agent Runtime retrieve（受 §5 限流；此處計 1 次請求）
    import boto3

    client._throttle()  # KB Retrieve 亦計入 ≤1 RPS
    agent_rt = boto3.client("bedrock-agent-runtime", region_name=AWS_REGION)
    resp = agent_rt.retrieve(
        knowledgeBaseId=KB_ID,
        retrievalQuery={"text": query_text},
        retrievalConfiguration={
            "vectorSearchConfiguration": {
                "numberOfResults": num_results,
                "filter": _build_filter(route_key, doc_types, include_common_law),
            }
        },
    )
    results = []
    for r in resp.get("retrievalResults", []):
        results.append({
            "text": r.get("content", {}).get("text", ""),
            "score": r.get("score", 0.0),
            "metadata": r.get("metadata", {}),
            "source": r.get("location", {}).get("s3Location", {}).get("uri", ""),
        })
    return results
