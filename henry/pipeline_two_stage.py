# -*- coding: utf-8 -*-
"""
兩段式訴願書初步受理判定 pipeline
=====================================================================
設計理念(成本 vs 準確率權衡):
  第一段  快而便宜的 Nova Lite 判所有案件,並自評信心(高/中/低)。
  第二段  只把「風險較高」的案件交給準確但慢/貴的 Claude Sonnet 4.5 覆核。

  交付 Claude 覆核的條件(命中任一即覆核):
    (1) Nova 判「受理(1)」            —— 經驗顯示 Nova 的錯誤幾乎都是
                                          「該不受理卻誤判受理」的偽陽性,
                                          故受理判斷最需要覆核。
    (2) Nova 自評信心不是「高」        —— 事實需權衡或標的性質難認定者。
  其餘(判不受理且高信心)直接採用 Nova 結果,省下 LLM 成本。

  最終標記以覆核後結果為準;未覆核者沿用 Nova。

共用 rag_triage 的知識庫、檢索與 LLM 呼叫;本檔只負責兩段式編排。

用法:
  python pipeline_two_stage.py <案號或PDF的S3 key>   # 判單一案件
  python pipeline_two_stage.py --batch [N]            # 批次前N筆並評分(預設全部)
"""
import os, sys, json, time
from pathlib import Path
import boto3
from botocore.config import Config

import rag_triage as R

NOVA_MODEL = os.environ.get("BEDROCK_MODEL_LIGHT", "amazon.nova-lite-v1:0")
CLAUDE_MODEL = os.environ.get("BEDROCK_MODEL_WRITER", "us.anthropic.claude-sonnet-4-5-20250929-v1:0")
BASE_DIR = Path(__file__).resolve().parent


def _set_model(model_id):
    """切換 rag_triage 使用的 LLM,並為 Claude 加上呼叫逾時避免卡住。"""
    R.LLM_MODEL = model_id
    R.rt = boto3.Session(region_name=R.REGION).client(
        "bedrock-runtime",
        config=Config(read_timeout=90, connect_timeout=10, retries={"max_attempts": 2}),
    )


def _call_with_retry(text, chunks, mat, tries=4):
    for attempt in range(tries):
        try:
            return R.triage_one(text, chunks, mat)
        except Exception as e:
            if attempt == tries - 1:
                return None, None, "ERROR:" + repr(e), None, None
            time.sleep(3 * (attempt + 1))


def needs_review(label, conf):
    """判斷是否需要交 Claude 覆核。"""
    if label is None:
        return True                      # 第一段失敗,交覆核
    if label == 1:
        return True                      # 判受理 -> 覆核
    if conf != "高":
        return True                      # 非高信心 -> 覆核
    return False


def triage_case(cid, chunks, mat):
    """對單一案件跑兩段式,回傳完整判定結果 dict。"""
    key = f"{R.PDF_PREFIX}{cid}.pdf"
    text = R.pdf_text(key)

    # ---- 第一段:Nova Lite ----
    _set_model(NOVA_MODEL)
    n_label, n_ground, n_reason, _, n_conf = _call_with_retry(text, chunks, mat)

    result = {
        "案號": cid,
        "第一段_模型": "nova-lite",
        "第一段_受理與否": ("受理" if n_label == 1 else "不受理") if n_label is not None else None,
        "第一段_款次": n_ground,
        "第一段_信心": n_conf,
        "第一段_理由": n_reason,
    }

    # ---- 決定是否覆核 ----
    if not needs_review(n_label, n_conf):
        result.update({
            "是否覆核": False,
            "最終標記": n_label,
            "最終受理與否": "不受理",
            "最終款次": n_ground,
            "最終理由": n_reason,
            "採用模型": "nova-lite",
        })
        return result

    # ---- 第二段:Claude 覆核 ----
    _set_model(CLAUDE_MODEL)
    c_label, c_ground, c_reason, _, c_conf = _call_with_retry(text, chunks, mat)

    # Claude 失敗時保守退回 Nova 結果
    final_label = c_label if c_label is not None else n_label
    used = "claude-sonnet-4.5" if c_label is not None else "nova-lite(claude覆核失敗退回)"
    result.update({
        "是否覆核": True,
        "覆核_受理與否": ("受理" if c_label == 1 else "不受理") if c_label is not None else None,
        "覆核_款次": c_ground,
        "覆核_信心": c_conf,
        "覆核_理由": c_reason,
        "最終標記": final_label,
        "最終受理與否": ("受理" if final_label == 1 else "不受理") if final_label is not None else None,
        "最終款次": (c_ground if c_label is not None else n_ground),
        "最終理由": (c_reason if c_label is not None else n_reason),
        "採用模型": used,
    })
    return result


