# -*- coding: utf-8 -*-
"""
訴願書自動判別作業流 (Appeal Triage Workflow)
======================================================================
輸入: S3 上的訴願書 PDF
輸出: 每份訴願書一個 JSON 檔,內容為整份訴願書結構化資料 + 兩個分類註記欄位

作業流步驟:
  [1] 取件      從 S3 下載 PDF 並抽取全文
  [2] 結構化    將 PDF 內容解析成 JSON 欄位(訴願人、機關、文號、日期、事實、理由...)
  [3] 受理判定  兩段式 RAG 判定 (Nova Lite 初判 → 受理/低信心者交 Claude 覆核)
                  → 寫入註記欄位一 `受理註記`: 0=不受理, 1=受理
  [4] 類別判定  僅當欄位一為 1 時才判類別;為 0 時欄位二直接為 0
                  → 寫入註記欄位二 `類別註記`:
                       0 = 不受理(不分類)
                       1 = 空氣污染防制法
                       2 = 洗錢防制法
                       3 = 廢棄物清理法
                       4 = 其餘案件
  [5] 輸出      寫出 <案號>.json

用法:
  python workflow_appeal.py                    # 跑 測試訴願書_pdf/ 全部
  python workflow_appeal.py TEST001 TEST002    # 只跑指定案號
"""
import io
import os
import re
import json
import sys
import time
from pathlib import Path

import boto3
from botocore.config import Config
from pypdf import PdfReader

import rag_triage as R
import pipeline_two_stage as TS

PDF_PREFIX = os.environ.get("APPEAL_TEST_PDF_PREFIX", "測試訴願書_pdf/")
OUT_DIR = Path(os.environ.get("APPEAL_OUTPUT_DIR", Path(__file__).resolve().parent / "output_json"))

# ---- 類別代碼 ----
CAT_NOT_ACCEPTED = 0      # 不受理
CAT_AIR = 1               # 空氣污染防制法
CAT_MONEY_LAUNDERING = 2  # 洗錢防制法
CAT_WASTE = 3             # 廢棄物清理法
CAT_OTHER = 4             # 其餘案件

CAT_NAME = {
    0: "不受理(不分類)",
    1: "空氣污染防制法",
    2: "洗錢防制法",
    3: "廢棄物清理法",
    4: "其餘案件",
}


# ==================== [1] 取件 ====================
def list_pdf_case_ids(prefix=PDF_PREFIX):
    ids = []
    for page in R.s3.get_paginator("list_objects_v2").paginate(Bucket=R.BUCKET, Prefix=prefix):
        for o in page.get("Contents", []):
            if o["Key"].lower().endswith(".pdf"):
                ids.append(o["Key"].split("/")[-1].rsplit(".", 1)[0])
    return sorted(ids)


def fetch_pdf_text(case_id, prefix=PDF_PREFIX):
    key = f"{prefix}{case_id}.pdf"
    raw = R.s3.get_object(Bucket=R.BUCKET, Key=key)["Body"].read()
    reader = PdfReader(io.BytesIO(raw))
    pages = [(p.extract_text() or "") for p in reader.pages]
    return "\n".join(pages), pages, key


# ==================== [2] 結構化 ====================
def _flat(t):
    return re.sub(r"\s+", "", t or "")


def _find(pattern, text, group=1, default=None):
    m = re.search(pattern, text)
    return m.group(group) if m else default


def _roc_parts(s):
    m = re.search(r"民國(\d+)年(\d+)月(\d+)日", s or "")
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def compute_age(birth_roc, at_roc):
    """以民國年月日計算在 at_roc 當日的實歲。回傳 int 或 None。"""
    b, a = _roc_parts(birth_roc), _roc_parts(at_roc)
    if not b or not a:
        return None
    age = a[0] - b[0]
    if (a[1], a[2]) < (b[1], b[2]):
        age -= 1
    return age


