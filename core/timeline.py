"""案件時間軸引擎（亮點 B，§6）。[純規則，不呼叫 LLM]

一條時間軸同時支援：程序期間、裁處權時效、行為時法比對、教示版本。
確定性計算全部在此，不消耗 Bedrock 配額。
"""

from __future__ import annotations


def build_timeline(fields: dict) -> dict:
    """由擷取欄位建立 CaseTimeline（見 schemas.CaseTimeline）。

    含寄存送達生效日推算（自寄存之日起經 10 日生效，訴願法§47 III）。
    """
    od = fields.get("original_disposition", {})
    return {
        "act_date": od.get("act_date"),
        "disposition_date": od.get("date"),
        "service_date": od.get("service_date"),
        "filed_date": fields.get("petition_filed_date"),
        "decision_date": None,
        "service_method": None,
        "prior_dispositions": od.get("prior_dispositions", []),
        "_stub": True,
    }


def compute_service_effective_date(service_date: str, method: str) -> str | None:
    """寄存送達自寄存之日起經 10 日生效；本人/補充送達為當日。"""
    # TODO(§6.2): 實作日期計算 + 例假日順延
    return None
