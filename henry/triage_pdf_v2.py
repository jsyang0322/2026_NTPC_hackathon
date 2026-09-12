# -*- coding: utf-8 -*-
"""
訴願書 PDF 初步受理判定器 (原始訴願書v2_pdf)
================================================================
流程:
  1. 從 S3 下載 原始訴願書v2_pdf/SYN*.pdf,抽出純文字。
  2. 只用 PDF 文字判斷受理(1)/不受理(0)+第77條款次+理由。
     判斷階段完全不讀 appeals_structured.jsonl 的任何內容。
  3. 判完後,才用 PDF 檔名(SYNxxxx)去 appeals_structured.jsonl
     取 `受理與否` 當標準答案,計算正確率、混淆矩陣、錯誤清單。

日期一律自 PDF 文字解析民國年月日後自算期間 (真實情境)。
"""
import io
import json
import re
import datetime
import os
import boto3
from pypdf import PdfReader

BUCKET = os.environ.get("APPEAL_S3_BUCKET", "bucket-jahseh")
PDF_PREFIX = os.environ.get("APPEAL_PDF_PREFIX", "原始訴願書v2_pdf/")
ANSWER_KEY = os.environ.get("APPEAL_ANSWER_KEY", "appeals_structured.jsonl")

REGION = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-west-2"))
s3 = boto3.Session(region_name=REGION).client("s3")

_DATE_RE = re.compile(r"民國\s*(\d+)\s*年\s*(\d+)\s*月\s*(\d+)\s*日")
_ROC_TAIL_RE = re.compile(r"中\s*華\s*民\s*國\s*(\d+)\s*年\s*(\d+)\s*月\s*(\d+)\s*日")


def roc_to_date(y, m, d):
    try:
        return datetime.date(int(y) + 1911, int(m), int(d))
    except ValueError:
        return None


def norm(text):
    """移除空白以利跨行關鍵詞比對。"""
    return re.sub(r"\s+", "", text or "")


def extract_dates(text):
    """回傳 (收受日, 提起日)。收受日取『收受知悉行政處分之年月日:民國...』;
    提起日取結尾『中華民國...』。抽不到再退而求其次。"""
    flat = norm(text)

    served = None
    m = re.search(r"收受知悉行政處分之年月日民國(\d+)年(\d+)月(\d+)日", flat)
    if m:
        served = roc_to_date(*m.groups())
    if served is None:
        m = re.search(r"收受之?次?日?.*?民國(\d+)年(\d+)月(\d+)日", flat)  # loose
    if served is None:
        m2 = re.search(r"於民國(\d+)年(\d+)月(\d+)日收受", flat)
        if m2:
            served = roc_to_date(*m2.groups())

    filed = None
    m = re.search(r"中華民國(\d+)年(\d+)月(\d+)日", flat)
    if m:
        filed = roc_to_date(*m.groups())
    if filed is None:
        m2 = re.search(r"於民國(\d+)年(\d+)月(\d+)日提出本件訴願", flat)
        if m2:
            filed = roc_to_date(*m2.groups())
    return served, filed


def stated_elapsed_days(text):
    """PDF 事實段常明寫『距收受原處分之日已N日』,擷取該數字作為輔助。"""
    flat = norm(text)
    m = re.search(r"距收受原處分之日已(\d+)日", flat)
    return int(m.group(1)) if m else None


def triage_pdf(text):
    """只吃 PDF 文字,回傳 (label, 款次, 理由)。"""
    flat = norm(text)

    # ---- 77(1) 訴願書不合法定程式,經通知未補正 ----
    if ("通知" in flat and "補正" in flat and
            ("迄未補正" in flat or "仍未補正" in flat or "未補正" in flat) and
            ("空白" in flat or "應記載事項" in flat)):
        return 0, "77(1)", "訴願書未具備法定應記載事項,經通知後仍未補正。"

    # ---- 77(4) 無訴願能力 (未成年且無法定代理人) ----
    if (("未成年" in flat) or ("為未成年人" in flat)) and (
            "未由法定代理人" in flat or "無法定代理人" in flat or "未經法定代理" in flat):
        return 0, "77(4)", "訴願人無訴願能力(未成年且未由法定代理人代為訴願)。"
    if "無訴願能力" in flat:
        return 0, "77(4)", "訴願人無訴願能力。"

    # ---- 77(3) 當事人不適格 ----
    if ("無關第三人" in flat) or ("非利害關係人" in flat) or ("當事人不適格" in flat) \
       or ("與原處分並無法律上利害關係" in flat):
        return 0, "77(3)", "訴願人非原處分相對人或利害關係人,當事人不適格。"

    # ---- 77(8) 標的非行政處分 ----
    if ("非屬行政處分" in flat) or ("非行政處分" in flat) or ("觀念通知" in flat) \
       or ("性質上非屬行政處分" in flat):
        return 0, "77(8)", "所爭執標的性質上非屬行政處分。"

    # ---- 77(6) 原處分已不存在 ----
    if ("效力已消滅" in flat) or ("執行完畢" in flat and "消滅" in flat) \
       or ("原處分已撤銷" in flat) or ("已失效" in flat) or ("業已失效" in flat) \
       or ("已廢止" in flat) or ("處分之效力已消滅" in flat):
        return 0, "77(6)", "原處分已不存在(執行完畢/撤銷/失效,效力已消滅)。"

    # ---- 77(7) 重複提起訴願 ----
    if ("前曾就同一事件提起訴願" in flat) or ("重行提起" in flat) or ("重複提起" in flat) \
       or ("業經決定確定" in flat and "同一事件" in flat):
        return 0, "77(7)", "就同一事件重複提起訴願。"

    # ---- 77(2) 訴願逾期 (自算期間;輔以文字明載天數) ----
    served, filed = extract_dates(text)
    stated = stated_elapsed_days(text)
    elapsed = None
    if served and filed:
        elapsed = (filed - served).days
    if elapsed is None and stated is not None:
        elapsed = stated
    if elapsed is not None and elapsed > 30:
        basis = f"實際 {elapsed} 日" if (served and filed) else f"訴願書載明已 {elapsed} 日"
        return 0, "77(2)", f"提起訴願逾法定 30 日期間({basis},逾期 {elapsed - 30} 日)。"

    # ---- 無不受理事由 -> 受理 ----
    return 1, None, "程序要件具備,無不受理事由,應予受理進入實體審查。"


