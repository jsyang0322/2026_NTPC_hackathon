"""對抗式審查（§11.2）。[LLM]

對抗式審查迴圈的「第二層」：以高階模型扮演行政法院法官，攻擊草稿的論理弱點。
規則層（verify）擋不到的「涵攝跳躍、論理不足、漏審瑕疵」才交給法官。
輸出 passed（是否足以通過）+ attacks（攻擊點清單）+ severity，供迴圈判斷是否重寫。

成本：每次 1 次呼叫。迴圈次數由 pipeline 控制（重寫上限 1 次），守 §13.3 的 ≤5。
"""

from __future__ import annotations

import json

from .bedrock_client import BedrockClient, get_client, MODEL_WRITER


JUDGE_SYSTEM = (
    "你是資深行政法院法官，正在審查一份新北市政府訴願決定書草稿，"
    "目的是找出「若此案進入行政訴訟，本院可能據以撤銷訴願決定的瑕疵」。"
    "請嚴格、務實，只挑真正會影響裁判結果的重大問題，不挑文字風格。"
)


def _build_prompt(draft: dict, fields: dict) -> str:
    claims = fields.get("claims", [])
    reasons = draft.get("reasons", [])
    return (
        f"【主文】{draft.get('main','')}\n"
        f"【訴願人主張】{json.dumps(claims, ensure_ascii=False)}\n"
        f"【理由草稿】{json.dumps(reasons, ensure_ascii=False)}\n\n"
        "請就下列面向挑毛病：\n"
        "1. 是否每一項訴願人主張都得到實質回應（非僅形式帶過）。\n"
        "2. 涵攝是否跳躍：事實到結論之間的論理有無斷層。\n"
        "3. 是否漏審原處分本身的瑕疵（如應記載事項、裁處權時效、證據不足）。\n"
        "4. 引用之法條、函釋、判決是否與論點相符、有無可疑或不存在者。\n"
        "5. 主文與理由結論是否一致。\n\n"
        "輸出 JSON（僅輸出 JSON，不要多餘文字）：\n"
        '{"passed": true/false, '
        '"attacks": [{"point": "問題描述", "severity": "high/medium/low", "fix": "建議修正方向"}], '
        '"revocation_risk": "high/medium/low"}\n'
        "判定規則：只要存在任一 severity=high 的問題，passed 必須為 false。"
    )


def adversarial_review(draft: dict, fields: dict, client: BedrockClient | None = None) -> dict:
    """法官對抗式審查。回傳 {passed, attacks, revocation_risk}。"""
    client = client or get_client()
    prompt = _build_prompt(draft, fields)
    text = client.converse(
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        model_id=MODEL_WRITER,
        system=JUDGE_SYSTEM,
        temperature=0.1,
    )
    return _parse(text)


def _parse(text: str) -> dict:
    """解析法官回應；容錯 ```json 圍籬與非 JSON（dry_run）。"""
    import re
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    raw = m.group(1) if m else text.strip()
    try:
        data = json.loads(raw)
        return {
            "passed": bool(data.get("passed", True)),
            "attacks": data.get("attacks", []),
            "revocation_risk": data.get("revocation_risk", "unknown"),
        }
    except (json.JSONDecodeError, AttributeError):
        # dry_run 或非 JSON：預設通過，不擋流程
        return {"passed": True, "attacks": [], "revocation_risk": "unknown", "_raw": text[:200]}


def has_blocking_issue(review: dict, verify_issues: list[str]) -> bool:
    """判斷是否需要重寫：法官未通過，或規則層有問題。"""
    if verify_issues:
        return True
    if not review.get("passed", True):
        return True
    return any(a.get("severity") == "high" for a in review.get("attacks", []))


def collect_feedback(review: dict, verify_issues: list[str]) -> str:
    """把規則問題 + 法官攻擊點，整理成給重寫用的意見。"""
    lines: list[str] = []
    lines.extend(verify_issues)
    for a in review.get("attacks", []):
        if a.get("severity") in ("high", "medium"):
            lines.append(f"[{a.get('severity')}] {a.get('point','')} — 修正方向：{a.get('fix','')}")
    return "\n".join(f"- {x}" for x in lines) if lines else ""
