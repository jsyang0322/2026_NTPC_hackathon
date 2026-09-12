"""草稿自動驗證（§11.1）。[純規則，不呼叫 LLM，不吃 Bedrock 配額]

把「低幻覺」變成可檢查的結果：引用集合比對、主張回應覆蓋率、條號正則、
法規版本、日期重算、教示一致、主文理由一致、健檢紅燈是否已處理。

設計原則
--------
1. **本層只判斷、不改稿**：判斷結果分兩路輸出——
   `blocking`/`summary` 給人看（介面與事後抽查）、`issues` 給機器用
   （pipeline 的對抗式審查迴圈據以決定是否重寫，並塞回重寫 prompt）。
   改稿動作由 draft.generate_draft 的重寫模式執行，不在此模組。
2. **分三級**：red（必須處理，如引用捏造）/ amber（提醒複核）/ green（通過），
   與 §7 健檢的紅黃綠燈一致。只有 red 會進 `issues` 觸發重寫，
   amber 僅提示，避免為語意模糊的項目多花一次 Bedrock 呼叫。
3. **資料不足時回 skipped，不回 fail**：骨架階段（KB 結果未結構化、教示決定表未接、
   version_db 未建）不應產生假紅燈，否則報告失去訊號價值，也會誤觸發重寫。

補償定位（§4.5）：路線 A 走純語意檢索，中文條號精確度不足（「第15條之2」可能被抓成
「15」）。本模組的 V2/V3 正則校驗即為該風險的事後補償點。
"""

from __future__ import annotations

import re
from datetime import date

# ---------------------------------------------------------------------------
# 常數與樣式
# ---------------------------------------------------------------------------

#: 法規名稱結尾（用於從自由文字中辨識法規引用）
_LAW_SUFFIX = r"(?:法|條例|細則|辦法|規則|準則|通則|標準|要點|自治條例)"

#: 條號引用樣式。同時吃「§22」「第22條」「第15條之2」「第 22 條」等寫法。
_CITE_RE = re.compile(
    r"(?P<law>[\u4e00-\u9fff]{1,20}?" + _LAW_SUFFIX + r")\s*"
    r"(?:"
    r"§\s*(?P<a1>\d+)(?:\s*[之\-]\s*(?P<s1>\d+))?"
    r"|第\s*(?P<a2>\d+)\s*條(?:\s*之\s*(?P<s2>\d+))?"
    r")"
)

#: 已登錄法規名稱（資料集 11 部 + 常見周邊）。用「最長後綴匹配」把黏在前面的
#: 主詞與動詞切乾淨，例如「訴願人違反洗錢防制法」→「洗錢防制法」。
#: 未登錄的法規退回 _strip_leading_noise() 的啟發式剝除，可隨資料集擴充此表。
KNOWN_LAWS = (
    # 共通程序法
    "訴願法", "行政程序法", "行政罰法", "行政訴訟法", "地方制度法",
    # 三大案由
    "洗錢防制法", "資恐防制法",
    "廢棄物清理法", "資源回收再利用法",
    "空氣污染防制法",
    # 其他常見案由
    "建築法", "噪音管制法", "水污染防治法", "土壤及地下水污染整治法",
    "環境教育法", "菸害防制法", "食品安全衛生管理法",
    "道路交通管理處罰條例", "電子遊戲場業管理條例", "都市計畫法",
)

#: 會被 regex 黏進法名的主詞/動詞。未登錄法規時，切在最後一個出現位置之後。
#: 刻意不含「及／與／或」——「土壤及地下水污染整治法」會被誤切。
_LAW_NAME_CUT_WORDS = (
    "訴願人", "原處分機關", "處分機關", "受處分人", "行為人", "申請人", "業者",
    "違反", "依據", "依照", "按照", "揆諸", "適用", "觸犯", "牴觸", "前揭", "上開",
    "依", "按",
)

#: 指稱性法名（非正式名稱）。cites 欄位應寫全名，寫這些等於失去追溯性。
_IGNORABLE_LAW_NAMES = frozenset({"本法", "該法", "同法", "前法", "新法", "舊法", "母法", "此法"})

