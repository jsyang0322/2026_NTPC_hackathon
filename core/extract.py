"""F1 結構化擷取（§5.2）。[正則 + LLM]

兩層擷取：
  1. 正則層（純 Python，0 呼叫）：抓格式固定欄位——字號、罰鍰金額、各類日期。
  2. LLM 層（1 次 Haiku 呼叫）：抓需理解的欄位——訴願人、主張拆項、請求、機關認定事實。

輸出對齊 schemas.build_empty_input() 的 extracted_fields 形狀，供 pipeline 直接使用。
LLM 呼叫透過 bedrock_client（受 ≤1 RPS 限流）。合併為一次呼叫以省請求數（§13.3）。
"""

from __future__ import annotations

import json
import re

from .bedrock_client import BedrockClient, get_client, MODEL_LIGHT
from . import timeline as _tl


# ---------- 正則層 ----------
# 公文字號：如「北府經司字第1140012345號」「環署廢字第1130001號」
# 機關代字段限 2–8 字，且不跨越標點/空白（避免把「依」「按」等前導字一起吃進來）。
_DOC_NO_RE = re.compile(r"([\u4e00-\u9fff]{2,8}字第\s*[\dA-Za-z]+\s*號)")
# 常見於字號前、非機關代字一部分的前導字（連接詞、時間詞尾），命中時逐一剝除。
_DOC_NO_LEADING = ("依", "按", "以", "經", "爰", "查", "就", "對", "為", "之", "日", "號", "起")
# 罰鍰金額：「處新臺幣3,000元」「罰鍰6萬元」「裁處10,000元罰鍰」
_AMOUNT_RE = re.compile(r"(?:新臺幣|新台幣|罰鍰)?\s*([\d,]+)\s*(萬)?\s*元")
# 日期（民國/西元），沿用 timeline 的樣式
_DATE_TOKEN = re.compile(
    r"(?:民國)?\s*\d{2,4}\s*[年./\-]\s*\d{1,2}\s*[月./\-]\s*\d{1,2}\s*日?")


def _find_amount(text: str) -> int | None:
    """抓罰鍰金額（元）。取第一個像罰鍰的數字，含「萬」換算。"""
    for m in _AMOUNT_RE.finditer(text):
        num = m.group(1).replace(",", "")
        if not num.isdigit():
            continue
        val = int(num)
        if m.group(2) == "萬":
            val *= 10000
        # 過濾明顯非金額的小數字（如條號），罰鍰通常 >= 1000 或帶「萬」
        if val >= 1000 or m.group(2) == "萬":
            return val
    return None


def _find_doc_no(text: str) -> str:
    m = _DOC_NO_RE.search(text)
    if not m:
        return ""
    doc = m.group(1).replace(" ", "")
    # 剝除前導字（如「…3月1日以北府…字第…號」誤含「日以」→「北府…字第…號」）。
    # 逐一剝除首字在前導清單者，但保證「字第」前至少留 2 字機關代字。
    prefix_len = doc.index("字第")
    while prefix_len > 2 and doc[0] in _DOC_NO_LEADING:
        doc = doc[1:]
        prefix_len -= 1
    return doc


def _regex_layer(disposition_doc: str, petition: str) -> dict:
    """正則抽格式固定欄位，回傳部分 original_disposition + 提起日候選。"""
    od: dict = {}
    doc_no = _find_doc_no(disposition_doc)
    if doc_no:
        od["doc_no"] = doc_no
    amount = _find_amount(disposition_doc)
    if amount is not None:
        od["penalty_amount"] = amount
    return od


# ---------- LLM 層 ----------
_SYSTEM = (
    "你是行政法訴願案件的資料擷取助理。從提供的訴願書與原處分書文字中，"
    "擷取結構化欄位。只依據文字內容，不得臆測或杜撰；找不到的欄位留空字串或空陣列。"
    "日期一律以原文格式輸出（如「民國114年3月5日」）。只輸出 JSON，不要多餘文字。"
)


