"""F1 結構化擷取（§5.2）。[LLM]

兩層擷取：正規表示式抓格式固定欄位（日期/字號/金額），
LLM tool use 抓需要理解的欄位（主張拆項、機關認定事實），再以程式驗證。
所有 LLM 呼叫透過 bedrock_client（受 ≤1 RPS 限流）。

降低請求數：擷取、案由判斷、爭點標註可合併為一次呼叫（§13.3）。
"""

from __future__ import annotations

from .bedrock_client import BedrockClient, get_client, MODEL_LIGHT


def extract_fields(case_text: dict, client: BedrockClient | None = None) -> dict:
    """從案卷文字擷取結構化欄位。

    輸入 case_text: {"petition": str, "disposition": str, "reply": str}
    輸出: CaseFields.as_dict() 形狀的純 dict（見 schemas.CaseFields）
    """
    client = client or get_client()
    # TODO(§5.2): 正規表示式抓日期/字號/金額；LLM tool use 抓 claims/agency 認定事實
    # resp = client.converse(messages=[...], model_id=MODEL_LIGHT, system=...)
    _ = client  # 骨架階段先保留介面
    return {
        "petitioner": {},
        "original_disposition": {},
        "petition_filed_date": None,
        "requests": [],
        "claims": [],
        "agency_reply": {},
        "evidence_list": [],
        "case_type": None,
        "review_type": None,
        "_stub": True,
    }