#: 條號寫壞的樣式（「第15條之」缺數字、「第條」缺條號）——路線 A 語意檢索的典型殘留
_MALFORMED_CITE_RE = re.compile(r"第\s*\d+\s*條\s*之\s*(?!\d)|第\s*條|§\s*(?!\d)")

#: 民國/西元日期樣式
_DATE_PATTERNS = (
    re.compile(r"(?P<y>\d{2,4})\s*年\s*(?P<m>\d{1,2})\s*月\s*(?P<d>\d{1,2})\s*日"),
    re.compile(r"(?P<y>\d{4})-(?P<m>\d{1,2})-(?P<d>\d{1,2})"),
    re.compile(r"(?P<y>\d{2,4})/(?P<m>\d{1,2})/(?P<d>\d{1,2})"),
)

#: 訴願期間：自行政處分達到之次日起 30 日內（訴願法§14 I）
PETITION_DEADLINE_DAYS = 30

#: 已改制/裁撤的審判機關名稱。開賽前須請法制同仁複核後再擴充。
#: 註：112.8.15 行政訴訟新制上路，各地方法院行政訴訟庭已改制為高等行政法院地方行政訴訟庭。
DEPRECATED_COURTS = (
    "臺灣臺北地方法院行政訴訟庭",
    "臺灣新北地方法院行政訴訟庭",
    "台灣臺北地方法院行政訴訟庭",
    "台灣新北地方法院行政訴訟庭",
    "臺灣省政府訴願審議委員會",
)

#: 教示應具備的要素（訴願法§90：得於決定書送達之次日起 2 個月內提起行政訴訟）
_REMEDY_REQUIRED = ("行政訴訟",)
_REMEDY_PERIOD_HINTS = ("2個月", "二個月", "２個月")

#: 主文語意分類關鍵字
_MAIN_KEYWORDS = {
    "dismiss": ("駁回",),          # 訴願駁回 → 原處分維持
    "revoke": ("撤銷",),           # 原處分撤銷
    "inadmissible": ("不受理",),
}

#: 與各類主文相矛盾的理由用語
_CONTRADICTION_KEYWORDS = {
    "dismiss": ("原處分撤銷", "應予撤銷", "原處分違法", "撤銷原處分"),
    "revoke": ("訴願駁回", "應予維持", "並無違誤", "洵無違誤", "尚無不合"),
    "inadmissible": ("實體審究", "本案實體"),
}


# ---------------------------------------------------------------------------
# 對外主函式
# ---------------------------------------------------------------------------

def verify_draft(draft: dict, fields: dict, recommended_laws: list[dict],
                 timeline: dict, defects: list[dict],
                 version_db=None) -> dict:
    """驗證草稿並回傳檢核報告。

    參數：
        draft:             draft.generate_draft() 的輸出（含 main / reasons / remedy_notice）。
        fields:            交接契約的 extracted_fields。
        recommended_laws:  本案可引用清單（KB 檢索結果），作為引用比對的白名單。
        timeline:          時間軸引擎輸出；目前主線傳 {}，缺值時日期類檢核回 skipped。
        defects:           原處分健檢結果（§7），紅燈項須在理由中處理。
        version_db:        法規版本庫；None 時「行為時有效」檢核回 skipped（不擋開發）。

    回傳：
        {
          "checks": [{id, name, passed, status, level, detail, evidence}],
          "all_passed": bool,        # 無任何 fail（紅或黃）
          "blocking_passed": bool,   # 無紅燈 fail
          "summary": {...},
          "blocking": [check_id, ...],
          "issues": [str, ...],      # 紅燈的人語句，供重寫迴圈塞回 prompt
        }
    """
    paragraphs = _iter_paragraphs(draft)
    checks: list[dict] = [
        _check_citations_in_whitelist(paragraphs, recommended_laws),
        _check_inline_citations_declared(paragraphs),
        _check_article_number_format(paragraphs),
        _check_claim_coverage(paragraphs, fields),
        _check_law_effective_at_act_time(paragraphs, fields, timeline, version_db),
        _check_dates_grounded(paragraphs, fields, timeline),
        _check_petition_period(draft, fields, timeline),
        _check_remedy_notice(draft),
        _check_main_reason_consistency(draft, paragraphs),
        _check_red_defects_addressed(paragraphs, defects),
    ]

    fails = [c for c in checks if c["status"] == "fail"]
    blocking = [c["id"] for c in fails if c["level"] == "red"]
    summary = {
        "total": len(checks),
        "pass": sum(1 for c in checks if c["status"] == "pass"),
        "fail": len(fails),
        "skipped": sum(1 for c in checks if c["status"] == "skipped"),
        "red": len(blocking),
        "amber": sum(1 for c in fails if c["level"] == "amber"),
    }
    return {
        "checks": checks,
        "all_passed": not fails,
        "blocking_passed": not blocking,
        "summary": summary,
        "blocking": blocking,
        # 對抗式審查迴圈的規則層訊號（critic.has_blocking_issue / collect_feedback 取用）
        "issues": _build_issues(fails),
    }


