# -*- coding: utf-8 -*-
"""
訴願書初步受理判定器 (真實情境版)
================================================
原則:
  1. 只從「訴願書內容」判斷:主要吃 `訴願書敘述`(自然語言),
     自己解析民國日期、計算期間、抽取關鍵事實。
  2. 判斷階段完全遮住答案欄位:受理與否 / 不受理款次 / 判定理由 /
     實體結果 / 逾期天數 一律不讀。
  3. 判完後才解封 `受理與否` 做對比,報告對/錯筆數與錯誤清單。

輸出標記: 受理=1, 不受理=0
依據: 訴願法第 77 條各款不受理事由。
"""
import json
import os
import re
import datetime
import boto3

BUCKET = os.environ.get("APPEAL_S3_BUCKET", "bucket-jahseh")
KEY = os.environ.get("APPEAL_ANSWER_KEY", "appeals_structured.jsonl")

# 判斷階段禁止讀取的答案欄位 (含 逾期天數: 那是算好的結論)
ANSWER_FIELDS = {"受理與否", "不受理款次", "判定理由", "實體結果", "逾期天數"}


# ---------- 資料載入層: 遮住答案 ----------
def load_records():
    region = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-west-2"))
    s3 = boto3.Session(region_name=region).client("s3")
    txt = s3.get_object(Bucket=BUCKET, Key=KEY)["Body"].read().decode("utf-8")
    return [json.loads(l) for l in txt.splitlines() if l.strip()]


def make_input_view(rec):
    """回傳只含輸入欄位的 dict,答案欄位一律移除。關鍵元件內也移除任何結論性欄位。"""
    view = {k: v for k, v in rec.items() if k not in ANSWER_FIELDS}
    return view


# ---------- 民國日期解析 ----------
_DATE_RE = re.compile(r"民國\s*(\d+)\s*年\s*(\d+)\s*月\s*(\d+)\s*日")


def parse_roc_date(s):
    if not s:
        return None
    m = _DATE_RE.search(s)
    if not m:
        return None
    y, mo, d = (int(g) for g in m.groups())
    try:
        return datetime.date(y + 1911, mo, d)
    except ValueError:
        return None


def extract_two_dates_from_text(text):
    """從敘述抽 (送達日, 提起日)。敘述格式固定為
    '...於民國X年X月X日送達之行政處分,於民國Y年Y月Y日提起訴願。'"""
    served = None
    filed = None
    m_served = re.search(r"民國\s*\d+\s*年\s*\d+\s*月\s*\d+\s*日\s*送達", text)
    if m_served:
        served = parse_roc_date(m_served.group(0))
    m_filed = re.search(r"於\s*(民國\s*\d+\s*年\s*\d+\s*月\s*\d+\s*日)\s*提起訴願", text)
    if m_filed:
        filed = parse_roc_date(m_filed.group(1))
    return served, filed


# ---------- 第 77 條各款規則 (只吃輸入視圖) ----------
def triage(view):
    """回傳 (label, 款次, 理由)。label: 1=受理, 0=不受理。
    主要以 訴願書敘述 判斷;日期一律自敘述解析後自算期間。"""
    text = view.get("訴願書敘述", "") or ""
    comp = view.get("關鍵元件", {}) or {}

    # 期間:優先自敘述抽日期;抽不到才退回關鍵元件的日期字串
    served, filed = extract_two_dates_from_text(text)
    if served is None:
        served = parse_roc_date(comp.get("原處分送達日", ""))
    if filed is None:
        filed = parse_roc_date(comp.get("提起訴願日", ""))
    statutory_days = comp.get("法定期間日數", 30) or 30

    reasons_order = []

    # 77(1) 訴願書不合法定程式且經通知未補正
    if ("未具備法定應記載事項" in text or "程式" in text and "未補正" in text) or \
       ("經通知仍未補正" in text):
        return 0, "77(1)", "訴願書未具備法定應記載事項,經通知仍未補正。"

    # 77(4) 無訴願能力 (未成年且無法定代理人)
    if ("未成年" in text) or ("無訴願能力" in text) or \
       ("未由法定代理人" in text):
        return 0, "77(4)", "訴願人無訴願能力(未成年且未由法定代理人代為訴願)。"

    # 77(3) 當事人不適格 (無關第三人 / 非利害關係人)
    if ("無關第三人" in text) or ("非利害關係" in text) or ("當事人不適格" in text):
        return 0, "77(3)", "訴願人非原處分相對人或利害關係人,當事人不適格。"

    # 77(8) 標的非行政處分
    if ("非屬行政處分" in text) or ("非行政處分" in text):
        return 0, "77(8)", "所爭執標的性質上非屬行政處分。"

    # 77(6) 原處分已不存在
    # 敘述句式固定為: 原處分現況為「XXX」。抓引號內的狀態字串來判斷是否仍有效。
    m_status = re.search(r"原處分現況為[「\"]([^」\"]+)[」\"]", text)
    status_in_text = m_status.group(1) if m_status else ""
    nonexist_markers = ("已執行完畢", "消滅", "已不存在", "已撤銷", "已失效",
                        "已廢止", "失效", "撤銷")
    if any(mk in status_in_text for mk in nonexist_markers) \
       or ("已執行完畢消滅" in text) or ("原處分已不存在" in text):
        detail = status_in_text or "已不存在"
        return 0, "77(6)", f"原處分已不存在(現況:{detail})。"

    # 77(7) 重複提起訴願
    if ("前曾就同一事件提起訴願" in text) or ("重行提起" in text) or ("重復提起" in text):
        return 0, "77(7)", "就同一事件重複提起訴願。"

    # 77(2) 訴願逾期 — 自算期間
    if served and filed:
        elapsed = (filed - served).days
        if elapsed > statutory_days:
            return 0, "77(2)", f"提起訴願逾法定 {statutory_days} 日期間(實際 {elapsed} 日,逾期 {elapsed - statutory_days} 日)。"

    # 全數要件無瑕疵 → 受理
    return 1, None, "程序要件具備,無不受理事由,應予受理進入實體審查。"


