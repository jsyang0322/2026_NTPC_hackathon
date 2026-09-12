"""Bedrock Knowledge Bases 檢索封裝（路線 A，§4.4）。

RAG 的向量化/索引/檢索全交 Bedrock KB 託管；本模組只負責「呼叫 Retrieve API +
帶 metadata 過濾」。透過限流保護（KB Retrieve 亦計入 Bedrock 請求，§5）。

metadata 過濾策略（§4.2 方式 B）：
  - 法規/函釋/判解：案由 OR 共通法規（common_law）
  - 歷史決定書：不硬過濾案由（共池 + 加權，§9.2），只用 doc_type 過濾

三項專責 RAG（洗錢防制 / 廢棄物 / 空汙）兩種接法都支援，由環境變數決定：
  A. 單一 KB + metadata 過濾（架構定案的預設做法）
     BEDROCK_KB_ID=<kb>
     案由靠 case_type metadata 區隔，doc_type 區隔法規與決定書。
  B. 每案由一個專用 KB（資料分別落在三個 S3 bucket 時較直觀）
     BEDROCK_KB_ID_MONEY_LAUNDERING=<kb>
     BEDROCK_KB_ID_WASTE=<kb>
     BEDROCK_KB_ID_AIR_POLLUTION=<kb>
     此時案由已由 KB 本身隔開，過濾條件自動省略 case_type，只留 doc_type，
     避免「KB 已經只有這個案由，卻又用 case_type 過濾」導致零結果。
  兩者可混用：有專用 KB 的案由走專用，其餘 fallback 到 BEDROCK_KB_ID。

Retrieve API 有兩種設定形狀，用錯會 ValidationException：
  - managed KB（managedKnowledgeBaseConfiguration）→ managedSearchConfiguration
  - 自建向量庫 KB                                  → vectorSearchConfiguration
本模組預設 auto：先試 managed，被拒再試 vector，並記住該 KB 的形狀（每個 KB 只多
花一次探測）。已知可用 BEDROCK_KB_SEARCH_MODE=managed|vector 直接指定，省掉探測。

已知限制：managed store 不支援 startsWith 運算子（實測 ValidationException），
因此過濾只用 equals / in / orAll / andAll。

dry_run 或未設 KB id 時回 mock，確保骨架可離線跑。
"""

from __future__ import annotations

import os

from .bedrock_client import BedrockClient, get_client, throttle

KB_ID = os.environ.get("BEDROCK_KB_ID", "")
AWS_REGION = os.environ.get("AWS_REGION", "us-west-2")

#: 案由專用 KB 的環境變數名（key 與 schemas.ROUTE_KEYS 一致）
ROUTE_KB_ENV: dict[str, str] = {
    "money_laundering": "BEDROCK_KB_ID_MONEY_LAUNDERING",
    "waste": "BEDROCK_KB_ID_WASTE",
    "air_pollution": "BEDROCK_KB_ID_AIR_POLLUTION",
    "building": "BEDROCK_KB_ID_BUILDING",
    "noise": "BEDROCK_KB_ID_NOISE",
    "general": "BEDROCK_KB_ID_GENERAL",
}

#: managed | vector | auto
SEARCH_MODE = os.environ.get("BEDROCK_KB_SEARCH_MODE", "auto").strip().lower()

#: 過濾後零結果時，是否退一步用無過濾檢索再試一次。
#: 預設開啟：metadata 尚未補齊的 KB 若硬過濾會全空，整個 Demo 會啞掉；
#: 代價是這種情況多一次 Retrieve（仍受 ≤1 RPS 閘門保護）。
FILTER_FALLBACK = os.environ.get("BEDROCK_KB_FILTER_FALLBACK", "1") == "1"

#: 探測結果快取：kb_id -> "managed" | "vector"
_mode_cache: dict[str, str] = {}


def resolve_kb_id(route_key: str) -> tuple[str, bool]:
    """回傳 (kb_id, is_dedicated)。

    is_dedicated=True 表示這個 KB 只裝該案由的資料，過濾時就不該再加 case_type。
    """
    env_name = ROUTE_KB_ENV.get(route_key or "")
    if env_name:
        dedicated = os.environ.get(env_name, "").strip()
        if dedicated:
            return dedicated, True
    return os.environ.get("BEDROCK_KB_ID", KB_ID).strip(), False


def _build_filter(
    route_key: str,
    doc_types: list[str],
    include_common_law: bool,
    scoped: bool,
    filter_case_type: bool = True,
) -> dict | None:
    """組 Bedrock KB 的 metadata 過濾條件（§4.4）。

    filter_case_type=False：歷史決定書共池，案由當加權特徵不當過濾條件（§9.2）。
    scoped=True（案由專用 KB）時同樣不加 case_type：KB 本身已經只有該案由。
    回傳 None 表示不帶過濾（讓純語意檢索跑，不要因為過濾條件把結果清空）。
    """
    clauses: list[dict] = []

    if doc_types:
        clauses.append(_doc_type_clause(doc_types, include_common_law))

    if filter_case_type and not scoped and route_key:
        # 單一 KB：案由硬過濾，法規層額外放行共通法規（訴願法、行政程序法…）
        case_clause: dict = {"equals": {"key": "case_type", "value": route_key}}
        if include_common_law:
            case_clause = {"orAll": [
                case_clause,
                {"equals": {"key": "doc_type", "value": "common_law"}},
            ]}
        clauses.append(case_clause)

    if not clauses:
        return None
    if len(clauses) == 1:
        return clauses[0]
    return {"andAll": clauses}