def _build_issues(fails: list[dict]) -> list[str]:
    """把失敗項轉成可塞回重寫 prompt 的人語句。

    只收紅燈（客觀且必須修）。黃燈屬提醒複核，不觸發重寫，避免為了語意模糊的
    項目多花一次 Bedrock 呼叫。
    """
    issues: list[str] = []
    for c in fails:
        if c["level"] != "red":
            continue
        fix = _FIX_HINTS.get(c["id"], "")
        evidence = f"（涉及：{c['evidence']}）" if c.get("evidence") else ""
        issues.append(f"{c['name']}未通過：{c['detail']}{evidence}{fix}")
    return issues


#: 各檢核項失敗時，給重寫模型的具體修正方向
_FIX_HINTS = {
    "V1": " 請僅使用【可引用法條】清單內的條文，刪除清單外的引用。",
    "V3": " 請將條號寫完整（例如「第15條之2」不可寫成「第15條之」或「第條」）。",
    "V4": " 請為未回應的主張各補一段回應，並於 responds_to 標注其編號。",
    "V5": " 請改引用行為時有效之條文版本。",
    "V7": " 請確認訴願期間計算，並使主文與程序判斷一致。",
    "V8": " 請依現行審判機關名稱與 2 個月起訴期間更正教示。",
    "V9": " 請調整理由結論用語，使其與主文方向一致。",
}


# ---------------------------------------------------------------------------
# 個別檢核項（§6 檢核表）
# ---------------------------------------------------------------------------

def _check_citations_in_whitelist(paragraphs: list[dict], recommended_laws: list[dict]) -> dict:
    """V1 引用法條須在可引用清單內。對應風險：字號/條號捏造。"""
    allowed = _allowed_citations(recommended_laws)
    cited = _declared_citations(paragraphs)

    if not allowed:
        return _result("V1", "引用法條在可引用清單內", "skipped", "red",
                       "可引用清單尚無可解析條號（KB 檢索結果未結構化），暫不比對。",
                       evidence=sorted(cited))
    if not cited:
        return _result("V1", "引用法條在可引用清單內", "skipped", "red",
                       "草稿未宣告任何引用條文，無可比對對象。")

    unknown = sorted(cited - allowed)
    if unknown:
        return _result("V1", "引用法條在可引用清單內", "fail", "red",
                       f"有 {len(unknown)} 項引用不在可引用清單內，須確認是否捏造。",
                       evidence=unknown)
    return _result("V1", "引用法條在可引用清單內", "pass", "red",
                   f"{len(cited)} 項引用全部落在可引用清單內。", evidence=sorted(cited))