def main():
    # 列出所有 v2 PDF
    keys = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix=PDF_PREFIX):
        for o in page.get("Contents", []):
            if o["Key"].lower().endswith(".pdf"):
                keys.append(o["Key"])
    keys.sort()

    predictions = {}   # 案號 -> (label, 款次, 理由)
    extract_fail = []
    import sys
    for i, key in enumerate(keys, 1):
        case_id = key.split("/")[-1].rsplit(".", 1)[0]   # 檔名=案號,僅用於配對
        try:
            raw = s3.get_object(Bucket=BUCKET, Key=key)["Body"].read()
            text = "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(raw)).pages)
        except Exception as e:
            extract_fail.append((case_id, repr(e)))
            print(f"[{i}/{len(keys)}] {case_id} EXTRACT FAIL", flush=True)
            continue
        predictions[case_id] = triage_pdf(text)
        print(f"[{i}/{len(keys)}] {case_id} -> {predictions[case_id][0]}", flush=True)

    # === 解封答案 ===
    ans_txt = s3.get_object(Bucket=BUCKET, Key=ANSWER_KEY)["Body"].read().decode("utf-8")
    truth = {}
    for l in ans_txt.splitlines():
        if l.strip():
            r = json.loads(l)
            truth[r["案號"]] = (1 if r.get("受理與否") == "受理" else 0, r.get("不受理款次"))

    correct = wrong = 0
    wrong_list = []
    no_answer = []
    for case_id, (label, ground, reason) in predictions.items():
        if case_id not in truth:
            no_answer.append(case_id)
            continue
        t_label, t_ground = truth[case_id]
        if label == t_label:
            correct += 1
        else:
            wrong += 1
            wrong_list.append({
                "案號": case_id,
                "我的判定": "受理(1)" if label == 1 else "不受理(0)",
                "我判款次": ground,
                "正解": "受理" if t_label == 1 else "不受理",
                "正解款次": t_ground,
                "我的理由": reason,
            })

    n = correct + wrong
    lines = []
    lines.append(f"v2 PDF 總數: {len(keys)}")
    lines.append(f"成功抽取並判定: {len(predictions)}")
    lines.append(f"文字抽取失敗: {len(extract_fail)}")
    lines.append(f"有對應答案並納入評分: {n}")
    lines.append("")
    lines.append(f"判斷正確: {correct} 筆")
    lines.append(f"判斷錯誤: {wrong} 筆")
    if n:
        lines.append(f"正確率: {correct / n * 100:.1f}%")
    lines.append("")

    tp = sum(1 for c,(l,g,r) in predictions.items() if c in truth and l==1 and truth[c][0]==1)
    tn = sum(1 for c,(l,g,r) in predictions.items() if c in truth and l==0 and truth[c][0]==0)
    fp = sum(1 for c,(l,g,r) in predictions.items() if c in truth and l==1 and truth[c][0]==0)
    fn = sum(1 for c,(l,g,r) in predictions.items() if c in truth and l==0 and truth[c][0]==1)
    lines.append("混淆矩陣:")
    lines.append(f"  正確判為受理  (TP): {tp}")
    lines.append(f"  正確判為不受理(TN): {tn}")
    lines.append(f"  誤判為受理    (FP): {fp}")
    lines.append(f"  誤判為不受理  (FN): {fn}")
    lines.append("")

    if extract_fail:
        lines.append("=== 文字抽取失敗清單 ===")
        for cid, err in extract_fail:
            lines.append(f"  {cid}: {err}")
        lines.append("")
    if no_answer:
        lines.append("=== 無對應答案(未評分) ===")
        lines.append("  " + ", ".join(no_answer))
        lines.append("")
    if wrong_list:
        lines.append("=== 錯誤清單 ===")
        for w in wrong_list:
            lines.append(json.dumps(w, ensure_ascii=False, indent=2))
            lines.append("-" * 40)
    else:
        lines.append("沒有任何判斷錯誤。")

    with open("triage_pdf_v2_report.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    with open("triage_pdf_v2_predictions.jsonl", "w", encoding="utf-8") as f:
        for cid, (label, ground, reason) in sorted(predictions.items()):
            t = truth.get(cid)
            f.write(json.dumps({
                "案號": cid, "預測標記": label, "預測款次": ground, "預測理由": reason,
                "正解": ("受理" if t and t[0]==1 else "不受理") if t else None,
                "正解款次": t[1] if t else None,
            }, ensure_ascii=False) + "\n")

    print(f"done; correct={correct} wrong={wrong} of {n}")


if __name__ == "__main__":
    main()
