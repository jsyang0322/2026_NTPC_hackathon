"""共用資料結構與交接契約（v1.1）。

本模組（草稿撰寫與檢查）接收「同學（前段）」輸出的 JSON。契約見 INPUT_SCHEMA_VERSION
與 build_empty_input()。core 各函式以 dict 傳遞，不綁執行環境。
對應草稿模組定案說明 §7 的 JSON 介面。
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict


INPUT_SCHEMA_VERSION = "1.0"

# route_key 英文枚舉（§7 交接約定）：中文名稱不當路由鍵，避免汙/污、防制/管制不一致
ROUTE_KEYS = ("money_laundering", "waste", "air_pollution", "building", "noise", "general")


# ---------- 前段交接輸入（§7）----------
def build_empty_input() -> dict:
    """回傳符合交接契約的空白輸入，供測試與介面預填。key 不可省略。"""
    return {
        "schema_version": INPUT_SCHEMA_VERSION,
        "case_id": "",
        "admissibility": {"is_admissible": True, "note": ""},
        "case_type": {
            "label": "",
            "route_key": "general",
            "confidence": 0.0,
            "need_human_review": False,
        },
        "extracted_fields": {
            "petitioner": {"name": "", "address": "", "agent": ""},
            "original_disposition": {
                "agency": "", "date": "", "doc_no": "",
                "legal_basis": [], "penalty_amount": 0,
                "service_date": "", "act_date": "",
            },
            "petition_filed_date": "",
            "requests": [],
            "claims": [],                 # [{id, summary, quote}]
            "agency_reply": {"summary": "", "evidence_attached": False},
            "evidence_list": [],
        },
        "raw_text": {
            "petition": "",               # 訴願書全文（供 prompt）
            "original_disposition_doc": "",  # 原處分書全文（供 KB 檢索/對抗式審查）
            "agency_reply_doc": "",
        },
    }


def validate_input(payload: dict) -> list[str]:
    """檢查交接輸入是否符合契約，回傳問題清單（空清單=通過）。"""
    problems: list[str] = []
    if payload.get("schema_version") != INPUT_SCHEMA_VERSION:
        problems.append(f"schema_version 應為 {INPUT_SCHEMA_VERSION}")
    ct = payload.get("case_type", {})
    rk = ct.get("route_key")
    if rk not in ROUTE_KEYS:
        problems.append(f"route_key '{rk}' 不在枚舉 {ROUTE_KEYS}")
    for key in ("admissibility", "extracted_fields", "raw_text"):
        if key not in payload:
            problems.append(f"缺少必填欄位 {key}")
    return problems


# ---------- 健檢單項（§6 / 說明書 §7.5）----------
@dataclass
class CheckResult:
    id: str
    level: str                             # "red" | "amber" | "green"
    title: str
    basis: str = ""
    evidence: str = ""
    precedent: str = ""
    suggested_effect: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


# ---------- 相似案例（§9）----------
@dataclass
class SimilarCase:
    doc_id: str
    year: int | None = None
    similarity: float = 0.0
    disposition: str = ""
    shared_issues: list[str] = field(default_factory=list)
    reasoning_summary: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


# ---------- 草稿段落（§10.5）----------
@dataclass
class DraftParagraph:
    text: str
    responds_to: list[str] = field(default_factory=list)
    cites: list[str] = field(default_factory=list)
    based_on_case: str = ""
    source: str = "llm"                    # llm / template / rule

    def as_dict(self) -> dict:
        return asdict(self)