def _doc_type_clause(doc_types: list[str], include_common_law: bool) -> dict:
    """doc_type 過濾：法規層要順帶放行 common_law，否則共通法規永遠撈不到。"""
    wanted = list(doc_types)
    if include_common_law and "common_law" not in wanted:
        wanted.append("common_law")
    if len(wanted) == 1:
        return {"equals": {"key": "doc_type", "value": wanted[0]}}
    return {"in": {"key": "doc_type", "value": wanted}}


def retrieve(
    query_text: str,
    route_key: str,
    doc_types: list[str] | None = None,
    include_common_law: bool = True,
    num_results: int = 5,
    client: BedrockClient | None = None,
    filter_case_type: bool = True,
) -> list[dict]:
    """呼叫 Bedrock KB Retrieve，回傳相關段落清單。

    filter_case_type=False 時不以案由過濾（歷史決定書共池，§9.2）。

    回傳: [{"text": str, "score": float, "metadata": {...}, "source": str}]
    """
    client = client or get_client()
    doc_types = doc_types or ["law", "interpretation", "judgment", "decision"]
    kb_id, scoped = resolve_kb_id(route_key)

    if client.dry_run or not kb_id:
        return [{
            "text": f"[DRY_RUN::KB] route={route_key} 的檢索結果（mock）",
            "score": 0.0, "metadata": {"case_type": route_key}, "source": "mock",
        }]

    filt = _build_filter(route_key, doc_types, include_common_law, scoped, filter_case_type)
    hits = _retrieve_raw(kb_id, query_text, num_results, filt)

    if not hits and filt is not None and FILTER_FALLBACK:
        # metadata 尚未補齊時，硬過濾會全空。退一步用純語意再試一次，
        # 讓流程有東西可用；呼叫端可從 metadata 判斷是否為期望的案由/型別。
        hits = _retrieve_raw(kb_id, query_text, num_results, None)

    return hits


def _retrieve_raw(kb_id: str, query_text: str, num_results: int,
                  filt: dict | None) -> list[dict]:
    """實際呼叫 Retrieve。自動處理 managed / vector 兩種設定形狀。"""
    modes = _modes_to_try(kb_id)
    last_error: Exception | None = None

    for mode in modes:
        cfg: dict = {"numberOfResults": num_results}
        if filt is not None:
            cfg["filter"] = filt
        key = "managedSearchConfiguration" if mode == "managed" else "vectorSearchConfiguration"

        throttle()   # KB Retrieve 亦計入 ≤1 RPS，與 LLM 呼叫共用同一個閘門
        try:
            resp = _agent_runtime().retrieve(
                knowledgeBaseId=kb_id,
                retrievalQuery={"text": query_text},
                retrievalConfiguration={key: cfg},
            )
        except Exception as exc:                      # noqa: BLE001 — 需依訊息判斷
            if _is_wrong_shape(exc) and len(modes) > 1:
                last_error = exc
                continue                              # 換另一種形狀再試
            raise
        _mode_cache[kb_id] = mode                     # 記住，之後不再探測
        return [_normalize(r) for r in resp.get("retrievalResults", [])]

    raise last_error if last_error else RuntimeError("KB Retrieve 失敗")


def _modes_to_try(kb_id: str) -> list[str]:
    if SEARCH_MODE in ("managed", "vector"):
        return [SEARCH_MODE]
    cached = _mode_cache.get(kb_id)
    if cached:
        return [cached]
    return ["managed", "vector"]      # auto：先試 managed（新版 KB 預設）


def _is_wrong_shape(exc: Exception) -> bool:
    """判斷是否為「設定形狀用錯」的 ValidationException（而非權限/參數錯）。"""
    msg = str(exc)
    return "ValidationException" in msg and (
        "managedSearchConfiguration" in msg or "vectorSearchConfiguration" in msg
    )


def _normalize(result: dict) -> dict:
    """把 Retrieve 回傳統一成本專案的 hit 形狀。

    managed KB 的來源在 metadata['_source_uri'] / documentId（s3Location.uri 會是
    https 形式），vector KB 在 location.s3Location.uri，兩者都收。
    """
    meta = result.get("metadata") or {}
    location = result.get("location") or {}
    source = (
        (location.get("s3Location") or {}).get("uri")
        or result.get("documentId")
        or meta.get("_source_uri", "")
        or ""
    )
    return {
        "text": (result.get("content") or {}).get("text", ""),
        "score": result.get("score", 0.0),
        "metadata": meta,
        "source": source,
    }


def _agent_runtime():
    import boto3          # 延遲 import：dry_run 或未裝 boto3 時不受影響

    return boto3.client("bedrock-agent-runtime", region_name=AWS_REGION)
