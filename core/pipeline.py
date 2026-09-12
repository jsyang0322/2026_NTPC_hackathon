"""單體主線 v1.3（全自動）：接收前段 JSON → 路由 → KB 檢索 → 自動判定主文 → 撰稿 → 檢核。

流程（v1.3：移除人工確認，端到端自動產出）：
  前段 JSON → Router(route_key)
            → analyze_case（KB 檢索：法規 + 相似案例；自動判定主文）
            → generate_case_draft（撰稿 → 規則驗證 → 法官審查 → 有限重寫）
            → 輸出：草稿 + 檢核報告

主文判定原則（_decide_disposition）：
  1. 前段判定不受理 → 訴願不受理（快速通道，0 次 Bedrock 呼叫）
  2. 原處分健檢有紅燈 → 原處分撤銷，另為適法之處分
  3. 否則依相似案例主文多數決；無資料時預設訴願駁回

品質把關全由機器承擔（無人工閘門）：
  - verify_draft（純規則）：客觀問題，紅燈轉 issues 觸發重寫
  - adversarial_review（法官）：論理弱點，high severity 觸發重寫
  - 重寫上限 1 次；仍不過則標 needs_human_review 供事後抽查，不阻斷輸出

RAG 走 Bedrock KB（路線 A）。所有 LLM / KB 呼叫走 bedrock_client（≤1 RPS）。
單件呼叫預算：檢索 1 + 撰稿 1 + 法官 1 = 3 次；觸發重寫最壞 5 次；不受理案 0 次。
"""

from __future__ import annotations

import argparse
import json
from collections import Counter

from . import router, similar_cases, recommend_laws, draft, verify, critic
from .schemas import build_empty_input, validate_input
from .bedrock_client import get_client


# ---------- 主文枚舉（自動判定的三種結果）----------
DISPOSITION_DISMISS = "訴願駁回"
DISPOSITION_REVOKE = "原處分撤銷，另為適法之處分"
DISPOSITION_INADMISSIBLE = "訴願不受理"


def _query_text(payload: dict) -> str:
    """組 KB 檢索用的查詢字串：優先用原處分書全文 + 訴願主張。"""
    raw = payload.get("raw_text", {})
    fields = payload.get("extracted_fields", {})
    claims = " ".join(c.get("summary", "") for c in fields.get("claims", []))
    return (raw.get("original_disposition_doc", "") + "\n" + raw.get("petition", "") + "\n" + claims).strip()


def analyze_case(payload: dict, client=None) -> dict:
    """步驟一：路由 → KB 檢索（法規 + 相似案例）→ 自動判定主文。

    前段已判定不受理者走快速通道：不檢索、不撰稿，直接回不受理（0 次 Bedrock 呼叫）。
    """
    client = client or get_client()
    problems = validate_input(payload)
    if problems:
        return {"error": "介面不符契約", "problems": problems}

    route_key = router.resolve_route(payload)
    fields = payload.get("extracted_fields", {})
    admissibility = payload.get("admissibility", {})

    base = {
        "case_id": payload.get("case_id"),
        "route_key": route_key,
        "need_human_review": router.needs_human_review(payload),
        "fields": fields,
    }

    # 快速通道（§10.4）：程序不受理，不進實體審查
    if not admissibility.get("is_admissible", True):
        return {
            **base,
            "inadmissible": True,
            "inadmissible_note": admissibility.get("note", ""),
            "recommended_laws": [],
            "similar_cases": [],
            "defects": [],
            "decided_disposition": DISPOSITION_INADMISSIBLE,
            "decision_basis": "前段判定程序不受理（快速通道，未進實體審查）",
        }

    q = _query_text(payload)
    laws = recommend_laws.recommend_laws(route_key, q, client=client)     # KB：法規/函釋/判解
    sims = similar_cases.find_similar(route_key, q, client=client)        # KB：歷史決定書共池
    defects: list[dict] = []   # TODO: 接 core.defects.health_check（健檢那組）

    disposition, basis = _decide_disposition(sims, defects)
    return {
        **base,
        "inadmissible": False,
        "recommended_laws": laws,
        "similar_cases": sims,
        "defects": defects,
        "decided_disposition": disposition,
        "decision_basis": basis,
    }