def load_truth():
    ans = R.s3.get_object(Bucket=R.BUCKET, Key=R.ANSWER_KEY)["Body"].read().decode("utf-8")
    truth = {}
    for l in ans.splitlines():
        if l.strip():
            r = json.loads(l)
            truth[r["案號"]] = (1 if r.get("受理與否") == "受理" else 0, r.get("不受理款次"))
    return truth


def run_batch(limit=None):
    chunks, mat = R.load_kb()
    keys = []
    for page in R.s3.get_paginator("list_objects_v2").paginate(Bucket=R.BUCKET, Prefix=R.PDF_PREFIX):
        for o in page.get("Contents", []):
            if o["Key"].lower().endswith(".pdf"):
                keys.append(o["Key"].split("/")[-1].rsplit(".", 1)[0])
    keys.sort()
    if limit:
        keys = keys[:limit]

    truth = load_truth()
    results = []
    reviewed = 0
    for i, cid in enumerate(keys, 1):
        r = triage_case(cid, chunks, mat)
        results.append(r)
        if r.get("是否覆核"):
            reviewed += 1
        t = truth.get(cid)
        ok = "OK" if (t and r["最終標記"] == t[0]) else "XX"
        rv = "→Claude覆核" if r.get("是否覆核") else "(Nova直採)"
        print(f"[{i}/{len(keys)}] {cid} 最終={r['最終標記']} 真={t[0] if t else '?'} {ok} {rv}", flush=True)
        with open(BASE_DIR / "two_stage_predictions.jsonl", "w", encoding="utf-8") as f:
            for x in results:
                tt = truth.get(x["案號"])
                f.write(json.dumps({**x, "正解": (tt[0] if tt else None),
                                    "正解款次": (tt[1] if tt else None)}, ensure_ascii=False) + "\n")

    # 評分
    correct = wrong = 0
    tp = tn = fp = fn = 0
    for r in results:
        t = truth.get(r["案號"])
        if not t or r["最終標記"] is None:
            continue
        pl, tl = r["最終標記"], t[0]
        if pl == tl: correct += 1
        else: wrong += 1
        if pl == 1 and tl == 1: tp += 1
        elif pl == 0 and tl == 0: tn += 1
        elif pl == 1 and tl == 0: fp += 1
        elif pl == 0 and tl == 1: fn += 1
    n = correct + wrong

    lines = []
    lines.append("=== 兩段式 (Nova Lite → Claude 覆核) 結果 ===")
    lines.append(f"總筆數: {len(keys)}  交 Claude 覆核: {reviewed}  Nova 直接採用: {len(keys)-reviewed}")
    lines.append(f"判斷正確: {correct}  判斷錯誤: {wrong}")
    if n:
        lines.append(f"正確率: {correct/n*100:.1f}%")
    lines.append(f"混淆矩陣: TP受理{tp} TN不受理{tn} FP誤判受理{fp} FN誤判不受理{fn}")
    lines.append("")
    lines.append(f"成本對照: 只有 {reviewed}/{len(keys)} 筆呼叫到較貴的 Claude,"
                 f"其餘 {len(keys)-reviewed} 筆僅用 Nova Lite。")
    report = "\n".join(lines)
    with open(BASE_DIR / "two_stage_report.txt", "w", encoding="utf-8") as f:
        f.write(report)
    print(report)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python pipeline_two_stage.py <案號> | --batch [N]")
        sys.exit(1)
    if sys.argv[1] == "--batch":
        lim = int(sys.argv[2]) if len(sys.argv) > 2 else None
        run_batch(lim)
    else:
        chunks, mat = R.load_kb()
        res = triage_case(sys.argv[1], chunks, mat)
        print(json.dumps(res, ensure_ascii=False, indent=2))
