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

from . import (router, similar_cases, recommend_laws, draft, verify, critic,
               defects as defects_mod, timeline as timeline_mod, procedure,
               extract as extract_mod, classify as classify_mod, issues as issues_mod)
from .schemas import build_empty_input, validate_input
from .bedrock_client import get_client


# 中文案由 label → route_key（classify 回中文 case_type，pipeline 內部用英文枚舉）
_LABEL_TO_ROUTE = {
    "洗錢防制法": "money_laundering",
    "廢棄物清理法": "waste",
    "空氣污染防制法": "air_pollution",
    "建築法": "building",
    "噪音管制法": "noise",
    "其他": "general",
}


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


def _public_timeline(timeline: dict) -> dict:
    """回傳可 JSON 序列化的 timeline：移除內部用的 _parsed（含 date 物件）。

    _parsed 的 date 物件僅供 procedure / defects 內部計算；對外輸出（result → CLI /
    Lambda / S3 寫檔）只需頂層的顯示字串欄位（皆為 str/None，可序列化）。
    """
    return {k: v for k, v in (timeline or {}).items() if k != "_parsed"}


# ---------- 前段入口：原始案卷 → 交接契約 payload ----------
CONFIDENCE_THRESHOLD = 0.5   # 分類信心低於此值標 need_human_review


def intake(case_text: dict, case_id: str = "", client=None,
           with_issues: bool = False) -> dict:
    """前段全自動入口：原始案卷文字 → 符合交接契約的 payload。

    流程：classify（0 呼叫規則版）→ extract（1 呼叫）→ 組 payload。
    with_issues=True 時額外呼叫 tag_issues（+1 呼叫），預設關閉以守呼叫預算
    （主線 extract1+檢索1+撰稿1+法官1=4，重寫最壞6；開 issues 則各 +1）。

    輸入 case_text: {"petition","original_disposition_doc"/"disposition"}
    輸出: schemas 交接契約 payload（可直接餵 process_case）。
    """
    client = client or get_client()

    # 1) 案由分類（關鍵字規則，0 呼叫）
    cls = classify_mod.classify_case_type(case_text)
    label = cls.get("case_type", "其他")
    route_key = _LABEL_TO_ROUTE.get(label, "general")
    confidence = float(cls.get("confidence", 0.0) or 0.0)

    # 2) 結構化擷取（1 呼叫）
    fields = extract_mod.extract_fields(case_text, client=client)

    # 3) 爭點標註（可選，+1 呼叫）
    if with_issues:
        tagged = issues_mod.tag_issues(fields, route_key, client=client)
        fields["issues"] = tagged.get("issues", [])

    # 4) 組交接契約 payload
    payload = build_empty_input()
    payload["case_id"] = case_id
    payload["case_type"] = {
        "label": label,
        "route_key": route_key,
        "confidence": confidence,
        # 分類信心低於門檻時提示人工確認（仍照常路由與撰稿，不阻斷）
        "need_human_review": confidence < CONFIDENCE_THRESHOLD,
    }
    payload["extracted_fields"] = fields
    payload["raw_text"] = {
        "petition": case_text.get("petition", "") or "",
        "original_disposition_doc": (case_text.get("disposition")
                                     or case_text.get("original_disposition_doc", "") or ""),
    }
    return payload


