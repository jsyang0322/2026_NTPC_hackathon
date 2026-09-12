"""F2 智能法規推薦（§8，路線 A：Bedrock KB）。[KB 檢索 + 版本檢查]

法規/函釋/判解可依案由過濾（案由 OR 共通法規），與歷史案例不同——法規層可硬過濾。
檢索用純語意（S3 Vectors 限制，§4.5），條號精確性由 core.citations 的正則解析補足。
版本檢查（修正狀態）為純規則（§6.3）。

輸出為**結構化清單**而非 KB 原始 hits，這點很重要：
  - `draft` 的 prompt 只列乾淨的條號，模型不易引用到雜訊。
  - `verify` 的 V1 白名單直接吃 `citation` 欄位，比從自由文字硬撈精確。
    若回原始 hits，白名單會包含「檢索段落裡剛好被提到但不該引用」的條文。
"""

from __future__ import annotations

from .bedrock_client import BedrockClient, get_client
from . import kb
from .citations import extract_parsed


def recommend_laws(
    route_key: str,
    query_text: str,
    version_db=None,
    num_results: int = 5,
    client: BedrockClient | None = None,
) -> list[dict]:
    """回傳結構化推薦法條清單。

    每筆形狀：
        {"citation": "洗錢防制法第22條",   # 正規化條號，verify 白名單的鍵
         "law": "洗錢防制法", "article": "22", "sub_article": None,
         "status": "unknown|現行|已修正|已移列|已刪除",
         "doc_type": "law|interpretation|judgment",
         "score": 0.87,                    # KB 相關度
         "reason": "檢索命中段落摘要",      # 為何推薦，供承辦人判斷
         "source": "s3://.../訴願法.pdf"}   # 追溯來源

    同一條文被多個段落命中時合併，取最高分並保留第一個來源。
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
    laws = _structure(hits)
    _annotate_status(laws, version_db)
    return laws


def _structure(hits: list[dict]) -> list[dict]:
    """把 KB hits 解析成以條號為單位的清單。

    KB 回的是「段落」，一個段落可能提到多個條文，多個段落也可能提到同一條文，
    因此需要展開再依 citation 合併。
    """
    merged: dict[str, dict] = {}
    for hit in hits or []:
        if not isinstance(hit, dict):
            continue
        text = str(hit.get("text", ""))
        meta = hit.get("metadata") if isinstance(hit.get("metadata"), dict) else {}
        score = _as_float(hit.get("score"))
        source = hit.get("source", "")
        doc_type = str(meta.get("doc_type", "") or "")

        for parts in extract_parsed(text):
            key = parts["citation"]
            existing = merged.get(key)
            if existing is None:
                merged[key] = {
                    **parts,
                    "status": "unknown",
                    "doc_type": doc_type,
                    "score": score,
                    "reason": _summarize(text),
                    "source": source,
                }
            elif score > existing["score"]:
                # 同條文被更相關的段落命中 → 更新分數與推薦理由
                existing["score"] = score
                existing["reason"] = _summarize(text)
                if not existing.get("source"):
                    existing["source"] = source

    return sorted(merged.values(), key=lambda x: x["score"], reverse=True)


def _annotate_status(laws: list[dict], version_db) -> None:
    """標註修正狀態（§6.3）。version_db 未建時保持 unknown，不臆測。"""
    if version_db is None:
        return
    for law in laws:
        status = None
        if hasattr(version_db, "status_of"):
            status = version_db.status_of(law["citation"])
        elif isinstance(version_db, dict):
            entry = version_db.get(law["citation"])
            if isinstance(entry, dict):
                status = entry.get("status")
        if status:
            law["status"] = status


def _summarize(text: str, limit: int = 120) -> str:
    """取檢索段落前段當推薦理由，讓承辦人知道為何推薦這條。"""
    cleaned = " ".join((text or "").split())
    return cleaned[:limit] + ("…" if len(cleaned) > limit else "")


def _as_float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