def _check_inline_citations_declared(paragraphs: list[dict]) -> dict:
    """V2 內文出現的條號都應宣告於 cites。對應風險：夾帶未申報的引用。"""
    declared = _declared_citations(paragraphs)
    inline: set[str] = set()
    for p in paragraphs:
        inline |= _extract_citations(p.get("text", ""))

    if not inline:
        return _result("V2", "內文條號均已宣告於 cites", "skipped", "amber",
                       "內文未偵測到條號引用。")
    undeclared = sorted(inline - declared)
    if undeclared:
        return _result("V2", "內文條號均已宣告於 cites", "fail", "amber",
                       f"內文有 {len(undeclared)} 項條號未列入 cites，追溯性不完整。",
                       evidence=undeclared)
    return _result("V2", "內文條號均已宣告於 cites", "pass", "amber",
                   "內文條號與 cites 一致。", evidence=sorted(inline))


def _check_article_number_format(paragraphs: list[dict]) -> dict:
    """V3 條號書寫正確（含「之2」）。補路線 A 語意檢索之不足（§4.5）。"""
    bad_format: list[str] = []
    malformed_text: list[str] = []

    for c in _raw_cites(paragraphs):
        if _normalize_citation(c) is None:
            bad_format.append(c)
    for p in paragraphs:
        for m in _MALFORMED_CITE_RE.finditer(p.get("text", "")):
            malformed_text.append(_snippet(p.get("text", ""), m.start()))

    problems = bad_format + malformed_text
    if problems:
        detail_parts = []
        if bad_format:
            detail_parts.append(f"cites 有 {len(bad_format)} 項無法解析為合法條號")
        if malformed_text:
            detail_parts.append(f"內文有 {len(malformed_text)} 處條號寫法不完整（如「條之」缺數字）")
        return _result("V3", "條號書寫格式正確（含之N）", "fail", "red",
                       "；".join(detail_parts) + "。", evidence=problems)

    if not _raw_cites(paragraphs):
        return _result("V3", "條號書寫格式正確（含之N）", "skipped", "red",
                       "草稿未宣告引用條文。")
    return _result("V3", "條號書寫格式正確（含之N）", "pass", "red",
                   "所有條號均可解析且格式完整。")


def _check_claim_coverage(paragraphs: list[dict], fields: dict) -> dict:
    """V4 每項訴願主張皆有回應。對應風險：理由不備。"""
    claim_ids = [c.get("id") for c in fields.get("claims", []) if c.get("id")]
    if not claim_ids:
        return _result("V4", "訴願主張全部獲回應", "skipped", "red",
                       "案件無 claims 資料（前段未提供或本案無主張分項）。")

    responded: set[str] = set()
    for p in paragraphs:
        responded |= {r for r in p.get("responds_to", []) if r}

    missing = [cid for cid in claim_ids if cid not in responded]
    extra = sorted(responded - set(claim_ids))

    if missing:
        return _result("V4", "訴願主張全部獲回應", "fail", "red",
                       f"共 {len(claim_ids)} 項主張，{len(missing)} 項未獲回應。",
                       evidence=missing)
    if extra:
        return _result("V4", "訴願主張全部獲回應", "fail", "amber",
                       f"全部主張已回應，但 responds_to 出現不存在的主張編號 {extra}。",
                       evidence=extra)
    return _result("V4", "訴願主張全部獲回應", "pass", "red",
                   f"{len(claim_ids)} 項主張全部獲回應。", evidence=claim_ids)


