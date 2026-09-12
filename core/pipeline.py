"""單體主線 v1.1：接收前段（同學）輸出的 JSON，路由 → KB 檢索 → 撰稿 → 檢查。

流程（草稿模組定案 §2）：
  前段 JSON → Router(route_key) → analyze_case(KB 檢索 + 健檢)
            → ★承辦人確認主文★ → finalize_draft(撰稿 + 驗證 + 對抗式審查)

RAG 走 Bedrock KB（路線 A）。所有 LLM / KB 呼叫走 bedrock_client（≤1 RPS）。
單件呼叫預算約 3–4 次（§5）。
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
    """確認主文前的分析：路由 → KB 檢索（法規 + 相似案例）→（健檢由 defects 模組接）。"""
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
        "suggested_disposition": "訴願駁回（建議，待承辦人確認）",
    }


def finalize_draft(analysis: dict, confirmed_disposition: str, client=None) -> dict:
    """承辦人確認主文後：撰稿（1 次）→ 驗證（規則）→ 對抗式審查（1–2 次）。"""
    client = client or get_client()
    d = draft.generate_draft(
        analysis["route_key"],
        confirmed_disposition,
        analysis["fields"],
        analysis.get("recommended_laws", []),
        analysis.get("similar_cases", []),
        analysis.get("defects", []),
        client=client,
    )
    report = verify.verify_draft(
        d, analysis["fields"], analysis.get("recommended_laws", []), {}, analysis.get("defects", []),
    )
    review = critic.adversarial_review(d, analysis["fields"], client=client)
    return {"draft": d, "verification": report, "adversarial": review,
            "confirmed_disposition": confirmed_disposition}


def process_case(payload: dict, confirmed_disposition: str | None = None, client=None) -> dict:
    """端到端便利函式（測試用；正式須由承辦人確認主文）。"""
    client = client or get_client()
    analysis = analyze_case(payload, client=client)
    if analysis.get("error"):
        return analysis
    disposition = confirmed_disposition or analysis["suggested_disposition"]
    return {**analysis, **finalize_draft(analysis, disposition, client=client)}


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
    parser = argparse.ArgumentParser(description="ntpc-appeal-ai 單體主線 v1.1")
    parser.add_argument("--dry-run", action="store_true", help="不呼叫真實 Bedrock/KB")
    args = parser.parse_args()
    client = get_client(dry_run=args.dry_run)
    result = process_case(_demo_payload(), client=client)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
