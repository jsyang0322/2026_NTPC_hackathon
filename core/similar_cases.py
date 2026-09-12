"""F3 相似案例比對（§9，路線 A：Bedrock KB）。[KB 檢索 + 應用層加權]

歷史決定書「共池 + 案由加權」，不硬過濾案由（§9.2）：實體案樣本少，若按案由硬過濾
會撈不到東西。因此 doc_type 只過濾 decision，案由改當**加權特徵**在應用層排序。

輸出為結構化清單（見 schemas.SimilarCase），不是 KB 原始 hits，因為：
  - `pipeline._decide_disposition()` 需要 `disposition` 欄位做主文多數決；
    回原始 hits 時該欄位不存在，多數決會永遠落到預設值。
  - `draft` 的 prompt 需要乾淨的論理摘要，不是整段雜訊。
"""

from __future__ import annotations

import re

from .bedrock_client import BedrockClient, get_client
from . import kb

#: 同案由的加權係數。案由相同者相關度加成，但不排除其他案由（共池原則）。
ROUTE_MATCH_BONUS = 1.25

#: 主文分類關鍵字。順序有意義：不受理優先（程序判斷不進實體）。
_DISPOSITION_PATTERNS = (
    ("訴願不受理", ("不受理",)),
    ("原處分撤銷", ("撤銷",)),
    ("訴願駁回", ("駁回",)),
)

#: 決定書年度樣式（民國年）
_YEAR_RE = re.compile(r"(\d{2,3})\s*年度?")


def find_similar(
    route_key: str,
    query_text: str,
    top_k: int = 5,
    client: BedrockClient | None = None,
) -> list[dict]:
    """回傳前 top_k 筆相似歷史決定書（結構同 schemas.SimilarCase）。

    每筆形狀：
        {"doc_id": "114訴字第0001號", "year": 114,
         "similarity": 0.91,            # 已含案由加權
         "disposition": "訴願駁回",      # 供 _decide_disposition 多數決
         "shared_issues": ["交付帳戶有無正當理由"],
         "reasoning_summary": "...",     # 供 draft 參考論理架構
         "route_key": "money_laundering",  # 該案例本身的案由
         "route_match": True,            # 是否與本案同案由
         "source": "s3://..."}

    檢索時多撈幾筆（top_k*4）再加權排序，避免加權後可選範圍太小。
    """
    client = client or get_client()
    hits = kb.retrieve(
        query_text=query_text,
        route_key=route_key,
        doc_types=["decision"],
        include_common_law=False,   # 歷史案例不摻共通法規
        num_results=max(top_k * 4, 20),
        client=client,
        filter_case_type=False,     # 共池：案由當加權特徵，不當過濾條件（§9.2）
    )
    cases = [_structure(h, route_key) for h in (hits or []) if isinstance(h, dict)]
    cases.sort(key=lambda c: c["similarity"], reverse=True)
    return cases[:top_k]


def _structure(hit: dict, route_key: str) -> dict:
    """把單筆 KB hit 轉成結構化案例，並套用案由加權。"""
    text = str(hit.get("text", ""))
    meta = hit.get("metadata") if isinstance(hit.get("metadata"), dict) else {}
    source = hit.get("source", "")

    case_route = str(meta.get("case_type", "") or "")
    route_match = bool(case_route) and case_route == route_key
    base = _as_float(hit.get("score"))

    return {
        "doc_id": str(meta.get("doc_id") or meta.get("case_id") or _doc_id_from_source(source)),
        "year": _year(meta, text),
        "similarity": round(base * (ROUTE_MATCH_BONUS if route_match else 1.0), 4),
        "base_score": base,
        "disposition": _disposition(meta, text),
        "shared_issues": _shared_issues(route_key, text),
        "reasoning_summary": _summarize(text),   # 短摘要，供 draft prompt（維持精簡）
        "full_text": " ".join((text or "").split()),  # 完整內文，供 UI 展開顯示
        "route_key": case_route,
        "route_match": route_match,
        "source": source,
    }


def _disposition(meta: dict, text: str) -> str:
    """取該案例的主文。metadata 優先，否則從內文判斷。

    判不出來時回空字串——`_decide_disposition` 會略過無主文的案例，
    不要臆測成「駁回」而讓多數決失真。
    """
    raw = str(meta.get("disposition", "") or "")
    for label, keywords in _DISPOSITION_PATTERNS:
        if any(k in raw for k in keywords):
            return label
    head = text[:200]          # 主文通常在決定書開頭
    for label, keywords in _DISPOSITION_PATTERNS:
        if any(k in head for k in keywords):
            return label
    return ""


def _shared_issues(route_key: str, text: str) -> list[str]:
    """找出案例內文命中本案由常見爭點者，供承辦人快速判斷可參考性。"""
    from .draft import get_profile      # 延遲匯入：只讀設定，避免模組層相依

    return [issue for issue in get_profile(route_key).get("common_issues", [])
            if _issue_hit(issue, text)]


def _issue_hit(issue: str, text: str) -> bool:
    """爭點標籤含括號註記（如「主觀故意過失(行政罰法§7)」），取主要詞比對。"""
    main = re.split(r"[（(]", issue)[0].strip()
    return bool(main) and main in text


def _year(meta: dict, text: str) -> int | None:
    """取決定書年度（民國年）。metadata 優先，否則從內文抓。"""
    raw = meta.get("year")
    try:
        if raw not in (None, ""):
            return int(raw)
    except (TypeError, ValueError):
        pass
    m = _YEAR_RE.search(text[:120])
    return int(m.group(1)) if m else None


def _doc_id_from_source(source: str) -> str:
    """無 metadata 時，以來源檔名當識別碼，至少可追溯。"""
    return source.rstrip("/").split("/")[-1] if source else ""


def _summarize(text: str, limit: int = 200) -> str:
    cleaned = " ".join((text or "").split())
    return cleaned[:limit] + ("…" if len(cleaned) > limit else "")


def _as_float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
