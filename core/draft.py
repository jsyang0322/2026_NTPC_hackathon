"""F4 決定書草稿生成（§10）。[LLM]

定案設計（鎖定，勿改為 Bedrock Agents）：
  - 「案由專責」= 1 顆 Claude（MODEL_WRITER）+ 依 route_key 切換的 prompt / KB 檢索範圍。
  - 「寫草稿用 Bedrock」= generate_draft() 組好 prompt 後，一次 client.converse 呼叫 Claude。
  - route_key 為英文枚舉（schemas.ROUTE_KEYS），非中文，避免名稱不一致。

v1.3 起流程全自動：主文由 pipeline._decide_disposition() 自動判定後傳入，
本模組只負責「依既定主文寫理由」，不自行變更結論方向（主文理由一致性由 verify V9 把關）。

兩種模式：
  - 初次生成：generate_draft(...)
  - 重寫模式：generate_draft(..., prev_draft=上一版, feedback=審查意見)
    由對抗式審查迴圈觸發，僅修正被指出的段落。

單件呼叫預算（§5）：KB 檢索 1 + 撰寫 1 + 法官審查 1 = 3 次；
觸發重寫時 +2（重寫 1 + 再審 1），最壞 5 次，仍守 ≤1 RPS 與 §13.3 上限。
"""

from __future__ import annotations

import json
import re as _re

from .bedrock_client import BedrockClient, get_client, MODEL_WRITER
from .citations import extract_parsed, normalize_citation


# ---------- 案由專責 profile，key = route_key 英文枚舉 ----------
CASE_PROFILES: dict[str, dict] = {
    "money_laundering": {
        "label": "洗錢防制法",
        "common_issues": ["交付帳戶有無正當理由", "主觀故意過失(行政罰法§7)"],
        "writing_style": "以新北市訴願決定書用語撰寫，常用「卷查」「經查」「揆諸前揭規定」。",
    },
    "waste": {
        "label": "廢棄物清理法",
        "common_issues": ["行為人認定", "共有人連帶責任", "是否屬廢棄物"],
        "writing_style": "以新北市訴願決定書用語撰寫，常用「卷查」「經查」「揆諸前揭規定」。",
    },
    "air_pollution": {
        "label": "空氣污染防制法",
        "common_issues": ["定檢通知是否合法送達", "車輛是否已過戶或報廢"],
        "writing_style": "以新北市訴願決定書用語撰寫，常用「卷查」「經查」「揆諸前揭規定」。",
    },
    "building": {
        "label": "建築法",
        "common_issues": ["違規使用認定", "公安申報義務", "簽證內容不實"],
        "writing_style": "以新北市訴願決定書用語撰寫，常用「卷查」「經查」「揆諸前揭規定」。",
    },
    "noise": {
        "label": "噪音管制法",
        "common_issues": ["檢測程序是否合法", "行為人認定", "超標認定"],
        "writing_style": "以新北市訴願決定書用語撰寫，常用「卷查」「經查」「揆諸前揭規定」。",
    },
    "general": {
        "label": "通用",
        "common_issues": ["從新從輕(行政罰法§5)", "權利保護必要", "行政程序再開"],
        "writing_style": "以新北市訴願決定書用語撰寫，常用「卷查」「經查」「揆諸前揭規定」。",
    },
}


def get_profile(route_key: str | None) -> dict:
    """依 route_key 取專責 profile；未知退回 general。"""
    return CASE_PROFILES.get(route_key or "", CASE_PROFILES["general"])


#: 主文枚舉（LLM 須從中擇一）。與 schemas / pipeline 的三種主文一致。
_ALLOWED_DISPOSITIONS = ("訴願駁回", "原處分撤銷，另為適法之處分", "訴願不受理")


