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

import re

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

    # 分層檢索（解法A）：法條與函釋判決分兩批撈。
    # 原因：單批檢索時，判決/函釋段落的語意常壓過法條本文，把 doc_type=law 擠出
    # num_results 之外，導致「可引用法條白名單」為空。分層可保證白名單一定有法條。
    # 兩次檢索各過 throttle（kb.retrieve 內建），呼叫預算 +1。
    law_hits = kb.retrieve(
        query_text=query_text, route_key=route_key,
        doc_types=["law"],            # 含共通法規（include_common_law 會併入 common_law）
        include_common_law=True,
        num_results=num_results, client=client, filter_case_type=False,
    )
    ref_hits = kb.retrieve(
        query_text=query_text, route_key=route_key,
        doc_types=["interpretation", "judgment"],
        include_common_law=False,     # 參考資料層不再併共通法規
        num_results=max(num_results // 2, 2), client=client, filter_case_type=False,
    )

    laws = _structure(law_hits + ref_hits)
    _annotate_status(laws, version_db)
    return laws


# 純條號正則（不含法名）：搭配 metadata.law_name 組成乾淨 citation。
_ARTICLE_RE = re.compile(r"第\s*(\d+)\s*條(?:\s*之\s*(\d+))?")

# 法名可靠、適合組「法名第N條」的 doc_type（法規本文）。
# interpretation/judgment 的 law_name 是描述性長字串（函釋/判決名），不組條號。
_LAW_DOC_TYPES = frozenset({"law", "common_law"})


def _structure(hits: list[dict]) -> list[dict]:
    """把 KB hits 解析成以條號為單位的清單。

    法名來源（方向2，治本）：doc_type=law/common_law 者用 metadata.law_name（乾淨），
    條號從內文抽數字組成「法名第N條」，避免從破碎的 PDF 內文切法名產生髒資料
    （如「棄物清理法」「理法」）。函釋/判決不組條號，以摘要形式列入供參考。
    """
    merged: dict[str, dict] = {}

    def _upsert(key, parts, doc_type, score, text, source):
        existing = merged.get(key)
        if existing is None:
            merged[key] = {
                **parts, "status": "unknown", "doc_type": doc_type,
                "score": score, "reason": _summarize(text), "source": source,
            }
        elif score > existing["score"]:
            existing["score"] = score
            existing["reason"] = _summarize(text)
            if not existing.get("source"):
                existing["source"] = source

    for hit in hits or []:
        if not isinstance(hit, dict):
            continue
        text = str(hit.get("text", ""))
        meta = hit.get("metadata") if isinstance(hit.get("metadata"), dict) else {}
        score = _as_float(hit.get("score"))
        source = hit.get("source", "")
        doc_type = str(meta.get("doc_type", "") or "")
        law_name = str(meta.get("law_name", "") or "").strip()

        if doc_type in _LAW_DOC_TYPES and law_name:
            # 法名用 metadata（乾淨），條號從內文抽數字
            for m in _ARTICLE_RE.finditer(text):
                art = int(m.group(1))
                sub = m.group(2)
                citation = f"{law_name}第{art}條" + (f"之{int(sub)}" if sub else "")
                parts = {
                    "citation": citation, "law": law_name,
                    "article": str(art), "sub_article": str(int(sub)) if sub else None,
                }
                _upsert(citation, parts, doc_type, score, text, source)
        elif doc_type in _LAW_DOC_TYPES:
            # 法規段落但無 law_name（不預期）：退回內文法名解析
            for parts in extract_parsed(text):
                _upsert(parts["citation"], parts, doc_type, score, text, source)
        else:
            # 函釋/判決：不組條號，以其名稱為 key 列入供參考
            name = law_name or _doc_name_from_source(source)
            if name:
                _upsert(name, {"citation": name, "law": name,
                               "article": None, "sub_article": None},
                        doc_type, score, text, source)

    return sorted(merged.values(), key=lambda x: x["score"], reverse=True)


def _doc_name_from_source(source: str) -> str:
    return source.rstrip("/").split("/")[-1] if source else ""


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