def _build_prompt(petition: str, disposition_doc: str) -> str:
    return (
        "【訴願書】\n" + (petition or "（無）") + "\n\n"
        "【原處分書】\n" + (disposition_doc or "（無）") + "\n\n"
        "請擷取並輸出以下 JSON（僅 JSON）：\n"
        "{\n"
        '  "petitioner": {"name": "訴願人姓名或名稱", "address": "地址", "agent": "代理人"},\n'
        '  "original_disposition": {"agency": "原處分機關", "date": "處分作成日期",\n'
        '     "legal_basis": ["所引法條，如 洗錢防制法第15條之2"],\n'
        '     "service_date": "處分送達訴願人之日期", "act_date": "違規行為發生日期"},\n'
        '  "petition_filed_date": "提起訴願日期",\n'
        '  "requests": ["訴願人聲明請求，如 撤銷原處分"],\n'
        '  "claims": [{"id": "C1", "summary": "主張要旨（一句）", "quote": "原文摘句"}],\n'
        '  "evidence_list": ["證據名稱"]\n'
        "}\n"
        "規則：claims 逐項拆分並編號 C1, C2...；找不到的欄位給空字串或空陣列。"
    )


def _parse_llm(text: str) -> dict:
    """解析 LLM 回應；容錯 ```json 圍籬與 dry_run 非 JSON。"""
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    raw = m.group(1) if m else text.strip()
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, AttributeError):
        return {}


def _empty_fields() -> dict:
    return {
        "petitioner": {"name": "", "address": "", "agent": ""},
        "original_disposition": {
            "agency": "", "date": "", "doc_no": "",
            "legal_basis": [], "penalty_amount": 0,
            "service_date": "", "act_date": "",
        },
        "petition_filed_date": "",
        "requests": [],
        "claims": [],
        "evidence_list": [],
    }


def _normalize_claims(claims) -> list[dict]:
    """確保 claims 為 [{id, summary, quote}]，補齊編號。"""
    out: list[dict] = []
    if not isinstance(claims, list):
        return out
    for i, c in enumerate(claims, 1):
        if isinstance(c, dict):
            out.append({
                "id": c.get("id") or f"C{i}",
                "summary": c.get("summary", ""),
                "quote": c.get("quote", ""),
            })
        elif isinstance(c, str):
            out.append({"id": f"C{i}", "summary": c, "quote": ""})
    return out


def extract_fields(case_text: dict, client: BedrockClient | None = None) -> dict:
    """從案卷文字擷取結構化欄位。

    輸入 case_text（相容兩種 key 命名）：
      petition               訴願書全文
      disposition / original_disposition_doc  原處分書全文
    輸出：schemas 的 extracted_fields 形狀純 dict（含來源標記 _source）。
    """
    client = client or get_client()
    petition = case_text.get("petition", "") or ""
    disposition_doc = case_text.get("disposition") or case_text.get("original_disposition_doc", "") or ""

    fields = _empty_fields()

    # 1) 正則層（0 呼叫）：字號、金額
    reg = _regex_layer(disposition_doc, petition)
    fields["original_disposition"].update(reg)

    # 2) LLM 層（1 次呼叫）：理解型欄位
    prompt = _build_prompt(petition, disposition_doc)
    text = client.converse(
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        model_id=MODEL_LIGHT,
        system=_SYSTEM,
        temperature=0.0,
    )
    data = _parse_llm(text)

    # 合併 LLM 結果（正則抓到的 doc_no/penalty_amount 優先保留）
    if isinstance(data.get("petitioner"), dict):
        fields["petitioner"].update({k: v for k, v in data["petitioner"].items() if v})
    if isinstance(data.get("original_disposition"), dict):
        for k, v in data["original_disposition"].items():
            if k in ("doc_no", "penalty_amount") and fields["original_disposition"].get(k):
                continue  # 正則層已抓到，優先採用（較可靠）
            if v:
                fields["original_disposition"][k] = v
    for key in ("petition_filed_date",):
        if data.get(key):
            fields[key] = data[key]
    if isinstance(data.get("requests"), list):
        fields["requests"] = [r for r in data["requests"] if r]
    fields["claims"] = _normalize_claims(data.get("claims"))
    if isinstance(data.get("evidence_list"), list):
        fields["evidence_list"] = [e for e in data["evidence_list"] if e]

    # 正規化日期為可解析格式（保留原字串，另存解析結果供追溯）
    fields["_source"] = "regex+llm"
    return fields