def _check_law_effective_at_act_time(paragraphs: list[dict], fields: dict,
                                     timeline: dict, version_db) -> dict:
    """V5 引用條文於行為時有效（行政罰法§5 從新從輕）。對應風險：適用法規錯誤。"""
    if version_db is None:
        return _result("V5", "引用條文於行為時有效", "skipped", "red",
                       "未提供法規版本庫（version_db），本項未檢查。待版本庫建置後啟用。")

    act_date = _pick_date(
        timeline.get("act_date") if timeline else None,
        fields.get("original_disposition", {}).get("act_date"),
    )
    if act_date is None:
        return _result("V5", "引用條文於行為時有效", "skipped", "red",
                       "缺行為日（act_date），無法判斷行為時法。")

    cited = _declared_citations(paragraphs)
    if not cited:
        return _result("V5", "引用條文於行為時有效", "skipped", "red", "草稿未宣告引用條文。")

    invalid: list[str] = []
    unknown: list[str] = []
    for c in sorted(cited):
        status = _lookup_version(version_db, c, act_date)
        if status == "not_found":
            unknown.append(c)
        elif status == "not_effective":
            invalid.append(c)

    if invalid:
        return _result("V5", "引用條文於行為時有效", "fail", "red",
                       f"有 {len(invalid)} 項引用於行為日（{act_date.isoformat()}）尚未生效或已刪除。",
                       evidence=invalid)
    if unknown:
        return _result("V5", "引用條文於行為時有效", "fail", "amber",
                       f"有 {len(unknown)} 項引用在版本庫查無資料，無法確認行為時效力。",
                       evidence=unknown)
    return _result("V5", "引用條文於行為時有效", "pass", "red",
                   f"全部引用於行為日（{act_date.isoformat()}）均有效。")


def _check_dates_grounded(paragraphs: list[dict], fields: dict, timeline: dict) -> dict:
    """V6 理由中的日期須來自案件事實。對應風險：日期幻覺。"""
    known = _known_dates(fields, timeline)
    if not known:
        return _result("V6", "理由日期均見於案件事實", "skipped", "amber",
                       "案件事實無任何日期可比對（骨架資料）。")

    ungrounded: list[str] = []
    for p in paragraphs:
        for d, raw in _extract_dates(p.get("text", "")):
            if d not in known:
                ungrounded.append(raw)

    if ungrounded:
        return _result("V6", "理由日期均見於案件事實", "fail", "amber",
                       f"理由出現 {len(ungrounded)} 個未見於案件事實的日期，須人工確認。",
                       evidence=sorted(set(ungrounded)))
    return _result("V6", "理由日期均見於案件事實", "pass", "amber",
                   "理由中的日期均可對應案件事實。")


def _check_petition_period(draft: dict, fields: dict, timeline: dict) -> dict:
    """V7 訴願期間重算（訴願法§14：處分達到次日起 30 日）。對應風險：程序判斷錯誤。"""
    service = _pick_date(
        timeline.get("service_date") if timeline else None,
        fields.get("original_disposition", {}).get("service_date"),
    )
    filed = _pick_date(
        timeline.get("filed_date") if timeline else None,
        fields.get("petition_filed_date"),
    )
    if service is None or filed is None:
        return _result("V7", "訴願期間計算一致", "skipped", "amber",
                       "缺送達日或提起日，無法重算訴願期間。")

    elapsed = (filed - service).days          # 次日起算，經過天數即為已用日數
    overdue = elapsed > PETITION_DEADLINE_DAYS
    main = str(draft.get("main", ""))
    says_inadmissible = "不受理" in main

    detail = (f"送達 {service.isoformat()} → 提起 {filed.isoformat()}，"
              f"經過 {elapsed} 日（法定 {PETITION_DEADLINE_DAYS} 日）。")

    if elapsed < 0:
        return _result("V7", "訴願期間計算一致", "fail", "red",
                       detail + " 提起日早於送達日，日期資料矛盾。")
    if overdue and not says_inadmissible:
        return _result("V7", "訴願期間計算一致", "fail", "red",
                       detail + " 已逾法定期間，但主文非「不受理」，請確認是否漏判逾期。")
    if not overdue and says_inadmissible:
        return _result("V7", "訴願期間計算一致", "fail", "amber",
                       detail + " 未逾期但主文為「不受理」，請確認不受理事由是否為其他款次。")
    return _result("V7", "訴願期間計算一致", "pass", "red", detail + " 與主文方向一致。")


