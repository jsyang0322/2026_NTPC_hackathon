"""F2 智能法規推薦（§8，路線 A：Bedrock KB）。[KB 檢索 + 版本檢查]

法規/函釋/判解可依案由過濾（案由 OR 共通法規），與歷史案例不同——法規層可硬過濾。
檢索用純語意（S3 Vectors 限制，§4.5），條號精確性由 verify_draft 的正則事後補償。
版本檢查（修正狀態）為純規則（§6.3）。
"""

from __future__ import annotations

from .bedrock_client import BedrockClient, get_client
from . import kb


def recommend_laws(
    route_key: str,
    query_text: str,
    version_db=None,
    num_results: int = 5,
    client: BedrockClient | None = None,
) -> list[dict]:
    """回傳推薦法條清單，每條附修正狀態與來源。

    路線 A：KB Retrieve（doc_type=law/interpretation/judgment，案由 OR 共通法規）。
    """
    client = client or get_client()
    hits = kb.retrieve(
        query_text=query_text,
        route_key=route_key,
        doc_types=["law", "interpretation", "judgment"],
        include_common_law=True,      # 法規層帶入共通法規（訴願法、行政程序法…）
        num_results=num_results,
        client=client,
    )
    # TODO(§8.3): 由 hits 整理成 {law, article, status, reason, source}
    # TODO(§6.3): version_db 檢查修正狀態（現行/已修正/已移列/已刪除）
    return hits
