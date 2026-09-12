"""閘門 1：程序審查（訴願法§77 各款，§6.2）。[純規則，不呼叫 LLM]

判斷是否不受理及對應款次；不受理者走 §10.4 快速通道，全程不呼叫 LLM。
"""

from __future__ import annotations


def procedure_check(timeline: dict, fields: dict) -> dict:
    """回傳 ProcedureResult（見 schemas.ProcedureResult）。

    檢查：逾期(§14)、補正、當事人適格、訴願能力、處分是否已不存在、重行提起等。
    """
    # TODO(§6.2): 依 timeline 計算訴願期間、補正期間，判定§77 各款
    return {
        "is_inadmissible": False,
        "clause": None,
        "reason": None,
        "computed_dates": {},
        "_stub": True,
    }