def structure_appeal(full_text, pages):
    """把訴願書 PDF 文字解析成結構化 JSON 欄位。抓不到的欄位為 None。"""
    flat = _flat(full_text)

    doc = {}
    doc["訴願人"] = _find(r"訴願人\s*\n?\s*([^\n]{1,30}?)(?:\n|民國|（)", full_text)
    doc["原行政處分機關"] = _find(r"原行政處分機關\s*\n\s*([^\n]+)", full_text)
    doc["受理訴願機關"] = _find(r"受理訴願機關\s*\n\s*([^\n]+)", full_text)

    # 處分書文號與發文日
    doc["處分書文號"] = _find(r"((?:新北[^\s]{0,6}字第\s*\d+\s*號))", flat)
    doc["處分發文日"] = _find(r"發\s*文\s*日\s*[:：]?\s*(民國\d+年\d+月\d+日)", flat)
    # 收受知悉日
    doc["收受處分日"] = _find(r"收受知悉行政處分之年月日\s*(民國\d+年\d+月\d+日)", flat)
    # 提起訴願日(文末中華民國 X 年 X 月 X 日)
    y = re.search(r"中華民國(\d+)年(\d+)月(\d+)日", flat)
    doc["提起訴願日"] = f"民國{y.group(1)}年{y.group(2)}月{y.group(3)}日" if y else None

    # 出生年月日(法人無此欄位) + 以程式精算提起訴願時之實歲。
    # 訴願能力屬可客觀計算之要件,不交由 LLM 心算,避免誤判 77(4)。
    doc["出生年月日"] = _find(r"(民國\d+年\d+月\d+日)", _flat(
        full_text[:full_text.find("原行政處分機關")] if "原行政處分機關" in full_text else full_text[:600]))
    doc["提起訴願時年齡"] = compute_age(doc["出生年月日"], doc["提起訴願日"])
    doc["是否成年"] = (None if doc["提起訴願時年齡"] is None
                    else doc["提起訴願時年齡"] >= 18)
    doc["是否法人"] = ("統一編號" in flat) or ("股份有限公司" in flat) or ("有限公司" in flat)

    doc["訴願請求事項"] = _find(r"訴願請求事項[:：]\s*\n?\s*([^\n]+)", full_text)

    # 事實 / 理由 段落
    def section(start_kw, end_kws):
        i = full_text.find(start_kw)
        if i < 0:
            return None
        j = len(full_text)
        for e in end_kws:
            k = full_text.find(e, i + len(start_kw))
            if k > 0:
                j = min(j, k)
        return full_text[i + len(start_kw):j].strip()

    doc["事實"] = section("事實：", ["理由：", "檢附之證據"])
    doc["理由"] = section("理由：", ["檢附之證據", "此致"])
    doc["檢附證據"] = section("檢附之證據或附件：", ["此致"])

    doc["頁數"] = len(pages)
    doc["全文"] = full_text
    return doc


# ==================== [4] 類別判定 ====================
CATEGORY_SYSTEM = (
    "你是訴願案件分類助理。請根據訴願書內容,判斷本案所涉及的『主要違反法規』屬於下列哪一類。"
    "只依客觀事實(處分所依據的法規、處分事由)判斷,不受訴願人主張影響。"
)

CATEGORY_PROMPT = """以下為訴願書內容:
---訴願書開始---
{doc}
---訴願書結束---

請判斷本案主要涉及的法規類別,只輸出一個 JSON 物件(不要多餘文字):
{{"類別": "空氣污染防制法" 或 "洗錢防制法" 或 "廢棄物清理法" 或 "其餘案件", "依據": "簡要說明依何法條/事由判斷"}}

判斷原則:
- 若處分依據為空氣污染防制法(如排放標準、固定污染源、粒狀污染物)→ 空氣污染防制法
- 若處分依據為洗錢防制法(如提供帳戶、書面告誡、洗錢防制法第22條)→ 洗錢防制法
- 若處分依據為廢棄物清理法(如任意棄置垃圾、菸蒂、廢棄物清理法第27條)→ 廢棄物清理法
- 其他一切法規(建築法、噪音管制法、停車場法、都市更新、稅務等)→ 其餘案件
"""