def build_draft_prompt(disposition_hint: str, fields: dict, recommended_laws: list[dict],
                       similar_cases: list[dict], defects: list[dict], profile: dict,
                       prev_draft: dict | None = None, feedback: str = "",
                       reference_note: str = "") -> str:
    """組 §10.5 的 prompt。KB 檢索到的法條/案例由此塞入。

    v2：主文改由 LLM 依事實、可引用法條、相似案例、健檢結果自行判斷（撤銷/駁回/不受理），
    多數決與健檢僅作為「參考」，不再強制結論方向。disposition_hint 為系統參考建議。

    重寫模式（prev_draft + feedback）：帶入上一版與規則/法官意見，要求僅針對問題修正，
    保留其餘正確段落，避免整篇重寫又引入新問題（重寫時沿用上一版已定的主文，維持一致）。
    """
    base = (
        "你是新北市政府訴願審議委員會的審議輔助人員。"
        f"{profile['writing_style']}\n"
        "請依下列資料，先判斷本案主文，再撰寫理由欄「涵攝與逐項回應」段落。\n\n"
        f"【本案常見爭點】{profile['common_issues']}\n"
        f"【案件事實】{json.dumps(fields, ensure_ascii=False)}\n"
        f"【可引用法條】{json.dumps(recommended_laws, ensure_ascii=False)}（只能引用此清單）\n"
        f"【相似案例論理（參考，非結論）】{json.dumps(similar_cases, ensure_ascii=False)}\n"
        f"【原處分健檢結果（參考）】{json.dumps(defects, ensure_ascii=False)}（紅燈項須在理由中處理）\n"
    )
    if reference_note:
        base += f"【系統參考建議】{reference_note}（僅供參考，請以本案事實與法律判斷為準）\n"
    base += "\n"

    if prev_draft and feedback:
        # 重寫模式：沿用上一版主文，只修被指出的問題
        base += (
            f"【本案主文】{disposition_hint}（沿用上一版，重寫時不變更結論方向）\n"
            "【上一版理由草稿】"
            + json.dumps(prev_draft.get("reasons", []), ensure_ascii=False) + "\n"
            "【審查意見（規則檢查與法官提出的問題）】\n" + feedback + "\n\n"
            "重寫規則：僅針對上述審查意見修正對應段落，保留其餘正確段落的論理與結構，"
            "不要引入新的問題，也不要變更主文方向。\n"
            "撰寫規則：\n"
            "1. 逐一回應每項訴願人主張並標注編號。\n"
            "2. 不得引用【可引用法條】以外的法條、函釋或判決字號。\n"
            "3. 不得新增案件事實中沒有的內容。\n"
            f'4. 輸出 JSON：{{"disposition":"{disposition_hint}",'
            '"paragraphs":[{"text":"...","responds_to":["C1"],'
            '"cites":["洗錢防制法§22"],"based_on_case":"doc_id"}]}\n'
        )
        return base

    # 初次生成：由 LLM 判斷主文
    base += (
        "判斷主文規則：\n"
        f"- 主文必須從下列三者擇一：{list(_ALLOWED_DISPOSITIONS)}。\n"
        "- 若原處分於認定事實、適用法令、程序或裁量有違法或不當（含健檢紅燈）→"
        "「原處分撤銷，另為適法之處分」。\n"
        "- 若原處分於法無違誤、訴願人主張無理由 →「訴願駁回」。\n"
        "- 若屬程序上不應受理（如逾期、非行政處分）→「訴願不受理」。\n"
        "- 依本案事實與可引用法條實質判斷，相似案例與健檢僅供參考，勿盲從多數。\n\n"
        "撰寫規則：\n"
        "1. 理由結論方向必須與你判斷的主文一致。\n"
        "2. 逐一回應每項訴願人主張並標注編號。\n"
        "3. 不得引用【可引用法條】以外的法條、函釋或判決字號。\n"
        "4. 不得新增案件事實中沒有的內容。\n"
        '5. 輸出 JSON：{"disposition":"你判斷的主文（三選一）",'
        '"paragraphs":[{"text":"...","responds_to":["C1"],'
        '"cites":["洗錢防制法§22"],"based_on_case":"doc_id"}]}\n'
    )
    return base


