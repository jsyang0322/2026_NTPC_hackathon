"""法律查詢輔助（獨立功能，不參與草稿管線）。[KB 檢索 + LLM]

定位（與 recommend_laws / similar_cases 明確區隔）：
  - 這是給承辦人「查詢相關法律」的獨立工具，不進 draft、不進 verify 白名單、
    不影響任何決定書結論。
  - 因此**刻意不做 citation 校驗**：允許 LLM 綜合 KB 檢索結果與自身法律知識，
    回覆比純 KB 命中更豐富的相關法規與相似案例。這正是它與 recommend_laws
    （只能引用檢索清單、供草稿引用可追溯）的根本差異。

流程（使用者主動點才跑，2 次呼叫，都走 bedrock_client 的 ≤1 RPS 閘門）：
  KB 檢索（kb.retrieve，1 次）→ Haiku 綜合（MODEL_LIGHT，1 次）→ Markdown 純文字。

輸出為 Markdown 字串，外殼（Streamlit）直接 st.markdown 渲染，不再結構化。
"""

from __future__ import annotations

from .bedrock_client import BedrockClient, get_client, MODEL_LIGHT
from . import kb, classify


# 中文案由 label → route_key（classify 回中文，本模組對外用英文枚舉，與 pipeline 一致）
_LABEL_TO_ROUTE = {
    "洗錢防制法": "money_laundering",
    "廢棄物清理法": "waste",
    "空氣污染防制法": "air_pollution",
    "建築法": "building",
    "噪音管制法": "noise",
    "其他": "general",
}

#: 檢索用查詢字串長度上限（kb.retrieve 內部亦會截，這裡先收斂避免組 prompt 過長）
_QUERY_MAX_CHARS = 500


def lookup_from_text(
    case_text: dict,
    route_key: str | None = None,
    client: BedrockClient | None = None,
) -> dict:
    """從原始案卷文字直接查詢（外殼入口）。

    route_key 為 None 時以 classify（關鍵字規則，0 次呼叫）自動分類；
    指定時直接採用（供 UI 讓使用者手動改案由）。

    參數：
        case_text: {"petition": ..., "original_disposition_doc": ...} 等自由文字。
        route_key: 英文枚舉（schemas.ROUTE_KEYS）或 None（自動分類）。
    回傳：見 lookup_laws_and_cases。
    """
    client = client or get_client()

    if route_key is None:
        cls = classify.classify_case_type(case_text)
        route_key = _LABEL_TO_ROUTE.get(cls.get("case_type", "其他"), "general")

    query_text = _build_query(case_text)
    result = lookup_laws_and_cases(route_key, query_text, client=client)
    return result


def lookup_laws_and_cases(
    route_key: str,
    query_text: str,
    client: BedrockClient | None = None,
) -> dict:
    """法律查詢輔助核心：KB 檢索（1 次）→ Haiku 綜合（1 次）→ Markdown。

    回傳：
        {"markdown": str,          # 給 st.markdown 直接渲染的 LLM 回覆
         "route_key": str,         # 實際使用的案由
         "retrieved_count": int}   # KB 命中段落數（供 UI 顯示「參考了幾筆」）
    """
    client = client or get_client()
    query_text = (query_text or "")[:_QUERY_MAX_CHARS]

    # 一次檢索撈法規層與歷史決定書；不硬過濾案由（共池），讓語意檢索有足夠素材。
    hits = kb.retrieve(
        query_text=query_text,
        route_key=route_key,
        doc_types=["law", "interpretation", "judgment", "decision"],
        include_common_law=True,
        num_results=8,
        client=client,
        filter_case_type=False,
    )

    prompt = _build_prompt(route_key, query_text, hits)
    # use_cache=False：法律小幫手為即時查詢工具，每次都重新問 LLM，
    # 不讀既有快取（避免同輸入秒回、失去「即時生成」的體驗）。仍受 ≤1 RPS 閘門保護。
    markdown = client.converse(
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        model_id=MODEL_LIGHT,
        temperature=0.3,
        use_cache=False,
    )
    return {
        "markdown": markdown,
        "route_key": route_key,
        "retrieved_count": len(hits or []),
    }


# 案由英文枚舉 → 中文名，供 prompt 讓 LLM 知道案由脈絡
_ROUTE_LABEL = {
    "money_laundering": "洗錢防制法",
    "waste": "廢棄物清理法",
    "air_pollution": "空氣污染防制法",
    "building": "建築法",
    "noise": "噪音管制法",
    "general": "一般行政法（其他案由）",
}


def _build_query(case_text: dict) -> str:
    """組檢索用查詢字串：優先原處分書 + 訴願主張。"""
    parts = [
        str(case_text.get("original_disposition_doc", "") or case_text.get("disposition", "") or ""),
        str(case_text.get("petition", "") or ""),
    ]
    return "\n".join(p for p in parts if p).strip()


def _build_prompt(route_key: str, query_text: str, hits: list[dict]) -> str:
    """組給 Haiku 的 prompt。塞入 KB 檢索結果，要求綜合檢索與自身知識回覆 Markdown。

    刻意允許 LLM 補充檢索結果以外的相關法規與案例（本功能定位為查詢輔助，
    非草稿引用來源，故不限制只能引用檢索清單）。
    """
    label = _ROUTE_LABEL.get(route_key, "一般行政法")
    retrieved = _format_hits(hits)

    return (
        "你是熟悉臺灣行政法與訴願實務的法律助理。承辦人正在查詢與本案相關的法規與"
        "歷史案例，作為研究參考（非決定書引用，無須拘泥於下列檢索結果）。\n\n"
        f"【案由】{label}\n"
        f"【案情摘要】{query_text[:800]}\n\n"
        f"【知識庫檢索到的參考資料】\n{retrieved}\n\n"
        "請綜合上述檢索資料與你自身的法律知識，以 Markdown 條列回覆下列兩部分：\n\n"
        "## 相關法規\n"
        "列出與本案相關的法律條文（法名 + 條號 + 一句白話說明其規範重點）。\n\n"
        "## 相似歷史案例與常見爭點\n"
        "說明此類案件常見的爭點、法院或訴願機關的常見見解方向。\n\n"
        "撰寫要求：用語精確、條理清楚；若引用具體條號請盡量正確；"
        "以繁體中文回覆。"
    )


def _format_hits(hits: list[dict]) -> str:
    """把 KB hits 攤成 prompt 可讀的條列文字。"""
    if not hits:
        return "（知識庫無相關檢索結果，請主要依你自身的法律知識回覆。）"
    lines = []
    for i, hit in enumerate(hits, start=1):
        if not isinstance(hit, dict):
            continue
        text = " ".join(str(hit.get("text", "")).split())[:300]
        meta = hit.get("metadata") if isinstance(hit.get("metadata"), dict) else {}
        tag = str(meta.get("doc_type", "") or "")
        lines.append(f"{i}. [{tag}] {text}")
    return "\n".join(lines) if lines else "（無有效檢索段落）"