def _check_remedy_notice(draft: dict) -> dict:
    """V8 教示與決定表一致，且未引用已改制/裁撤之法院。對應風險：引用已裁撤法院。"""
    notice = draft.get("remedy_notice")
    if not notice:
        return _result("V8", "教示記載完備且法院名稱有效", "skipped", "red",
                       "remedy_notice 尚未填入（教示決定表 §6.4 未接），本項未檢查。")

    text = str(notice)
    problems: list[str] = []
    for kw in _REMEDY_REQUIRED:
        if kw not in text:
            problems.append(f"教示未載明「{kw}」")
    if not any(h in text for h in _REMEDY_PERIOD_HINTS):
        problems.append("教示未載明 2 個月起訴期間")
    hits = [c for c in DEPRECATED_COURTS if c in text]
    if hits:
        problems.append(f"引用已改制/裁撤之機關：{hits}")

    if problems:
        return _result("V8", "教示記載完備且法院名稱有效", "fail", "red",
                       "；".join(problems) + "。", evidence=problems)
    return _result("V8", "教示記載完備且法院名稱有效", "pass", "red", "教示記載完備。")


def _check_main_reason_consistency(draft: dict, paragraphs: list[dict]) -> dict:
    """V9 主文與理由結論一致。對應風險：主文理由矛盾。"""
    main = str(draft.get("main", ""))
    kind = _classify_main(main)
    if kind is None:
        return _result("V9", "主文與理由結論一致", "skipped", "red",
                       f"無法由主文「{main}」判斷結論方向。")

    body = " ".join(p.get("text", "") for p in paragraphs)
    if not body.strip():
        return _result("V9", "主文與理由結論一致", "skipped", "red", "理由欄為空。")

    hits = [kw for kw in _CONTRADICTION_KEYWORDS[kind] if kw in body]
    if hits:
        return _result("V9", "主文與理由結論一致", "fail", "red",
                       f"主文為「{main}」，但理由出現相反結論用語 {hits}。", evidence=hits)
    return _result("V9", "主文與理由結論一致", "pass", "red",
                   f"理由未出現與主文（{main}）相反的結論用語。")


def _check_red_defects_addressed(paragraphs: list[dict], defects: list[dict]) -> dict:
    """V10 健檢紅燈須在理由中處理（§7.7）。對應風險：未審酌之瑕疵。"""
    reds = [d for d in (defects or []) if str(d.get("level", "")).lower() == "red"]
    if not reds:
        return _result("V10", "健檢紅燈已於理由處理", "skipped", "amber",
                       "本案無健檢紅燈項（或健檢尚未接入）。")

    body = " ".join(p.get("text", "") for p in paragraphs)
    unhandled = [d.get("id") or d.get("title", "") for d in reds
                 if not _mentions(body, str(d.get("title", "")))]
    if unhandled:
        return _result("V10", "健檢紅燈已於理由處理", "fail", "amber",
                       f"{len(reds)} 項紅燈中，{len(unhandled)} 項未見於理由。",
                       evidence=unhandled)
    return _result("V10", "健檢紅燈已於理由處理", "pass", "amber",
                   f"{len(reds)} 項紅燈均已於理由論述。")


# ---------------------------------------------------------------------------
# 內部工具
# ---------------------------------------------------------------------------

def _result(check_id: str, name: str, status: str, level: str,
            detail: str, evidence: list | None = None) -> dict:
    """組單項檢核結果。

    status: pass / fail / skipped
    level:  該項失敗時的嚴重度（red 必須處理 / amber 提醒複核）；
            實際顯示色由 status 決定：pass→green、skipped→gray、fail→level。
    """
    display = {"pass": "green", "skipped": "gray"}.get(status, level)
    return {
        "id": check_id,
        "name": name,
        "passed": status == "pass",
        "status": status,
        "level": level,
        "display_level": display,
        "detail": detail,
        "evidence": evidence or [],
    }


def _iter_paragraphs(draft: dict) -> list[dict]:
    """取草稿理由段落，容錯處理非 list 或 None。"""
    reasons = draft.get("reasons") or []
    return [p for p in reasons if isinstance(p, dict)]


def _raw_cites(paragraphs: list[dict]) -> list[str]:
    """取所有 cites 原始字串（未正規化）。"""
    out: list[str] = []
    for p in paragraphs:
        for c in p.get("cites", []) or []:
            if isinstance(c, str) and c.strip():
                out.append(c.strip())
    return out


