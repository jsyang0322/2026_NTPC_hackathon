"""F3 相似案例比對（§9，路線 A：Bedrock KB）。[KB 檢索]

改用 core.kb.retrieve（Bedrock Knowledge Bases 託管），不自建記憶體索引。
歷史決定書「共池 + 案由加權」，不硬過濾案由（§9.2）：doc_type 過濾為 decision，
案由當加權特徵而非排除條件。
"""

from __future__ import annotations

from .bedrock_client import BedrockClient, get_client
from . import kb


def find_similar(
    route_key: str,
    query_text: str,
    top_k: int = 5,
    client: BedrockClient | None = None,
) -> list[dict]:
    """回傳前 top_k 筆相似歷史決定書（見 schemas.SimilarCase）。

    路線 A：呼叫 KB Retrieve，doc_type=decision，不硬過濾案由（共池）。
    案由加權可於取回後在應用層排序（同 route_key 加分）。
    """
    client = client or get_client()
    hits = kb.retrieve(
        query_text=query_text,
        route_key=route_key,
        doc_types=["decision"],
        include_common_law=False,   # 歷史案例不摻共通法規
        num_results=max(top_k * 4, 20),
        client=client,
    )
    # TODO(§9.2): 同 route_key 加權排序 → 取前 top_k；補 disposition / shared_issues
    return hits[:top_k]
