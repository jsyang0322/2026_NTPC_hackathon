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

from .bedrock_client import BedrockClient, get_client, MODEL_WRITER


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


def build_draft_prompt(disposition: str, fields: dict, recommended_laws: list[dict],
                       similar_cases: list[dict], defects: list[dict], profile: dict,
                       prev_draft: dict | None = None, feedback: str = "") -> str:
    """組 §10.5 的 prompt。KB 檢索到的法條/案例由此塞入。

    重寫模式（prev_draft + feedback）：帶入上一版與規則/法官意見，要求僅針對問題修正，
    保留其餘正確段落，避免整篇重寫又引入新問題。
    """
    base = (
        "你是新北市政府訴願審議委員會的撰稿輔助人員。"
        f"{profile['writing_style']}\n"
        "請依下列資料撰寫理由欄「涵攝與逐項回應」段落。\n\n"
        f"【本案主文】{disposition}（已由系統依健檢結果與相似案例判定，不得變更結論方向）\n"
        f"【本案常見爭點】{profile['common_issues']}\n"
        f"【案件事實】{json.dumps(fields, ensure_ascii=False)}\n"
        f"【可引用法條】{json.dumps(recommended_laws, ensure_ascii=False)}（只能引用此清單）\n"
        f"【相似案例論理】{json.dumps(similar_cases, ensure_ascii=False)}\n"
        f"【原處分健檢結果】{json.dumps(defects, ensure_ascii=False)}（紅燈項須在理由中處理）\n\n"
    )
    if prev_draft and feedback:
        base += (
            "【上一版理由草稿】"
            + json.dumps(prev_draft.get("reasons", []), ensure_ascii=False) + "\n"
            "【審查意見（規則檢查與法官提出的問題）】\n" + feedback + "\n\n"
            "重寫規則：僅針對上述審查意見修正對應段落，保留其餘正確段落的論理與結構，"
            "不要引入新的問題，也不要變更主文方向。\n"
        )
    base += (
        "撰寫規則：\n"
        "1. 逐一回應每項訴願人主張並標注編號。\n"
        "2. 不得引用【可引用法條】以外的法條、函釋或判決字號。\n"
        "3. 不得新增案件事實中沒有的內容。\n"
        '4. 輸出 JSON：{"paragraphs":[{"text":"...","responds_to":["C1"],'
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
) -> dict:
    """依判定主文與 route_key 生成草稿。★ 一次 client.converse 呼叫 Claude。

    重寫模式：傳入 prev_draft + feedback，模型僅針對審查意見修正
    （對抗式審查迴圈用，見 pipeline.generate_case_draft）。
    """
    client = client or get_client()
    profile = get_profile(route_key)
    prompt = build_draft_prompt(
        disposition, fields, recommended_laws, similar_cases, defects, profile,
        prev_draft=prev_draft, feedback=feedback,
    )
    text = client.converse(
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        model_id=MODEL_WRITER,
        temperature=0.2,
    )
    return {
        "main": disposition,
        "facts": {},
        "reasons": _parse_reasons(text),
        "remedy_notice": None,             # 教示決定表（§6.4）填入，非 LLM
        "source": "llm",
        "route_key": route_key,
        "profile_label": profile["label"],
        "revised": bool(prev_draft and feedback),
    }


def _parse_reasons(text: str) -> list[dict]:
    """解析模型回傳的段落 JSON。容錯處理 ```json 圍籬與前後雜訊。"""
    cleaned = _strip_code_fence(text)
    try:
        paragraphs = json.loads(cleaned).get("paragraphs", [])
        # 補上 source 標記，供介面標色與 Word 註解
        for p in paragraphs:
            p.setdefault("source", "llm")
        return paragraphs
    except (json.JSONDecodeError, AttributeError):
        return [{"text": text, "responds_to": [], "cites": [], "based_on_case": "", "source": "llm"}]


def _strip_code_fence(text: str) -> str:
    """去除 ```json ... ``` 圍籬與前後空白（Claude 常這樣包 JSON）。"""
    import re
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if m:
        return m.group(1)
    return text.strip()


def quick_template(procedure: dict, timeline: dict) -> dict:
    """不受理快速通道（§10.4）：套款次模板，不呼叫 LLM（0 次 Bedrock）。

    v1.3 起由 pipeline.generate_case_draft() 在 analysis["inadmissible"] 為真時呼叫。
    TODO(§10.4)：依 procedure 的訴願法§77 款次填入對應理由模板。
    """
    return {
        "main": "訴願不受理。",
        "facts": {},
        "reasons": [],
        "remedy_notice": None,
        "source": "template",
        "clause": (procedure or {}).get("clause"),
        "_stub": True,
    }
