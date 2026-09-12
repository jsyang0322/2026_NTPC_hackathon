"""F1 案由分類（§5.3）。[規則 / 模型]

分類目標為「案由」（廢棄物/建築/噪音/空污/洗錢/其他），非「程序 vs 實體」。
主路線：字元 n-gram TF-IDF + LogisticRegression（SageMaker Notebook 訓練，模型檔存 S3）。
對照組：Bedrock 輕量模型 few-shot。此處不呼叫 LLM，載入已訓練模型即可。
"""

from __future__ import annotations

CASE_TYPES = ["廢棄物清理法", "建築法", "噪音管制法", "空氣污染防制法", "洗錢防制法", "其他"]


def classify_case_type(case_text: dict, model=None) -> dict:
    """回傳 {"case_type": str, "confidence": float}。

    model: 已載入的 sklearn pipeline；None 時走關鍵字後備判斷。
    """
    # TODO(§5.3): model.predict_proba；低於門檻標「建議人工確認」
    text = " ".join(str(v) for v in case_text.values())
    for ct in CASE_TYPES[:-1]:
        if ct[:3] in text:
            return {"case_type": ct, "confidence": 0.0, "_stub": True}
    return {"case_type": "其他", "confidence": 0.0, "_stub": True}