def process_from_text(case_text: dict, case_id: str = "", client=None,
                      with_issues: bool = False) -> dict:
    """端到端全自動（含前段）：原始案卷文字 → intake → process_case。"""
    client = client or get_client()
    payload = intake(case_text, case_id=case_id, client=client, with_issues=with_issues)
    return process_case(payload, client=client)


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
    timeline = timeline_mod.build_timeline(fields)                        # 純規則，不吃配額

    base = {
        "case_id": payload.get("case_id"),
        "route_key": route_key,
        "need_human_review": router.needs_human_review(payload),
        "fields": fields,
    }

    # 程序不受理判定（§10.4 快速通道，0 次 Bedrock 呼叫）：
    #   1. 前段已於 admissibility 標不受理 → 直接採用
    #   2. 否則後段以 procedure_check 依 timeline 自行複算（逾期等可程式判定款次）
    proc = procedure.procedure_check(timeline, fields)
    front_inadmissible = not admissibility.get("is_admissible", True)
    if front_inadmissible or proc.get("is_inadmissible"):
        note = admissibility.get("note", "") if front_inadmissible else (proc.get("reason") or "")
        basis = ("前段判定程序不受理（快速通道，未進實體審查）" if front_inadmissible
                 else f"後段程序審查判定不受理（§{proc.get('clause')}）：{proc.get('reason')}")
        return {
            **base,
            "inadmissible": True,
            "inadmissible_note": note,
            "procedure": proc,
            "timeline": _public_timeline(timeline),
            "recommended_laws": [],
            "similar_cases": [],
            "defects": [],
            "decided_disposition": DISPOSITION_INADMISSIBLE,
            "decision_basis": basis,
        }

    q = _query_text(payload)
    laws = recommend_laws.recommend_laws(route_key, q, client=client)     # KB：法規/函釋/判解
    sims = similar_cases.find_similar(route_key, q, client=client)        # KB：歷史決定書共池
    defects = defects_mod.health_check(fields, timeline, client=client)   # 原處分健檢（D1–D6 純規則，0 次呼叫）

    disposition, basis = _decide_disposition(sims, defects)
    return {
        **base,
        "inadmissible": False,
        "procedure": proc,
        "timeline": _public_timeline(timeline),
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
      5. 重寫後仍有規則層紅燈（issues 非空）→ 標 needs_human_review；
         法官意見不計入（僅驅動重寫），但仍輸出（全自動不阻斷）

    呼叫數：正常 2 次（撰稿 + 法官）；觸發重寫最壞 4 次。加上 analyze 的 KB 檢索 1 次，
    單件合計 3–5 次，守 §13.3 上限。

    disposition 未指定時採用 analysis 自動判定的主文（正常路徑）；
    保留此參數供批次重跑或特定案件覆寫，主線不會用到。
    """
    client = client or get_client()
    # 系統參考主文（多數決/健檢）：初次生成時僅作 LLM 的 fallback 與參考，
    # 實際主文由 LLM 依事實與法律判斷後回傳（見 draft.generate_draft）。
    ref_disposition = disposition or analysis.get("decided_disposition", DISPOSITION_DISMISS)
    ref_note = analysis.get("decision_basis", "")
    fields = analysis.get("fields", {})
    laws = analysis.get("recommended_laws", [])
    sims = analysis.get("similar_cases", [])
    defects = analysis.get("defects", [])
    timeline = analysis.get("timeline", {})

    # 不受理快速通道：依§77 款次套理由模板，不呼叫 LLM，也不進審查迴圈
    if analysis.get("inadmissible"):
        d = draft.quick_template(analysis.get("procedure", {}), timeline, fields)
        report = verify.verify_draft(d, fields, laws, timeline, defects)
        needs_human = bool(d.get("needs_human_review"))
        flags = []
        if needs_human:
            flags.append("不受理款次無法辨識，套用通用理由，需人工複核")
        return {
            "draft": d,
            "verification": report,
            "adversarial": {"passed": True, "attacks": [], "revocation_risk": "n/a",
                            "note": "不受理案件未進實體審查，不做對抗式審查"},
            "disposition": ref_disposition, "decided_by": "auto",
            "rewrite_count": 0, "needs_human_review": needs_human,
            "quality_flags": flags, "bedrock_calls_estimate": 0,
        }

    # 1) 第一版：主文由 LLM 依事實/法條/相似案例/健檢判斷（系統參考主文僅作 fallback）
    d = draft.generate_draft(analysis["route_key"], ref_disposition, fields, laws, sims, defects,
                             client=client, reference_note=ref_note)
    used = d.get("disposition") or ref_disposition   # 以 LLM 判定的主文為準

    rewrite_count = 0
    while True:
        report = verify.verify_draft(d, fields, laws, timeline, defects)  # 規則層（0 次）
        review = critic.adversarial_review(d, fields, client=client)      # 法官（1 次）

        if not critic.has_blocking_issue(review, report.get("issues", [])):
            break
        if rewrite_count >= max_rewrites:
            break

        # 2) 帶規則問題 + 法官意見重寫（1 次）；沿用 LLM 已定主文，維持主文理由一致
        feedback = critic.collect_feedback(review, report.get("issues", []))
        d = draft.generate_draft(analysis["route_key"], used, fields, laws, sims, defects,
                                 client=client, prev_draft=d, feedback=feedback)
        rewrite_count += 1

    # needs_human_review 只認「規則層紅燈」這類可程式判定的客觀錯誤（引用捏造、
    # 漏回應主張、日期矛盾、條號寫壞）。法官的攻擊點屬「論理可更周延」的提升空間，
    # 幾乎每件都挑得到，僅用來驅動重寫一次，不再計入人工複核，避免提示浮濫失去可信度。
    needs_human = bool(report.get("issues"))
    return {
        "draft": d,
        "verification": report,
        "adversarial": review,
        "disposition": used,
        "decided_by": "llm",
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
    """產生「系統參考主文」，回傳 (參考主文, 參考理由)。

    v2：主文改由 LLM 依事實與法律判斷（見 draft.generate_draft），本函式僅提供
    給 LLM 的參考建議與 fallback，不再是最終結論：
      1. 健檢紅燈 → 傾向撤銷
      2. 相似案例主文多數 → 該主文
      3. 無資料 → 傾向駁回（僅 fallback）
    理由字串為對外顯示用，不含原始統計 dict 與 (n/總) 比數。
    """
    reds = [d for d in (defects or []) if str(d.get("level", "")).lower() == "red"]
    if reds:
        return DISPOSITION_REVOKE, f"原處分健檢發現 {len(reds)} 項重大瑕疵，傾向撤銷"

    counts: Counter[str] = Counter()
    for s in sims or []:
        label = _normalize_disposition(_case_disposition(s))
        if label:
            counts[label] += 1
    if counts:
        top, _ = counts.most_common(1)[0]
        return top, f"相似歷史案例多數為「{top}」"

    return DISPOSITION_DISMISS, "無明顯瑕疵且無相似案例可參，暫採駁回為參考"


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
    """符合交接契約的示範輸入（洗錢案，資料為虛構示範用）。"""
    p = build_empty_input()
    p["case_id"] = "sim-114-001"
    p["case_type"] = {"label": "違反洗錢防制法事件", "route_key": "money_laundering",
                      "confidence": 0.92, "need_human_review": False}
    p["extracted_fields"]["original_disposition"] = {
        "agency": "新北市政府警察局", "date": "民國114年3月1日",
        "doc_no": "新北警刑字第1140012345號", "legal_basis": ["洗錢防制法第15條之2"],
        "penalty_amount": 0, "service_date": "民國114年3月5日", "act_date": "民國113年12月10日",
    }
    p["extracted_fields"]["petition_filed_date"] = "民國114年3月20日"
    p["extracted_fields"]["claims"] = [
        {"id": "C1", "summary": "原處分認定訴願人交付帳戶之事實有誤", "quote": ""},
        {"id": "C2", "summary": "訴願人並無違法之故意或過失", "quote": ""},
    ]
    p["raw_text"]["petition"] = (
        "訴願人主張原處分認定其交付帳戶之事實有誤，且無違法故意過失，請求撤銷原處分。")
    p["raw_text"]["original_disposition_doc"] = (
        "新北市政府警察局以訴願人違反洗錢防制法第15條之2規定，交付金融帳戶予他人，為書面告誡處分。")
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
