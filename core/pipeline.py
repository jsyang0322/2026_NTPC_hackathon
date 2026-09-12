"""單體主線 v1.2：接收前段（同學）輸出的 JSON → 路由 → KB 檢索 → 生成草稿 → 承辦人確認。

流程（v1.2 調整：先生成草稿，最後才確認）：
  前段 JSON → Router(route_key)
            → analyze_case（KB 檢索：法規 + 相似案例，產生建議主文）
            → generate_case_draft（用建議主文先生成完整草稿 + 驗證 + 對抗式審查）
            → ★承辦人審閱草稿並確認主文★
                 ├─ 維持建議主文 → 直接定稿（0 次額外呼叫）
                 └─ 改主文 → confirm_disposition 重生理由（+1 次，僅在改動時）

RAG 走 Bedrock KB（路線 A）。所有 LLM / KB 呼叫走 bedrock_client（≤1 RPS）。
單件呼叫預算：檢索 1 + 撰稿 1 + 對抗式審查 1–2 ≈ 3–4 次；改主文才 +1。
"""

from __future__ import annotations

import argparse
import json

from . import router, similar_cases, recommend_laws, draft, verify, critic
from .schemas import build_empty_input, validate_input
from .bedrock_client import get_client


def _query_text(payload: dict) -> str:
    """組 KB 檢索用的查詢字串：優先用原處分書全文 + 訴願主張。"""
    raw = payload.get("raw_text", {})
    fields = payload.get("extracted_fields", {})
    claims = " ".join(c.get("summary", "") for c in fields.get("claims", []))
    return (raw.get("original_disposition_doc", "") + "\n" + raw.get("petition", "") + "\n" + claims).strip()


def analyze_case(payload: dict, client=None) -> dict:
    """步驟一：路由 → KB 檢索（法規 + 相似案例）→ 產生建議主文。尚未撰稿。"""
    client = client or get_client()
    problems = validate_input(payload)
    if problems:
        return {"error": "介面不符契約", "problems": problems}

    route_key = router.resolve_route(payload)
    fields = payload.get("extracted_fields", {})
    q = _query_text(payload)

    laws = recommend_laws.recommend_laws(route_key, q, client=client)     # KB：法規/函釋/判解
    sims = similar_cases.find_similar(route_key, q, client=client)        # KB：歷史決定書共池

    return {
        "case_id": payload.get("case_id"),
        "route_key": route_key,
        "need_human_review": router.needs_human_review(payload),
        "fields": fields,
        "recommended_laws": laws,
        "similar_cases": sims,
        "defects": [],   # TODO: 接 core.defects.health_check（健檢那組）
        "suggested_disposition": _suggest_disposition(sims),
    }


def generate_case_draft(analysis: dict, disposition: str | None = None,
                        client=None, max_rewrites: int = 1) -> dict:
    """步驟二：對抗式審查迴圈——生成 → 規則檢查 → 法官審查 →（有問題）有限重寫。

    設計（§11.2）：
      1. 生成草稿（1 次呼叫）
      2. verify 規則檢查（0 次，純程式，先擋客觀問題）
      3. critic 法官對抗式審查（1 次呼叫）
      4. 若規則或法官指出重大問題 → 帶意見重寫（≤ max_rewrites 次，預設 1）
      5. 仍不過 → 標「建議人工複核」，仍輸出

    呼叫數：正常 2 次；最壞（重寫 1 次）4 次。守 §13.3 的單件 ≤5。
    回傳含 draft / verification / adversarial / used_disposition / rewrite_count /
    needs_human_review。
    """
    client = client or get_client()
    rk = analysis["route_key"]
    fields = analysis["fields"]
    laws = analysis.get("recommended_laws", [])
    sims = analysis.get("similar_cases", [])
    defects = analysis.get("defects", [])
    used = disposition or analysis.get("suggested_disposition", "")

    # 1) 第一版
    d = draft.generate_draft(rk, used, fields, laws, sims, defects, client=client)

    rewrite_count = 0
    while True:
        report = verify.verify_draft(d, fields, laws, {}, defects)      # 規則層（0 次）
        review = critic.adversarial_review(d, fields, client=client)     # 法官（1 次）

        blocking = critic.has_blocking_issue(review, report.get("issues", []))
        if not blocking or rewrite_count >= max_rewrites:
            break

        # 2) 帶意見重寫（1 次）
        feedback = critic.collect_feedback(review, report.get("issues", []))
        d = draft.generate_draft(rk, used, fields, laws, sims, defects,
                                 client=client, prev_draft=d, feedback=feedback)
        rewrite_count += 1

    needs_human = critic.has_blocking_issue(review, report.get("issues", []))
    return {
        "draft": d, "verification": report, "adversarial": review,
        "used_disposition": used, "rewrite_count": rewrite_count,
        "needs_human_review": needs_human,
    }


def confirm_disposition(analysis: dict, draft_result: dict, confirmed_disposition: str, client=None) -> dict:
    """步驟三：承辦人確認主文。

    - 若確認的主文與生成時用的一致 → 直接定稿，不重生（0 次額外呼叫）。
    - 若承辦人改了主文 → 重生理由，確保主文與理由一致（+1 次呼叫）。
    """
    client = client or get_client()
    used = draft_result.get("used_disposition", "")
    if confirmed_disposition == used:
        return {**draft_result, "confirmed_disposition": confirmed_disposition, "regenerated": False}
    # 主文被改 → 重生（避免主文理由矛盾）
    regen = generate_case_draft(analysis, disposition=confirmed_disposition, client=client)
    return {**regen, "confirmed_disposition": confirmed_disposition, "regenerated": True}


def process_case(payload: dict, confirmed_disposition: str | None = None, client=None) -> dict:
    """端到端便利函式（測試用）：分析 → 生成草稿 →（可選）確認主文。"""
    client = client or get_client()
    analysis = analyze_case(payload, client=client)
    if analysis.get("error"):
        return analysis
    draft_result = generate_case_draft(analysis, client=client)
    result = {**analysis, **draft_result}
    if confirmed_disposition is not None:
        result.update(confirm_disposition(analysis, draft_result, confirmed_disposition, client=client))
    return result


def _suggest_disposition(sims: list[dict]) -> str:
    """依相似案例主文分布建議主文（僅建議，人保留決定權）。骨架階段先回預設。"""
    # TODO: 依 sims 的 disposition 分布與 §7 健檢紅燈調整建議
    return "訴願駁回（建議，待承辦人確認）"


def _demo_payload() -> dict:
    """符合 v1.1 交接契約的骨架測試輸入（洗錢案）。"""
    p = build_empty_input()
    p["case_id"] = "sim-114-001"
    p["case_type"] = {"label": "違反洗錢防制法事件", "route_key": "money_laundering",
                      "confidence": 0.92, "need_human_review": False}
    p["extracted_fields"]["claims"] = [{"id": "C1", "summary": "原處分認定事實有誤", "quote": ""}]
    p["raw_text"]["petition"] = "訴願人主張原處分認定事實有誤，請求撤銷。（骨架測試假資料）"
    p["raw_text"]["original_disposition_doc"] = "原處分書：因違反洗錢防制法裁處。（骨架測試假資料）"
    return p


def main() -> None:
    parser = argparse.ArgumentParser(description="ntpc-appeal-ai 單體主線 v1.2")
    parser.add_argument("--dry-run", action="store_true", help="不呼叫真實 Bedrock/KB")
    args = parser.parse_args()
    client = get_client(dry_run=args.dry_run)
    result = process_case(_demo_payload(), client=client)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
