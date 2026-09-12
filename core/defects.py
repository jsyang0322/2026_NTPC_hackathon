"""閘門 2：原處分健檢 / 撤銷風險雷達（亮點 A，§7）。[規則 + LLM]

D1–D6 純 Python 規則（離線可跑，不吃配額）；
D7–D13 合併為「一次」LLM 呼叫輸出 JSON 陣列（§7.5），由人判斷。
"""

from __future__ import annotations

from .bedrock_client import BedrockClient, get_client, MODEL_WRITER


# --- 規則項（D1–D6）：純 Python ---
def rule_d1(fields, timeline):  # 應記載事項是否齊全（行政程序法§96、§114）
    return None

def rule_d2(fields, timeline):  # 裁處權時效（行政罰法§27，超過 3 年）
    return None

def rule_d3(fields, timeline):  # 累犯次數與罰鍰金額
    return None

def rule_d4(fields, timeline):  # 行為時法與裁處時法（行政罰法§5）
    return None

def rule_d5(fields, timeline):  # 前置程序是否完成
    return None

def rule_d6(fields, timeline):  # 送達是否合法（行政程序法§72–74）
    return None


RULE_CHECKS = [rule_d1, rule_d2, rule_d3, rule_d4, rule_d5, rule_d6]


def health_check(fields: dict, timeline: dict, client: BedrockClient | None = None) -> list[dict]:
    """回傳 CheckResult 清單（見 schemas.CheckResult）。

    先跑規則項，再以單次 LLM 呼叫處理 D7–D13。紅燈優先排序（§7.7）。
    """
    client = client or get_client()
    results: list[dict] = []
    for check in RULE_CHECKS:
        r = check(fields, timeline)
        if r:
            results.append(r)
    # TODO(§7.4/§7.5): 合併 D7–D13 為一次 client.converse 呼叫
    _ = client
    return results  # 骨架階段回空清單
