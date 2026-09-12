"""草稿自動驗證（§11.1）。[純規則，不呼叫 LLM]

對抗式審查迴圈的「第一層」：客觀、可程式判定的問題先用規則擋，不花 Bedrock 配額。
把「低幻覺」變成可檢查的結果：主張回應覆蓋、引用集合比對、主文理由一致。
"""

from __future__ import annotations

import re


def verify_draft(draft: dict, fields: dict, recommended_laws: list[dict],
                 timeline: dict, defects: list[dict]) -> dict:
    """回傳 {"checks": [...], "all_passed": bool, "issues": [...]}。

    issues 是給重寫迴圈用的「規則層問題清單」（人看得懂、也能塞回重寫 prompt）。
    """
    checks: list[dict] = []
    issues: list[str] = []
    reasons = draft.get("reasons", [])

    # 1) 每項訴願主張皆有回應（比對 claims 的 id 與 reasons 的 responds_to）
    claim_ids = {c.get("id") for c in fields.get("claims", []) if c.get("id")}
    responded = set()
    for p in reasons:
        responded.update(p.get("responds_to", []))
    missing = claim_ids - responded
    ok = not missing
    checks.append({"name": "主張回應覆蓋", "passed": ok,
                   "detail": "全部回應" if ok else f"未回應：{sorted(missing)}"})
    if not ok:
        issues.append(f"下列訴願主張未在理由中回應：{sorted(missing)}，請補上對應段落並標注編號。")

    # 2) 引用法條都在推薦清單內（防字號捏造）
    allowed = _allowed_citations(recommended_laws)
    cited = set()
    for p in reasons:
        cited.update(p.get("cites", []))
    # 推薦清單為空（KB 未接）時跳過此檢查，避免誤報
    if allowed:
        illegal = {c for c in cited if c not in allowed}
        ok = not illegal
        checks.append({"name": "引用在推薦清單內", "passed": ok,
                       "detail": "全部合法" if ok else f"清單外引用：{sorted(illegal)}"})
        if not ok:
            issues.append(f"下列引用不在可引用清單內，疑似捏造：{sorted(illegal)}，僅能引用推薦法條。")
    else:
        checks.append({"name": "引用在推薦清單內", "passed": True, "detail": "推薦清單為空，略過"})

    # 3) 主文與理由結論一致（撤銷/駁回/不受理 的方向一致性）
    ok, detail = _main_reason_consistent(draft)
    checks.append({"name": "主文與理由一致", "passed": ok, "detail": detail})
    if not ok:
        issues.append(f"主文與理由方向可能矛盾：{detail}，請調整理由結論以呼應主文。")

    # 4) 條號書寫格式（含「之2」「第15條之2」）
    ok, bad = _citation_format_ok(reasons)
    checks.append({"name": "條號書寫格式", "passed": ok,
                   "detail": "正常" if ok else f"可疑條號：{bad}"})

    return {"checks": checks, "all_passed": all(c["passed"] for c in checks), "issues": issues}


def _allowed_citations(recommended_laws: list[dict]) -> set[str]:
    """把推薦法條整理成可比對的引用字串集合。"""
    allowed: set[str] = set()
    for law in recommended_laws:
        # 支援兩種形狀：{"law":..,"article":..} 或 KB 回傳的 {"text":..}
        if law.get("law") and law.get("article"):
            allowed.add(f"{law['law']}§{law['article']}")
        if law.get("citation"):
            allowed.add(law["citation"])
    return allowed


def _main_reason_consistent(draft: dict) -> tuple[bool, str]:
    main = draft.get("main", "")
    concl = " ".join(p.get("text", "") for p in draft.get("reasons", [])[-2:])  # 末段通常是結論
    if not concl:
        return True, "無理由段落可比對"
    撤銷 = ("撤銷" in main)
    駁回 = ("駁回" in main)
    if 撤銷 and "駁回" in concl and "撤銷" not in concl:
        return False, "主文為撤銷，理由結論卻傾向駁回"
    if 駁回 and "撤銷" in concl and "應予撤銷" in concl:
        return False, "主文為駁回，理由結論卻傾向撤銷"
    return True, "方向一致"


def _citation_format_ok(reasons: list[dict]) -> tuple[bool, list[str]]:
    """抓明顯錯誤的條號寫法，例如「第條」「第 條之」等殘缺。"""
    bad: list[str] = []
    for p in reasons:
        for m in re.finditer(r"第\s*條|第\d+條之(?!\d)", p.get("text", "")):
            bad.append(m.group(0))
    return (not bad), bad