def generate_draft(
    route_key: str,
    disposition: str,
    fields: dict,
    recommended_laws: list[dict],
    similar_cases: list[dict],
    defects: list[dict],
    client: BedrockClient | None = None,
    prev_draft: dict | None = None,
    feedback: str = "",
    reference_note: str = "",
) -> dict:
    """依 route_key 生成草稿，主文由 LLM 判斷。★ 一次 client.converse 呼叫 Claude。

    disposition 參數為「系統參考主文」（多數決/健檢），初次生成時僅作 fallback；
    reference_note 為給 LLM 的參考說明。重寫模式（prev_draft + feedback）沿用 disposition。
    """
    client = client or get_client()
    profile = get_profile(route_key)
    prompt = build_draft_prompt(
        disposition, fields, recommended_laws, similar_cases, defects, profile,
        prev_draft=prev_draft, feedback=feedback, reference_note=reference_note,
    )
    text = client.converse(
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        model_id=MODEL_WRITER,
        temperature=0.2,
    )
    # 初次生成：主文由 LLM 判斷（自回應解析）；重寫模式：沿用傳入主文，維持一致。
    is_rewrite = bool(prev_draft and feedback)
    if is_rewrite:
        used_disposition = disposition
    else:
        used_disposition = _parse_disposition(text) or disposition
    return {
        "main": used_disposition,
        "disposition": used_disposition,
        "decided_by": "llm" if not is_rewrite else "llm(rewrite)",
        "facts": {},
        "reasons": _parse_reasons(text),
        "remedy_notice": build_remedy_notice(used_disposition),   # 教示（§90），純規則
        "source": "llm",
        "route_key": route_key,
        "profile_label": profile["label"],
        "revised": is_rewrite,
    }


def _parse_disposition(text: str) -> str | None:
    """從 LLM 回應取出其判斷的主文，正規化為三種枚舉之一。

    優先讀 JSON 的 disposition 欄位；容錯處理自由文字（含關鍵字）。
    無法辨識回 None（呼叫端會 fallback 到系統參考主文）。
    """
    raw = ""
    cleaned = _strip_code_fence(text)
    try:
        raw = str(json.loads(cleaned).get("disposition", "") or "")
    except (json.JSONDecodeError, AttributeError, TypeError):
        raw = ""
    hay = raw or text
    # 先判不受理（避免「不受理」被「受理」誤判），再撤銷，再駁回
    if "不受理" in hay:
        return "訴願不受理"
    if "撤銷" in hay:
        return "原處分撤銷，另為適法之處分"
    if "駁回" in hay:
        return "訴願駁回"
    return None


# 接續條號：「…第27條及第50條」「第9條、第11條」——同一法名下以「及/、/與/或/,」
# 銜接的後續條號。citations.CITE_RE 只抓「法名+條號」相鄰者，會漏掉接續條，
# 故於 draft 補一道接續掃描（只在 _reconcile_cites 用，不動共用的 citations）。
_FOLLOW_CITE_RE = _re.compile(
    r"[及與或、,，和]\s*第\s*(\d+)\s*條(?:\s*之\s*(\d+))?"
)


def _follow_on_citations(text: str) -> list[str]:
    """補抽同法接續條號。以 citations 抽出的引用為錨，法名沿用最近一個。"""
    out: list[str] = []
    parsed = extract_parsed(text)
    if not parsed:
        return out
    # 以每個主引用的法名為基準，掃描其後的接續條號
    for m in _FOLLOW_CITE_RE.finditer(text):
        # 找此接續條號之前最近出現的、已解析的法名
        law = None
        pos = m.start()
        for p in extract_parsed(text[:pos]):
            law = p["law"]
        if not law:
            law = parsed[0]["law"]
        art = int(m.group(1))
        sub = m.group(2)
        cite = f"{law}第{art}條" + (f"之{int(sub)}" if sub else "")
        out.append(cite)
    return out


# ---------- 教示（remedy_notice）：純規則決定表，依主文推導，不呼叫 LLM ----------
# 訴願法§90：決定書應附記如不服決定得於送達次日起 2 個月內提起行政訴訟。
# 行政訴訟新制（112.8.15）：第一審向「高等行政法院地方行政訴訟庭」提起。
# 新北市轄區之行政訴訟第一審由臺北高等行政法院地方行政訴訟庭管轄。
# 內容須通過 verify V8：含「行政訴訟」「2個月」，且不得引用已改制/裁撤法院名稱。
_REMEDY_COURT = "臺北高等行政法院地方行政訴訟庭"

_REMEDY_STANDARD = (
    "訴願人如不服本決定，得於本決定書送達之次日起2個月內，"
    f"向{_REMEDY_COURT}提起行政訴訟。"
)