# 關鍵詞備援(LLM 失敗時使用)
KEYWORD_RULES = [
    (CAT_AIR, ["空氣污染防制法", "空氣污染物排放", "固定污染源", "排放標準", "新北環空字"]),
    (CAT_MONEY_LAUNDERING, ["洗錢防制法", "書面告誡", "帳戶", "警刑字"]),
    (CAT_WASTE, ["廢棄物清理法", "廢棄物", "菸蒂", "棄置", "環稽字"]),
]


def classify_by_keyword(full_text):
    """關鍵詞備援分類。回傳 (代碼, 依據說明)。"""
    flat = _flat(full_text)
    best, best_hits = CAT_OTHER, []
    best_score = 0
    for code, kws in KEYWORD_RULES:
        hits = [k for k in kws if k in flat]
        # 法規全名命中權重最高
        score = len(hits) + (5 if kws[0] in flat else 0)
        if score > best_score:
            best, best_hits, best_score = code, hits, score
    if best_score == 0:
        return CAT_OTHER, "未命中三類法規關鍵詞,歸為其餘案件"
    return best, "關鍵詞命中: " + ", ".join(best_hits)


def classify_category(full_text):
    """用 LLM 判類別,失敗則退回關鍵詞。回傳 (代碼, 依據)。"""
    TS._set_model(TS.NOVA_MODEL)
    try:
        raw = R.nova(CATEGORY_PROMPT.format(doc=full_text[:6000]),
                     system=CATEGORY_SYSTEM, max_tokens=300)
        obj = R.parse_json_loose(raw)
        if obj and obj.get("類別"):
            name = obj["類別"]
            code = {"空氣污染防制法": CAT_AIR,
                    "洗錢防制法": CAT_MONEY_LAUNDERING,
                    "廢棄物清理法": CAT_WASTE,
                    "其餘案件": CAT_OTHER}.get(name)
            if code is not None:
                return code, f"LLM判定:{name};{obj.get('依據','')}"
    except Exception as e:
        pass
    # 備援
    return classify_by_keyword(full_text)


