"""Henry 前段 → core 後段 交接轉接層。

Henry 的 workflow_appeal.py 從 S3 PDF 產出一份 JSON（受理註記 + 類別註記 +
訴願書結構化 + 判定過程）。本模組把該 JSON 轉成 core.schemas 的交接契約
payload，讓 henry 前段直接接上 core.pipeline 後段（不改 henry、不重做 core 前段）。

設計原則：
  - 忠實搬運 henry 已擷取的欄位，不重新呼叫 LLM。
  - route_key：henry 類別註記只有 4 類（空污/洗錢/廢清/其餘）；「其餘」涵蓋
    建築/噪音等，粒度較粗。此時用 core.classify 的關鍵字後備（0 呼叫）就全文
    再細分一次，補回 building/noise，仍分不出才落 general。
  - claims 缺口：henry 未把訴願理由拆成逐項主張，而 core 撰稿的逐項回應需要
    claims。以規則從「理由」段落切分（序號/分行/句號），不呼叫 LLM。
"""

from __future__ import annotations

import re

from .schemas import build_empty_input
from . import classify as classify_mod
from . import citations as citations_mod


# henry 類別註記碼 → core route_key
_CAT_TO_ROUTE = {
    0: None,                 # 不受理，不分類
    1: "air_pollution",      # 空氣污染防制法
    2: "money_laundering",   # 洗錢防制法
    3: "waste",              # 廢棄物清理法
    4: None,                 # 其餘案件：需 core classify 再細分
}

_CAT_LABEL = {
    1: "空氣污染防制法",
    2: "洗錢防制法",
    3: "廢棄物清理法",
}

# core classify 中文 label → route_key（用於「其餘」再細分）
_LABEL_TO_ROUTE = {
    "洗錢防制法": "money_laundering",
    "廢棄物清理法": "waste",
    "空氣污染防制法": "air_pollution",
    "建築法": "building",
    "噪音管制法": "noise",
    "其他": "general",
}


# ---------- claims 切分（規則，0 呼叫）----------
# 中文條列序號：一、二、三… / (一)(二) / 1. 2. / 第一點
_CLAIM_SPLIT_RE = re.compile(
    r"(?:^|\n)\s*(?:[一二三四五六七八九十]+[、.．)）]|[（(][一二三四五六七八九十]+[)）]|\d+[、.．)）])\s*"
)


def _split_claims(reasons_text: str) -> list[dict]:
    """把訴願理由段落切成逐項 claims。無明顯序號時以句號分段，仍無則整段一項。"""
    text = (reasons_text or "").strip()
    if not text:
        return []

    # 先試條列序號切分
    parts = [p.strip() for p in _CLAIM_SPLIT_RE.split(text) if p.strip()]
    if len(parts) <= 1:
        # 無序號：以句末標點分段
        parts = [p.strip() for p in re.split(r"[。；]\s*", text) if p.strip()]

    claims: list[dict] = []
    for i, p in enumerate(parts, 1):
        summary = p if len(p) <= 60 else p[:57] + "…"
        claims.append({"id": f"C{i}", "summary": summary, "quote": p})
    return claims or [{"id": "C1", "summary": text[:57], "quote": text}]


def _extract_legal_basis(doc: dict) -> list[str]:
    """從 henry 訴願書文字補抽法令依據（法名第N條），供填 original_disposition。

    henry 未逐條抽 legal_basis；此處用 citations.extract_parsed 從
    事實 + 理由 + 全文正則補抽（0 呼叫），保序去重回傳 citation 字串清單。
    目的是讓 core D1 健檢不因「完全無法令依據」而誤判撤銷；D1 只檢查有無記載，
    故即使抽到的引用混有訴願人主張所援法條亦不影響此判斷。
    """
    text = "\n".join(filter(None, [
        doc.get("事實") or "",
        doc.get("理由") or "",
        doc.get("全文") or "",
    ]))
    return [p["citation"] for p in citations_mod.extract_parsed(text)]


# ---------- 主轉接 ----------
def _resolve_route(cat_code, full_text: str) -> tuple[str, str]:
    """由 henry 類別註記決定 route_key；「其餘」用 core classify 全文再細分。

    回傳 (route_key, label)。
    """
    route = _CAT_TO_ROUTE.get(cat_code)
    if route:
        return route, _CAT_LABEL.get(cat_code, route)

    # cat_code == 4（其餘）或 None：用 core classify 關鍵字後備細分（0 呼叫）
    cls = classify_mod.classify_case_type({"text": full_text or ""})
    label = cls.get("case_type", "其他")
    return _LABEL_TO_ROUTE.get(label, "general"), label


def _digit(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def from_henry_output(henry_json: dict) -> dict:
    """把 henry workflow_appeal 的單案 JSON 轉成 core 交接契約 payload。"""
    doc = henry_json.get("訴願書", {}) or {}
    judge = henry_json.get("判定", {}) or {}
    accepted = _digit(henry_json.get("受理註記"), default=1)   # 1=受理 0=不受理
    cat_code = _digit(henry_json.get("類別註記"), default=4)
    full_text = doc.get("全文", "") or ""

    payload = build_empty_input()
    payload["case_id"] = henry_json.get("案號", "") or ""

    # --- admissibility（受理註記 → 快速通道）---
    is_admissible = accepted == 1
    note = ""
    if not is_admissible:
        clause = judge.get("不受理款次") or ""
        reason = judge.get("判定理由") or henry_json.get("受理註記說明", "")
        note = f"（{clause}）{reason}".strip() if clause else reason
    payload["admissibility"] = {"is_admissible": is_admissible, "note": note}

    # --- case_type / route_key ---
    route_key, label = _resolve_route(cat_code, full_text)
    payload["case_type"] = {
        "label": label,
        "route_key": route_key,
        "confidence": 0.0,          # henry 未輸出數值信心，交後段 quality_flags 判斷
        "need_human_review": False,
    }

    # --- extracted_fields ---
    ef = payload["extracted_fields"]
    ef["petitioner"] = {
        "name": doc.get("訴願人") or "",
        "address": "",              # henry 未單獨抽地址（去識別化考量，留空）
        "agent": "",
    }
    ef["original_disposition"] = {
        "agency": doc.get("原行政處分機關") or "",
        "date": doc.get("處分發文日") or "",
        "doc_no": doc.get("處分書文號") or "",
        # henry 未逐條抽法令依據，改由 adapter 從全文/事實/理由正則補抽（0 呼叫），
        # 避免 core D1 健檢因「完全無法令依據」而誤判撤銷。
        "legal_basis": _extract_legal_basis(doc),
        "penalty_amount": 0,
        "service_date": doc.get("收受處分日") or "",
        "act_date": "",
    }
    ef["petition_filed_date"] = doc.get("提起訴願日") or ""
    req = doc.get("訴願請求事項")
    ef["requests"] = [req] if req else []
    ef["claims"] = _split_claims(doc.get("理由") or "")
    ef["evidence_list"] = []
    # 保留 henry 的成年/法人判定供追溯（procedure 77(4) 可參考）
    ef["_henry"] = {
        "是否成年": doc.get("是否成年"),
        "是否法人": doc.get("是否法人"),
        "事實": doc.get("事實"),
    }

    # --- raw_text ---
    payload["raw_text"] = {
        "petition": full_text,      # henry 全文含事實+理由，供 KB 檢索與對抗式審查
        "original_disposition_doc": (doc.get("事實") or "") + "\n" + (doc.get("理由") or ""),
    }
    return payload