def build_remedy_notice(disposition: str) -> str:
    """依主文產生教示文字（純規則）。

    不論駁回、原處分撤銷或不受理，救濟途徑均為行政訴訟、期間 2 個月，
    故用同一標準教示；保留此函式以便日後依主文細分（如部分撤銷之特別教示）。
    """
    # 目前三種主文的教示一致；保留 disposition 參數供未來細分
    _ = disposition
    return _REMEDY_STANDARD


def _reconcile_cites(paragraph: dict) -> None:
    """調和單一段落的 cites：合併 LLM 標註與內文正則補抽，正規化去重（就地修改）。

    LLM 常在內文寫了法條卻漏標 cites，或以「§」等變體書寫，導致 verify V1 白名單
    比對失準。此處：
      1. 把 LLM 已標的 cites 正規化（洗錢防制法§22 → 洗錢防制法第22條）。
      2. 用 citations 正則從段落 text 補抽內文實際引用的條號。
      3. 合併去重、保留出現順序，讓「內文引用的每一條」都進 cites，
         使 V1「引用須在檢索清單內」的校驗有完整比對對象（引用可追溯）。
    """
    seen: list[str] = []

    def _add(c: str | None) -> None:
        if c and c not in seen:
            seen.append(c)

    # 1) LLM 原本標的 cites，正規化
    for raw in paragraph.get("cites", []) or []:
        _add(normalize_citation(str(raw)) or str(raw).strip())

    # 2) 從內文正則補抽（保序）
    text = paragraph.get("text", "")
    for parts in extract_parsed(text):
        _add(parts["citation"])

    # 3) 補抽同法接續條號（「…第27條及第50條」的第50條）
    for cite in _follow_on_citations(text):
        _add(cite)

    paragraph["cites"] = seen


def _parse_reasons(text: str) -> list[dict]:
    """解析模型回傳的段落 JSON。容錯處理 ```json 圍籬與前後雜訊。

    解析後對每段調和 cites（LLM 標註 + 內文正則補抽），確保引用可追溯。
    """
    cleaned = _strip_code_fence(text)
    try:
        paragraphs = json.loads(cleaned).get("paragraphs", [])
        for p in paragraphs:
            p.setdefault("source", "llm")
            _reconcile_cites(p)
        return paragraphs
    except (json.JSONDecodeError, AttributeError):
        para = {"text": text, "responds_to": [], "cites": [], "based_on_case": "", "source": "llm"}
        _reconcile_cites(para)
        return [para]


def _strip_code_fence(text: str) -> str:
    """去除 ```json ... ``` 圍籬與前後空白（Claude 常這樣包 JSON）。"""
    import re
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if m:
        return m.group(1)
    return text.strip()


import re as _re


# ---------- 不受理理由模板（訴願法§77 各款，純模板，0 呼叫）----------
# 每款：法條依據（寫死、可被 verify 條號校驗覆蓋）+ 理由文字模板。
# {ded}=提起訴願日等填空；缺值時模板改用保守措辭，不編造具體日期。
_CLAUSE_TEMPLATES: dict[str, dict] = {
    "1": {
        "cites": ["訴願法第77條第1款"],
        "text": "本件訴願書不合法定程式，經通知補正而屆期未補正，"
                "揆諸訴願法第77條第1款規定，應不受理。",
    },
    "2": {
        "cites": ["訴願法第14條", "訴願法第77條第2款"],
        "text": "{overdue}揆諸訴願法第14條第1項及第77條第2款規定，應不受理。",
    },
    "3": {
        "cites": ["訴願法第18條", "訴願法第77條第3款"],
        "text": "本件訴願人非原行政處分之相對人，亦非法律上之利害關係人，"
                "當事人不適格，揆諸訴願法第18條及第77條第3款規定，應不受理。",
    },
    "4": {
        "cites": ["訴願法第77條第4款"],
        "text": "本件訴願人無訴願能力而未由法定代理人代為訴願行為，經通知補正逾期未補正，"
                "揆諸訴願法第77條第4款規定，應不受理。",
    },
    "6": {
        "cites": ["訴願法第77條第6款"],
        "text": "本件行政處分已不存在，揆諸訴願法第77條第6款規定，應不受理。",
    },
    "7": {
        "cites": ["訴願法第77條第7款"],
        "text": "本件係對已決定或已撤回之訴願事件重行提起訴願，"
                "揆諸訴願法第77條第7款規定，應不受理。",
    },
    "8": {
        "cites": ["訴願法第77條第8款"],
        "text": "本件所爭執之標的非屬行政處分，或屬依法不得提起訴願之事項，"
                "揆諸訴願法第77條第8款規定，應不受理。",
    },
}


