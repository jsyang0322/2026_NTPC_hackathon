"""草稿自動驗證（§11.1）。[純規則，不呼叫 LLM]

把「低幻覺」變成可檢查的結果：主張回應覆蓋率、引用集合比對、
日期重算、教示一致、主文理由一致、健檢紅燈是否已處理。
"""

from __future__ import annotations


def verify_draft(draft: dict, fields: dict, recommended_laws: list[dict],
                 timeline: dict, defects: list[dict]) -> dict:
    """回傳驗證報告 {"checks": [{name, passed, detail}], "all_passed": bool}。"""
    checks: list[dict] = []
    # TODO(§11.1): 逐項比對
    return {"checks": checks, "all_passed": True, "_stub": True}
