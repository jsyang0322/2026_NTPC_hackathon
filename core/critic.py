"""對抗式審查（§11.2）。[LLM]

以高階模型扮演行政法院法官挑草稿毛病：回應薄弱處、涵攝跳躍、未審酌之瑕疵。
輸出撤銷風險報告，與 §7 健檢合併顯示。成本 1–2 次呼叫。
"""

from __future__ import annotations

from .bedrock_client import BedrockClient, get_client, MODEL_WRITER


def adversarial_review(draft: dict, fields: dict, client: BedrockClient | None = None) -> dict:
    """回傳 {"attacks": [{"point": str, "severity": str}], "revocation_risk": str}。"""
    client = client or get_client()
    _ = client
    # TODO(§11.2): 以 MODEL_WRITER 扮演法官挑毛病
    return {"attacks": [], "revocation_risk": "unknown", "_stub": True}