def _declared_citations(paragraphs: list[dict]) -> set[str]:
    """草稿宣告的引用，正規化為「法名第N條[之M]」。無法解析者略過（由 V3 抓）。"""
    out: set[str] = set()
    for c in _raw_cites(paragraphs):
        norm = _normalize_citation(c)
        if norm:
            out.add(norm)
    return out


def _allowed_citations(recommended_laws: list[dict]) -> set[str]:
    """由可引用清單建白名單。

    容錯處理三種形狀：
      1. 已結構化：{"law": "洗錢防制法", "article": "22"} 或 {"citation": "..."}
      2. KB 原始 hit：{"text": "...", "metadata": {...}, "source": "..."}
      3. 純字串
    """
    allowed: set[str] = set()
    for item in recommended_laws or []:
        if isinstance(item, str):
            allowed |= _extract_citations(item)
            continue
        if not isinstance(item, dict):
            continue

        law = str(item.get("law") or item.get("law_name") or "").strip()
        article = str(item.get("article") or item.get("article_no") or "").strip()
        if law and article:
            norm = _normalize_citation(f"{law}第{article}條" if article.isdigit() else f"{law}{article}")
            if norm:
                allowed.add(norm)

        for key in ("citation", "cite", "title", "text", "content", "source"):
            val = item.get(key)
            if isinstance(val, str):
                allowed |= _extract_citations(val)
            elif isinstance(val, dict):
                allowed |= _extract_citations(str(val.get("text", "")))
        meta = item.get("metadata")
        if isinstance(meta, dict):
            for val in meta.values():
                if isinstance(val, str):
                    allowed |= _extract_citations(val)
    return allowed


def _extract_citations(text: str) -> set[str]:
    """從自由文字抽出所有條號引用，正規化後回傳。指稱性法名（本法/該法）略過。"""
    out: set[str] = set()
    for m in _CITE_RE.finditer(text or ""):
        norm = _canonical(m)
        if norm:
            out.add(norm)
    return out


def _normalize_citation(cite: str) -> str | None:
    """把單一引用字串正規化為「法名第N條[之M]」；無法解析回 None。

    需自字串開頭匹配（允許後綴項/款），避免「第15條之2」被截成「第15條」。
    指稱性法名（本法/該法）回 None——cites 應寫法規全名，否則失去追溯性。
    """
    m = _CITE_RE.match(cite.strip())
    if not m:
        return None
    return _canonical(m)


def _canonical(m: re.Match) -> str | None:
    """由 regex match 組出正規化條號；指稱性法名回 None。"""
    law = _clean_law_name(m.group("law"))
    if not law or law in _IGNORABLE_LAW_NAMES:
        return None
    article = m.group("a1") or m.group("a2")
    sub = m.group("s1") or m.group("s2")
    base = f"{law}第{int(article)}條"
    return f"{base}之{int(sub)}" if sub else base


def _clean_law_name(raw: str) -> str:
    """把 regex 抓到的法名字串收斂為正式法規名稱。

    兩段策略：
      1. 已登錄法規 → 最長後綴匹配（「訴願人違反洗錢防制法」→「洗錢防制法」）
      2. 未登錄法規 → 切除最後一個主詞/動詞之後的部分（啟發式）

    同時正規化異體字（汙→污、台→臺，架構 §4.3 第 3 點），讓白名單與草稿引用
    即使寫法不同也能比對成功。
    """
    law = _normalize_variants(re.sub(r"\s+", "", raw or ""))
    if not law:
        return ""

    matched = [k for k in KNOWN_LAWS if law.endswith(k)]
    if matched:
        return max(matched, key=len)

    return _strip_leading_noise(law)


def _normalize_variants(text: str) -> str:
    """法規名稱異體字正規化：汙→污、台→臺。"""
    return text.replace("汙", "污").replace("台", "臺")


def _strip_leading_noise(law: str) -> str:
    """未登錄法規的後備處理：切在最後一個主詞/動詞之後。

    切點須留下至少 2 字，避免把「水污染防治法」這類短名切壞。
    """
    best = 0
    for word in _LAW_NAME_CUT_WORDS:
        idx = law.rfind(word)
        if idx == -1:
            continue
        end = idx + len(word)
        if end > best and len(law) - end >= 2:
            best = end
    return law[best:] if best else law


