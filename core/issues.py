"""F1 爭點標註（§5.4）。[LLM]

以種子爭點表（取自資料集函釋與判解主題）+ LLM 標註 + 人工校正。
不使用分群（資料量不足）。程序不受理案件的爭點直接對應訴願法§77 各款（由規則決定）。
"""

from __future__ import annotations

from .bedrock_client import BedrockClient, get_client, MODEL_LIGHT

# 種子爭點表（§5.4，取自資料集函釋/判解，可擴充）
SEED_ISSUES = {
    "廢棄物清理法": ["共有人連帶責任", "行為責任與狀態責任", "清除義務主體"],
    "建築法": ["停歇業公安申報", "場所區隔方式", "簽證內容不實"],
    "洗錢防制法": ["交付帳戶有無正當理由", "主觀故意過失(行政罰法§7)"],
    "共通": ["從新從輕(行政罰法§5)", "權利保護必要", "行政程序再開"],
}


def tag_issues(fields: dict, client: BedrockClient | None = None) -> dict:
    """對每項主張標註所屬爭點（可多標籤）。

    輸出: {"issues": [{"claim_id": "C1", "tags": [...]}], ...}
    """
    client = client or get_client()
    _ = client
    return {"issues": [], "_stub": True}
