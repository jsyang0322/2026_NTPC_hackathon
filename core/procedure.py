"""閘門 1：程序審查（訴願法§77 各款，§6.2）。[純規則，不呼叫 LLM]

判斷是否不受理及對應款次；不受理者走 §10.4 快速通道，全程不呼叫 LLM。

保守原則：規則層只在「有明確欄位訊號」時判不受理，拿不準就放行進實體審查，
避免誤擋。需要語意判斷的款次（是否行政處分、利害關係）交由後段/人工，不在此硬判。
可靠的純規則判定為 §77(2) 逾期——只要有送達生效日與提起日即可算。
"""

from __future__ import annotations

from . import timeline as _tl


# §77 款次（可程式判定者）
CLAUSE_OVERDUE = "77-2"          # 逾法定期間
CLAUSE_NOT_ELIGIBLE = "77-3"     # 當事人不適格
CLAUSE_NO_DISPOSITION = "77-6"   # 原處分已不存在
CLAUSE_DUPLICATE = "77-7"        # 重行提起
CLAUSE_NOT_ADMIN_ACT = "77-8"    # 非行政處分


def procedure_check(timeline: dict, fields: dict) -> dict:
    """回傳程序審查結果 dict。

    輸出:
      is_inadmissible: bool
      clause: str | None   對應 §77 款次代碼（如 "77-2"）
      reason: str | None   人可讀理由
      computed_dates: dict 供追溯（送達生效日、訴願期間末日、逾期天數）
    """
    parsed = (timeline or {}).get("_parsed", {})
    service_eff = parsed.get("service_effective_date")
    filed = parsed.get("filed_date")
    deadline = parsed.get("appeal_deadline")

    computed = {
        "service_effective_date": _tl.format_roc(service_eff),
        "appeal_deadline": _tl.format_roc(deadline),
        "filed_date": _tl.format_roc(filed),
        "overdue_days": None,
    }

    # --- §77(2) 逾期：有提起日與期間末日才能判 ---
    if filed is not None and deadline is not None:
        if filed > deadline:
            overdue = (filed - deadline).days
            computed["overdue_days"] = overdue
            return {
                "is_inadmissible": True,
                "clause": CLAUSE_OVERDUE,
                "reason": (f"提起訴願日（{_tl.format_roc(filed)}）逾法定期間末日"
                           f"（{_tl.format_roc(deadline)}）{overdue} 日，"
                           f"依訴願法第77條第2款應不受理。"),
                "computed_dates": computed,
                "source": "rule",
            }

    # --- §77(6) 原處分已不存在：欄位有明確標記才判 ---
    od = fields.get("original_disposition", {}) or {}
    if _truthy(od.get("revoked")) or _truthy(fields.get("disposition_revoked")):
        return {
            "is_inadmissible": True,
            "clause": CLAUSE_NO_DISPOSITION,
            "reason": "原行政處分已經撤銷或失效，依訴願法第77條第6款應不受理。",
            "computed_dates": computed,
            "source": "rule",
        }

    # --- §77(7) 重行提起：欄位標記已就同一處分決定/撤回過 ---
    if _truthy(fields.get("previously_decided")):
        return {
            "is_inadmissible": True,
            "clause": CLAUSE_DUPLICATE,
            "reason": "就同一處分前已提起訴願並經決定或撤回，依訴願法第77條第7款應不受理。",
            "computed_dates": computed,
            "source": "rule",
        }

    # 其餘（適格、是否行政處分等）需語意判斷，規則層不硬判，放行進實體審查
    return {
        "is_inadmissible": False,
        "clause": None,
        "reason": None,
        "computed_dates": computed,
        "source": "rule",
    }


def _truthy(v) -> bool:
    """把可能是 bool / 字串 / None 的旗標統一判真。"""
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("true", "1", "yes", "是", "已撤銷", "已決定")
    return bool(v)
