"""F1 爭點標註（§5.4）。[LLM]

以種子爭點表（取自資料集函釋與判解主題）+ LLM 標註。不使用分群（資料量不足）。
候選池 = 案由專屬爭點（draft.CASE_PROFILES[route_key].common_issues）+ 共通爭點。
LLM 只能從候選池挑選（可多標、可標「其他」），確保爭點可追溯、不杜撰。

程序不受理案件的爭點直接對應訴願法§77 各款（由 procedure 規則決定），不需本模組。
"""

from __future__ import annotations

import json
import re

from .bedrock_client import BedrockClient, get_client, MODEL_LIGHT
from .draft import get_profile

# 共通爭點（跨案由適用；案由專屬爭點見 draft.CASE_PROFILES[route_key].common_issues）
COMMON_ISSUES = [
    "從新從輕(行政罰法§5)",
    "主觀故意過失(行政罰法§7)",
    "權利保護必要",
    "行政程序再開",
    "比例原則",
    "正當法律程序(陳述意見機會)",
]

# 向後相容：保留舊常數名（部分模組/測試可能 import）
SEED_ISSUES = {
    "廢棄物清理法": ["共有人連帶責任", "行為責任與狀態責任", "清除義務主體"],
    "建築法": ["停歇業公安申報", "場所區隔方式", "簽證內容不實"],
    "洗錢防制法": ["交付帳戶有無正當理由", "主觀故意過失(行政罰法§7)"],
    "共通": COMMON_ISSUES,
}

_OTHER = "其他"

_SYSTEM = (
    "你是行政法訴願案件的爭點標註助理。針對每項訴願人主張，"
    "從提供的『候選爭點清單』中選出最貼切的爭點（可多選）。"
    "只能從清單挑選，清單沒有貼切者才標「其他」。不得自行發明清單外的爭點名稱。"
    "只輸出 JSON，不要多餘文字。"
)


def _candidate_pool(route_key: str | None) -> list[str]:
    """組候選爭點池：案由專屬 + 共通，去重保序。"""
    profile = get_profile(route_key)
    pool: list[str] = []
    for x in list(profile.get("common_issues", [])) + COMMON_ISSUES + [_OTHER]:
        if x not in pool:
            pool.append(x)
    return pool


def _build_prompt(claims: list[dict], pool: list[str]) -> str:
    claim_lines = "\n".join(
        f'{c.get("id", f"C{i+1}")}: {c.get("summary", "")}'
        for i, c in enumerate(claims)
    )
    pool_lines = "\n".join(f"- {p}" for p in pool)
    return (
        "【候選爭點清單】\n" + pool_lines + "\n\n"
        "【訴願人各項主張】\n" + claim_lines + "\n\n"
        "請為每項主張標註所屬爭點（可多選），輸出 JSON：\n"
        '{"issues": [{"claim_id": "C1", "tags": ["爭點1", "爭點2"]}]}\n'
        "規則：tags 內容必須完全複製自候選清單文字；無貼切者用 [\"其他\"]。"
    )


def _parse(text: str) -> dict:
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    raw = m.group(1) if m else text.strip()
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, AttributeError):
        return {}


def tag_issues(fields: dict, route_key: str | None = None,
               client: BedrockClient | None = None) -> dict:
    """對每項主張標註所屬爭點（多標籤，限候選池內）。

    輸入 fields.claims: [{"id","summary","quote"}]
    輸出: {"issues": [{"claim_id": "C1", "tags": [...]}], "candidate_pool": [...]}
    無 claims 時回空 issues（0 呼叫）。
    """
    client = client or get_client()
    claims = fields.get("claims", []) or []
    pool = _candidate_pool(route_key)

    if not claims:
        return {"issues": [], "candidate_pool": pool}

    prompt = _build_prompt(claims, pool)
    text = client.converse(
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        model_id=MODEL_LIGHT,
        system=_SYSTEM,
        temperature=0.0,
    )
    data = _parse(text)

    # 正規化並過濾：只保留候選池內的標籤，確保可追溯、不杜撰
    pool_set = set(pool)
    valid = {c.get("id", f"C{i+1}") for i, c in enumerate(claims)}
    issues: list[dict] = []
    for item in data.get("issues", []) if isinstance(data, dict) else []:
        cid = item.get("claim_id")
        if cid not in valid:
            continue
        tags = [t for t in item.get("tags", []) if t in pool_set]
        issues.append({"claim_id": cid, "tags": tags or [_OTHER]})

    # 補齊：LLM 漏標的 claim 給空池標記，確保每項主張都有對應（供下游對照）
    tagged = {it["claim_id"] for it in issues}
    for i, c in enumerate(claims):
        cid = c.get("id", f"C{i+1}")
        if cid not in tagged:
            issues.append({"claim_id": cid, "tags": [_OTHER]})

    # 依 claim 原順序排序
    order = {c.get("id", f"C{i+1}"): i for i, c in enumerate(claims)}
    issues.sort(key=lambda it: order.get(it["claim_id"], 999))
    return {"issues": issues, "candidate_pool": pool}