# ==================== 主作業流 ====================
def run_case(case_id, chunks, mat):
    """對單一訴願書跑完整作業流,回傳輸出用的 dict。"""
    print(f"\n=== {case_id} ===", flush=True)

    # [1] 取件
    full_text, pages, key = fetch_pdf_text(case_id)
    print(f"[1/5] 取件完成: {key} ({len(pages)}頁, {len(full_text)}字)", flush=True)

    # [2] 結構化
    doc = structure_appeal(full_text, pages)
    print(f"[2/5] 結構化完成: 訴願人={doc.get('訴願人')} 收受={doc.get('收受處分日')} 提起={doc.get('提起訴願日')}", flush=True)

    # [3] 受理判定(兩段式)
    TS._set_model(TS.NOVA_MODEL)
    n_label, n_ground, n_reason, _, n_conf = TS._call_with_retry(full_text, chunks, mat)
    stage1 = {"模型": "nova-lite", "受理與否": ("受理" if n_label == 1 else "不受理") if n_label is not None else None,
              "款次": n_ground, "信心": n_conf, "理由": n_reason}
    reviewed = TS.needs_review(n_label, n_conf)
    stage2 = None
    final_label = n_label
    used_model = "nova-lite"
    guard_note = None
    if reviewed:
        TS._set_model(TS.CLAUDE_MODEL)
        c_label, c_ground, c_reason, _, c_conf = TS._call_with_retry(full_text, chunks, mat)
        stage2 = {"模型": "claude-sonnet-4.5",
                  "受理與否": ("受理" if c_label == 1 else "不受理") if c_label is not None else None,
                  "款次": c_ground, "信心": c_conf, "理由": c_reason}
        if c_label is not None:
            # --- 防呆:訴願能力(77(4)) 以程式計算之年齡為準 ---
            # LLM 對年齡推算易失誤,若其以無訴願能力為由不受理,但客觀上已成年
            # 或訴願人為法人,則否決該覆核結論,回歸初判。
            ground_is_capacity = (c_ground or "").replace(" ", "").startswith("77(4)")
            adult = doc.get("是否成年")
            is_corp = doc.get("是否法人")
            if c_label == 0 and ground_is_capacity and (adult is True or is_corp):
                guard_note = (
                    f"覆核以77(4)無訴願能力為由不受理,惟依訴願書所載出生年月日"
                    f"{doc.get('出生年月日')}計算,提起訴願時為{doc.get('提起訴願時年齡')}歲"
                    f"{'(已成年)' if adult else ''}{'/訴願人為法人' if is_corp else ''},"
                    f"訴願能力無欠缺,故不採用該覆核結論,回歸初判。"
                )
                final_label = n_label
                used_model = "nova-lite(覆核結論經防呆否決)"
            else:
                final_label = c_label
                used_model = "claude-sonnet-4.5"
        else:
            used_model = "nova-lite(claude覆核失敗退回)"
    print(f"[3/5] 受理判定: 初判={n_label}({n_conf}) 覆核={'是' if reviewed else '否'} 最終={final_label}", flush=True)

    # [4] 類別判定 — 只有受理(1)才分類
    if final_label == 1:
        cat_code, cat_basis = classify_category(full_text)
    else:
        cat_code, cat_basis = CAT_NOT_ACCEPTED, "不受理案件,類別註記為0"
    print(f"[4/5] 類別判定: {cat_code} ({CAT_NAME[cat_code]}) — {cat_basis[:60]}", flush=True)

    # [5] 組裝輸出
    # 最終款次/理由須與最終採用的判定一致(防呆否決時回歸初判)。
    adopted = stage1 if (guard_note or not stage2) else stage2
    ground = adopted.get("款次")
    reason = adopted.get("理由")
    if guard_note:
        reason = f"{reason}【防呆】{guard_note}"
    result = {
        "案號": case_id,
        "來源PDF": key,
        # ---- 兩個分類註記欄位 ----
        "受理註記": final_label,          # 欄位一: 0=不受理 1=受理
        "類別註記": cat_code,             # 欄位二: 0=不受理 1=空污 2=洗錢 3=廢清 4=其餘
        "受理註記說明": "受理" if final_label == 1 else "不受理",
        "類別註記說明": CAT_NAME[cat_code],
        # ---- 判定過程 ----
        "判定": {
            "不受理款次": ground,
            "判定理由": reason,
            "採用模型": used_model,
            "是否經Claude覆核": reviewed,
            "第一段_初判": stage1,
            "第二段_覆核": stage2,
            "防呆否決說明": guard_note,
            "類別判定依據": cat_basis,
        },
        # ---- 訴願書結構化內容 ----
        "訴願書": doc,
    }
    return result


def main(case_ids=None):
    os.makedirs(OUT_DIR, exist_ok=True)
    chunks, mat = R.load_kb()
    ids = case_ids or list_pdf_case_ids()
    print(f"待處理案件: {ids}", flush=True)

    summary = []
    for cid in ids:
        try:
            res = run_case(cid, chunks, mat)
        except Exception as e:
            print(f"!! {cid} 失敗: {e!r}", flush=True)
            continue
        path = os.path.join(OUT_DIR, f"{cid}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False, indent=2)
        print(f"[5/5] 已輸出 {path}", flush=True)
        summary.append((cid, res["受理註記"], res["類別註記"], res["類別註記說明"]))

    print("\n===== 作業流完成 =====", flush=True)
    lines = ["案號\t受理註記\t類別註記\t類別"]
    for cid, a, b, name in summary:
        lines.append(f"{cid}\t{a}\t{b}\t{name}")
    report = "\n".join(lines)
    print(report, flush=True)
    with open(os.path.join(OUT_DIR, "_summary.txt"), "w", encoding="utf-8") as f:
        f.write(report)


if __name__ == "__main__":
    args = sys.argv[1:]
    main(args if args else None)
