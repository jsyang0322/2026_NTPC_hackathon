"""閘門 2：原處分健檢 / 撤銷風險雷達（亮點 A，§7）。[規則 + LLM]

D1–D6 純 Python 規則（離線可跑，不吃配額）；
D7–D13 合併為「一次」LLM 呼叫輸出 JSON 陣列（§7.5），由人判斷。

給燈原則（與 verify 一致）：red = 可程式確認的客觀重大瑕疵（撤銷事由）；
amber = 提示需注意，但不足以斷定。保守：拿不準給 amber 或不給，避免假紅燈。
red 會被 pipeline._decide_disposition 採為「原處分撤銷」事由。
"""

from __future__ import annotations

from .bedrock_client import BedrockClient, get_client, MODEL_WRITER
from .schemas import CheckResult
from . import timeline as _tl


def _cr(id_, level, title, basis="", evidence="", suggested_effect="") -> dict:
    return CheckResult(id=id_, level=level, title=title, basis=basis,
                       evidence=evidence, suggested_effect=suggested_effect).as_dict()


# --- 規則項（D1–D6）：純 Python ---

def rule_d1(fields, timeline):
    """應記載事項是否齊全（行政程序法§96）。缺核心事項為撤銷風險。"""
    od = fields.get("original_disposition", {}) or {}
    required = {
        "agency": "處分機關",
        "date": "處分作成日期",
        "doc_no": "處分書字號",
        "legal_basis": "法令依據",
    }
    missing = [label for key, label in required.items() if not od.get(key)]
    if not missing:
        return None
    # 缺法令依據屬重大（無從審查裁罰依據）；其餘缺漏合併提示
    level = "red" if "法令依據" in missing else "amber"
    return _cr("D1", level, "原處分應記載事項不完備",
               basis="行政程序法第96條第1項",
               evidence="缺漏：" + "、".join(missing),
               suggested_effect="原處分撤銷，命補正應記載事項" if level == "red" else "")


def rule_d2(fields, timeline):
    """裁處權時效（行政罰法§27，自行為終了起 3 年）。逾期裁處為撤銷事由。"""
    parsed = (timeline or {}).get("_parsed", {})
    disposition = parsed.get("disposition_date")
    limit = parsed.get("penalty_limitation_date")
    if disposition is None or limit is None:
        return None
    if disposition > limit:
        days = (disposition - limit).days
        return _cr("D2", "red", "裁處已逾裁處權時效",
                   basis="行政罰法第27條第1項（3年）",
                   evidence=(f"處分作成日 {_tl.format_roc(disposition)} 逾時效末日 "
                             f"{_tl.format_roc(limit)} 共 {days} 日"),
                   suggested_effect="原處分撤銷（裁處權已消滅）")
    return None


def rule_d3(fields, timeline):
    """累犯次數與罰鍰金額一致性。宣稱累犯次數與金額級距明顯不符時提示。"""
    od = fields.get("original_disposition", {}) or {}
    amount = od.get("penalty_amount")
    times = od.get("violation_count") or od.get("repeat_count")
    if not amount or not times:
        return None
    try:
        amount = float(amount); times = int(times)
    except (TypeError, ValueError):
        return None
    if times <= 1:
        return None
    # 僅作提示：無法規費率表時不硬判金額對錯
    return _cr("D3", "amber", "累犯次數與罰鍰金額宜複核",
               basis="裁罰基準表",
               evidence=f"宣稱累犯 {times} 次、罰鍰 {amount:g}，請對照裁罰基準確認級距")


def rule_d4(fields, timeline):
    """行為時法與裁處時法（行政罰法§5 從新從輕）。跨法規修正時提示複核。"""
    parsed = (timeline or {}).get("_parsed", {})
    act = parsed.get("act_date")
    disposition = parsed.get("disposition_date")
    if act is None or disposition is None:
        return None
    # 行為與裁處跨年度且明顯久遠時，提示須做從新從輕比較
    if disposition.year - act.year >= 1:
        return _cr("D4", "amber", "應審酌從新從輕原則",
                   basis="行政罰法第5條",
                   evidence=(f"行為時 {_tl.format_roc(act)} 與裁處時 "
                             f"{_tl.format_roc(disposition)} 跨越法規可能修正之期間，"
                             "應比較行為時法與裁處時法擇有利者"))
    return None


def rule_d5(fields, timeline):
    """前置程序是否完成（如限期改善、令停工等先行處分）。缺前置為程序瑕疵。"""
    od = fields.get("original_disposition", {}) or {}
    requires_prior = od.get("requires_prior_order")
    prior_done = od.get("prior_order_done")
    if requires_prior and not prior_done:
        return _cr("D5", "red", "未完成法定前置程序即為裁處",
                   basis="各該作用法之前置程序規定",
                   evidence="本案需先行限期改善／令停工等前置處分，卷內查無完成紀錄",
                   suggested_effect="原處分撤銷，補正前置程序")
    return None


def rule_d6(fields, timeline):
    """送達是否合法（行政程序法§72–74、訴願法§47）。寄存送達缺日期無從起算。"""
    parsed = (timeline or {}).get("_parsed", {})
    method = (timeline or {}).get("service_method")
    service = parsed.get("service_date")
    if service is None:
        # 完全無送達日：無從起算救濟期間，屬程序瑕疵
        return _cr("D6", "amber", "原處分送達日期不明",
                   basis="行政程序法第72條以下",
                   evidence="卷內查無合法送達日期，救濟期間無從起算")
    return None


RULE_CHECKS = [rule_d1, rule_d2, rule_d3, rule_d4, rule_d5, rule_d6]

_LEVEL_ORDER = {"red": 0, "amber": 1, "green": 2}


def health_check(fields: dict, timeline: dict, client: BedrockClient | None = None) -> list[dict]:
    """回傳 CheckResult 清單（見 schemas.CheckResult）。

    健檢以純規則項（D1–D6，0 次呼叫）為準，紅燈優先排序（§7.7）。
    刻意不呼叫 LLM 做健檢：健檢結論須客觀可追溯，能用規則判定就不交給會幻覺的模型，
    這也讓每件的呼叫預算穩定守在檢索1+撰稿1+法官1。client 參數保留供外殼統一傳遞。
    """
    client = client or get_client()
    results: list[dict] = []
    for check in RULE_CHECKS:
        r = check(fields, timeline)
        if r:
            results.append(r)
    _ = client
    results.sort(key=lambda c: _LEVEL_ORDER.get(c.get("level", "green"), 3))
    return results