def _snippet(text: str, pos: int, width: int = 12) -> str:
    """取問題位置前後片段，供報告顯示證據。"""
    start = max(0, pos - width)
    return text[start:pos + width].replace("\n", " ")


def _parse_date(value) -> date | None:
    """解析民國/西元日期字串。年 <= 200 視為民國年（+1911）。"""
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    for pat in _DATE_PATTERNS:
        m = pat.search(value)
        if m:
            return _build_date(m.group("y"), m.group("m"), m.group("d"))
    return None


def _build_date(y: str, mo: str, d: str) -> date | None:
    try:
        year = int(y)
        year = year + 1911 if year <= 200 else year
        return date(year, int(mo), int(d))
    except (ValueError, TypeError):
        return None


def _pick_date(*candidates) -> date | None:
    """回傳第一個可解析的日期。"""
    for c in candidates:
        d = _parse_date(c)
        if d:
            return d
    return None


def _extract_dates(text: str) -> list[tuple[date, str]]:
    """從文字抽出所有日期，回傳 [(date, 原始字串)]。"""
    out: list[tuple[date, str]] = []
    for pat in _DATE_PATTERNS:
        for m in pat.finditer(text or ""):
            d = _build_date(m.group("y"), m.group("m"), m.group("d"))
            if d:
                out.append((d, m.group(0)))
    return out


def _known_dates(fields: dict, timeline: dict) -> set[date]:
    """案件事實中的已知日期集合，作為 V6 的比對基準。"""
    od = fields.get("original_disposition", {}) or {}
    raw = [
        od.get("date"), od.get("service_date"), od.get("act_date"),
        fields.get("petition_filed_date"),
    ]
    for pd in od.get("prior_dispositions", []) or []:
        if isinstance(pd, dict):
            raw.append(pd.get("date"))
        else:
            raw.append(pd)
    if timeline:
        raw += [timeline.get(k) for k in
                ("act_date", "disposition_date", "service_date", "filed_date", "decision_date")]

    known = {d for d in (_parse_date(v) for v in raw) if d}
    # 送達生效日等衍生日期（寄存送達自寄存日起 10 日生效，訴願法§47 III）一併容許
    return known


def _classify_main(main: str) -> str | None:
    """判斷主文結論方向。不受理優先於駁回/撤銷（不受理不進實體）。"""
    for kind in ("inadmissible", "revoke", "dismiss"):
        if any(kw in main for kw in _MAIN_KEYWORDS[kind]):
            return kind
    return None


def _mentions(body: str, title: str) -> bool:
    """粗略判斷理由是否觸及某健檢項：取標題中的關鍵詞比對。"""
    if not title:
        return True
    keywords = [w for w in re.split(r"[（）()、，,。\s/]+", title) if len(w) >= 2]
    return any(w in body for w in keywords) if keywords else True


def _lookup_version(version_db, citation: str, act_date: date) -> str:
    """查版本庫判斷某條文於行為日是否有效。

    version_db 介面（尚未實作，先定契約）：
      - 具 is_effective(citation, on_date) -> bool | None 方法者優先使用；
      - 或 dict 形狀 {citation: {"effective_from": "...", "repealed_on": "..."}}。
    回傳 "effective" / "not_effective" / "not_found"。
    """
    if hasattr(version_db, "is_effective"):
        res = version_db.is_effective(citation, act_date)
        if res is None:
            return "not_found"
        return "effective" if res else "not_effective"

    if isinstance(version_db, dict):
        entry = version_db.get(citation)
        if not isinstance(entry, dict):
            return "not_found"
        start = _parse_date(entry.get("effective_from"))
        end = _parse_date(entry.get("repealed_on"))
        if start and act_date < start:
            return "not_effective"
        if end and act_date >= end:
            return "not_effective"
        return "effective"

    return "not_found"