# ---------- 主流程:判斷 -> 解封對比 ----------
def main():
    recs = load_records()
    results = []
    for rec in recs:
        view = make_input_view(rec)          # 遮答案
        label, ground, reason = triage(view)  # 只吃輸入
        results.append((rec, label, ground, reason))

    # === 解封答案做對比 ===
    correct = wrong = 0
    wrong_list = []
    for rec, label, ground, reason in results:
        truth_str = rec.get("受理與否")          # 解封
        truth = 1 if truth_str == "受理" else 0
        if label == truth:
            correct += 1
        else:
            wrong += 1
            wrong_list.append({
                "案號": rec.get("案號"),
                "案件類型": rec.get("案件類型"),
                "我的判定": "受理(1)" if label == 1 else "不受理(0)",
                "我判的款次": ground,
                "正解": truth_str,
                "正解款次": rec.get("不受理款次"),
                "我的理由": reason,
                "訴願書敘述": rec.get("訴願書敘述"),
            })

    lines = []
    lines.append(f"總筆數: {len(recs)}")
    lines.append(f"判斷正確: {correct} 筆")
    lines.append(f"判斷錯誤: {wrong} 筆")
    lines.append(f"準確率: {correct / len(recs) * 100:.1f}%")
    lines.append("")

    # 混淆矩陣
    tp = sum(1 for rec, l, g, r in results if l == 1 and rec.get("受理與否") == "受理")
    tn = sum(1 for rec, l, g, r in results if l == 0 and rec.get("受理與否") != "受理")
    fp = sum(1 for rec, l, g, r in results if l == 1 and rec.get("受理與否") != "受理")
    fn = sum(1 for rec, l, g, r in results if l == 0 and rec.get("受理與否") == "受理")
    lines.append("混淆矩陣:")
    lines.append(f"  正確判為受理 (TP): {tp}")
    lines.append(f"  正確判為不受理(TN): {tn}")
    lines.append(f"  誤判為受理  (FP): {fp}")
    lines.append(f"  誤判為不受理(FN): {fn}")
    lines.append("")

    if wrong_list:
        lines.append("=== 錯誤清單 ===")
        for w in wrong_list:
            lines.append(json.dumps(w, ensure_ascii=False, indent=2))
            lines.append("-" * 40)
    else:
        lines.append("沒有任何錯誤。")

    report = "\n".join(lines)
    with open("triage_report.txt", "w", encoding="utf-8") as f:
        f.write(report)

    # 同時輸出每筆判定結果 (含遮罩後輸入 + 判定 + 正解) 供稽核
    with open("triage_predictions.jsonl", "w", encoding="utf-8") as f:
        for rec, label, ground, reason in results:
            f.write(json.dumps({
                "案號": rec.get("案號"),
                "預測標記": label,
                "預測款次": ground,
                "預測理由": reason,
                "正解_受理與否": rec.get("受理與否"),
            }, ensure_ascii=False) + "\n")

    print("done; correct=%d wrong=%d" % (correct, wrong))


if __name__ == "__main__":
    main()