def _normalize_clause(raw) -> str | None:
    """把款次代碼正規化為純數字字串。認 '77-2' / '77(2)' / '77（2）' / '2' 等。

    回傳 '1'..'8'，無法辨識回 None。
    """
    if raw is None:
        return None
    s = str(raw)
    # 取「77」之後的數字，或整串裡的第一個數字
    m = _re.search(r"77\s*[-(（]?\s*(\d+)", s) or _re.search(r"(\d+)", s)
    if not m:
        return None
    num = m.group(1)
    return num if num in _CLAUSE_TEMPLATES else None


def _overdue_sentence(timeline: dict) -> str:
    """組逾期（77-2）的具體事實句；日期齊全才寫具體日期，否則保守措辭。"""
    parsed = (timeline or {}).get("_parsed", {}) if isinstance(timeline, dict) else {}
    service_eff = (timeline or {}).get("service_effective_date")
    deadline = (timeline or {}).get("appeal_deadline")
    filed = (timeline or {}).get("filed_date")
    # filed_date 顯示字串在 timeline 頂層無、_parsed 為 date；用 timeline 頂層原字串
    filed_str = (timeline or {}).get("filed_date")
    if service_eff and deadline and filed_str:
        return (f"本件原行政處分於{service_eff}發生送達效力，"
                f"訴願人遲至{filed_str}始提起訴願，已逾法定30日訴願期間"
                f"（末日為{deadline}）；")
    return "本件訴願之提起已逾法定30日訴願期間；"


def quick_template(procedure: dict, timeline: dict, fields: dict | None = None) -> dict:
    """不受理快速通道（§10.4）：依訴願法§77 款次套理由模板，不呼叫 LLM（0 次 Bedrock）。

    防呆設計（選項1）：
      - 款次嚴格對映：只有§77 存在的款次（1–8，本專案資料涵蓋 1,2,3,4,6,7,8）才套模板；
        未知/無款次退通用理由並標 needs_human_review。
      - 模板法條寫死（cites），可被 verify 的條號校驗（V1/V3）覆蓋。
      - 日期缺漏時保守措辭，不編造具體日期。
      - 保留上游判定理由（procedure.reason）供人工抽查對照。

    參數：procedure（提供 clause/reason）、timeline（提供逾期日期）、
         fields（保留擴充，暫未用）。
    回傳：與 generate_draft 同形狀的 draft dict（reasons 為 template 段落）。
    """
    proc = procedure or {}
    clause = _normalize_clause(proc.get("clause"))
    upstream_reason = proc.get("reason") or ""

    if clause and clause in _CLAUSE_TEMPLATES:
        tpl = _CLAUSE_TEMPLATES[clause]
        text = tpl["text"]
        if clause == "2":
            text = text.format(overdue=_overdue_sentence(timeline))
        reasons = [{
            "text": text,
            "responds_to": [],
            "cites": list(tpl["cites"]),
            "based_on_case": "",
            "source": "template",
        }]
        needs_human = False
    else:
        # 防呆：款次無法辨識，退通用不受理理由，不硬套，標人工複核
        generic = "本件因具訴願法第77條所定不受理事由，應不受理。"
        if upstream_reason:
            generic += f"（前段判定：{upstream_reason}）"
        reasons = [{
            "text": generic,
            "responds_to": [],
            "cites": ["訴願法第77條"],
            "based_on_case": "",
            "source": "template",
        }]
        needs_human = True

    return {
        "main": "訴願不受理。",
        "facts": {},
        "reasons": reasons,
        "remedy_notice": build_remedy_notice("訴願不受理"),   # 教示（§90），純規則
        "source": "template",
        "clause": clause,
        "upstream_reason": upstream_reason,
        "needs_human_review": needs_human,
    }