def generate_case_draft(analysis: dict, disposition: str | None = None,
                       client=None, max_rewrites: int = 1) -> dict:
    """步驟二：對抗式審查迴圈——生成 → 規則檢查 → 法官審查 →（有問題）有限重寫。

    流程（§11.2）：
      1. 生成草稿（1 次呼叫）
      2. verify 規則檢查（0 次，純程式，先擋客觀問題並產出 issues）
      3. critic 法官對抗式審查（1 次呼叫）
      4. 規則紅燈或法官指出重大問題 → 帶意見重寫（≤ max_rewrites 次，預設 1）
      5. 仍不過 → 標 needs_human_review，但仍輸出（全自動不阻斷）

    呼叫數：正常 2 次（撰稿 + 法官）；觸發重寫最壞 4 次。加上 analyze 的 KB 檢索 1 次，
    單件合計 3–5 次，守 §13.3 上限。

    disposition 未指定時採用 analysis 自動判定的主文（正常路徑）；
    保留此參數供批次重跑或特定案件覆寫，主線不會用到。
    """
    client = client or get_client()
    used = disposition or analysis.get("decided_disposition", DISPOSITION_DISMISS)
    fields = analysis.get("fields", {})
    laws = analysis.get("recommended_laws", [])
    sims = analysis.get("similar_cases", [])
    defects = analysis.get("defects", [])

    # 不受理快速通道：套模板，不呼叫 LLM，也不進審查迴圈
    if analysis.get("inadmissible"):
        d = draft.quick_template({}, {})
        report = verify.verify_draft(d, fields, laws, {}, defects)
        return {
            "draft": d,
            "verification": report,
            "adversarial": {"passed": True, "attacks": [], "revocation_risk": "n/a",
                            "note": "不受理案件未進實體審查，不做對抗式審查"},
            "disposition": used, "decided_by": "auto",
            "rewrite_count": 0, "needs_human_review": False,
            "quality_flags": [], "bedrock_calls_estimate": 0,
        }

    # 1) 第一版
    d = draft.generate_draft(analysis["route_key"], used, fields, laws, sims, defects, client=client)

    rewrite_count = 0
    while True:
        report = verify.verify_draft(d, fields, laws, {}, defects)     # 規則層（0 次）
        review = critic.adversarial_review(d, fields, client=client)   # 法官（1 次）

        if not critic.has_blocking_issue(review, report.get("issues", [])):
            break
        if rewrite_count >= max_rewrites:
            break

        # 2) 帶規則問題 + 法官意見重寫（1 次）
        feedback = critic.collect_feedback(review, report.get("issues", []))
        d = draft.generate_draft(analysis["route_key"], used, fields, laws, sims, defects,
                                 client=client, prev_draft=d, feedback=feedback)
        rewrite_count += 1

    needs_human = critic.has_blocking_issue(review, report.get("issues", []))
    return {
        "draft": d,
        "verification": report,
        "adversarial": review,
        "disposition": used,
        "decided_by": "auto",
        "rewrite_count": rewrite_count,
        "needs_human_review": needs_human,
        "quality_flags": _quality_flags(analysis, report, review, rewrite_count),
    }


def process_case(payload: dict, client=None) -> dict:
    """端到端全自動：分析（含主文判定）→ 撰稿 → 驗證 → 對抗式審查。"""
    client = client or get_client()
    analysis = analyze_case(payload, client=client)
    if analysis.get("error"):
        return analysis
    return {**analysis, **generate_case_draft(analysis, client=client)}


# ---------- 自動判定與品質旗標 ----------

