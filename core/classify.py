"""案由分類（§5.3）。[規則]

分類目標為「案由」（廢棄物/建築/噪音/空污/洗錢/其他）。

本專案的案由判定以前段（henry 工作流）的模型分類為主力；本模組提供**離線關鍵字
分類**，供兩種用途：
  1. henry 判為「其餘案件」時，於後段再細分建築/噪音等（0 次呼叫）。
  2. 未接前段時的獨立後備判定。

以案由代表關鍵字比對，命中最多者勝；未命中歸「其他」。不呼叫 LLM、不吃配額。
"""

from __future__ import annotations

CASE_TYPES = ["廢棄物清理法", "建築法", "噪音管制法", "空氣污染防制法", "洗錢防制法", "其他"]

# 各案由的判別關鍵字（法規全名權重最高，另含常見事由用語）。
_KEYWORDS: dict[str, list[str]] = {
    "廢棄物清理法": ["廢棄物清理法", "廢棄物", "棄置", "菸蒂", "清除處理", "環稽字"],
    "建築法": ["建築法", "違章建築", "違規使用", "公安申報", "使用執照", "建管"],
    "噪音管制法": ["噪音管制法", "噪音", "音量", "分貝", "陳情噪音"],
    "空氣污染防制法": ["空氣污染防制法", "空氣污染", "空污", "固定污染源", "排放標準", "粒狀污染物"],
    "洗錢防制法": ["洗錢防制法", "洗錢", "人頭帳戶", "交付帳戶", "書面告誡", "警示帳戶"],
}


def classify_case_type(case_text: dict, model=None) -> dict:
    """回傳 {"case_type": str, "confidence": float}。

    以關鍵字命中數決定案由；命中法規全名者額外加權。confidence 為相對命中強度
    （0.0–1.0），供上層判斷是否需人工確認。
    """
    text = " ".join(str(v) for v in case_text.values())

    best_type = "其他"
    best_score = 0
    for case_type, keywords in _KEYWORDS.items():
        hits = [kw for kw in keywords if kw in text]
        # 法規全名（清單首項）命中權重最高
        score = len(hits) + (3 if keywords[0] in text else 0)
        if score > best_score:
            best_score, best_type = score, case_type

    # confidence：命中越多越高，封頂 1.0；未命中為 0
    confidence = 0.0 if best_score == 0 else min(1.0, round(best_score / 5.0, 2))
    return {"case_type": best_type, "confidence": confidence}