def _decide_disposition(sims: list[dict], defects: list[dict]) -> tuple[str, str]:
    """自動判定主文，回傳 (主文, 判定理由)。

    1. 健檢紅燈 → 原處分有重大瑕疵，撤銷另為適法處分
    2. 相似案例主文多數決（歷史一致性）
    3. 無資料 → 預設駁回
    """
    reds = [d for d in (defects or []) if str(d.get("level", "")).lower() == "red"]
    if reds:
        titles = [d.get("title", d.get("id", "")) for d in reds]
        return DISPOSITION_REVOKE, f"原處分健檢紅燈 {len(reds)} 項（{titles}），認有撤銷事由"

    counts: Counter[str] = Counter()
    for s in sims or []:
        label = _normalize_disposition(_case_disposition(s))
        if label:
            counts[label] += 1
    if counts:
        top, n = counts.most_common(1)[0]
        return top, f"相似案例主文多數決：{dict(counts)}，採 {top}（{n}/{sum(counts.values())}）"

    return DISPOSITION_DISMISS, "無健檢紅燈且相似案例無主文資料，採預設主文"


def _case_disposition(sim: dict) -> str:
    """從相似案例取主文字串，容錯 KB 原始 hit 形狀。"""
    if not isinstance(sim, dict):
        return ""
    direct = sim.get("disposition")
    if isinstance(direct, str) and direct:
        return direct
    meta = sim.get("metadata")
    if isinstance(meta, dict):
        val = meta.get("disposition")
        if isinstance(val, str):
            return val
    return ""


def _normalize_disposition(text: str) -> str | None:
    """把自由文字主文歸類到三種枚舉。不受理優先（不進實體）。"""
    if not text:
        return None
    if "不受理" in text:
        return DISPOSITION_INADMISSIBLE
    if "撤銷" in text:
        return DISPOSITION_REVOKE
    if "駁回" in text:
        return DISPOSITION_DISMISS
    return None


def _quality_flags(analysis: dict, report: dict, review: dict, rewrite_count: int = 0) -> list[str]:
    """彙整需要注意的訊號。全自動流程不阻斷，但把風險標出來供事後抽查。"""
    flags: list[str] = []
    if analysis.get("need_human_review"):
        flags.append("前段分類信心不足（need_human_review）")
    blocking = report.get("blocking") or []
    if blocking:
        flags.append(f"重寫後仍有驗證紅燈：{blocking}" if rewrite_count
                     else f"驗證紅燈未通過：{blocking}")
    if not review.get("passed", True):
        flags.append("法官對抗式審查未通過")
    risk = str(review.get("revocation_risk", ""))
    if risk in ("high", "medium"):
        flags.append(f"對抗式審查撤銷風險：{risk}")
    if rewrite_count:
        flags.append(f"已自動重寫 {rewrite_count} 次")
    return flags


# ---------- Demo / CLI ----------

def _demo_payload() -> dict:
    """符合交接契約的骨架測試輸入（洗錢案）。"""
    p = build_empty_input()
    p["case_id"] = "sim-114-001"
    p["case_type"] = {"label": "違反洗錢防制法事件", "route_key": "money_laundering",
                      "confidence": 0.92, "need_human_review": False}
    p["extracted_fields"]["claims"] = [{"id": "C1", "summary": "原處分認定事實有誤", "quote": ""}]
    p["raw_text"]["petition"] = "訴願人主張原處分認定事實有誤，請求撤銷。（骨架測試假資料）"
    p["raw_text"]["original_disposition_doc"] = "原處分書：因違反洗錢防制法裁處。（骨架測試假資料）"
    return p


def main() -> None:
    parser = argparse.ArgumentParser(description="ntpc-appeal-ai 單體主線 v1.3（全自動）")
    parser.add_argument("--dry-run", action="store_true", help="不呼叫真實 Bedrock/KB")
    args = parser.parse_args()
    client = get_client(dry_run=args.dry_run)
    result = process_case(_demo_payload(), client=client)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
