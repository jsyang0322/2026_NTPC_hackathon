"""法規條號解析與正規化（共用工具）。[純規則，不呼叫 LLM]

為什麼獨立成一支：`recommend_laws`（產生白名單）與 `verify`（比對白名單）必須用
**同一套正規化規則**，否則同一條法規在兩邊被寫成不同字串，V1 會把全部引用誤判為
清單外。這類「兩邊各寫一份解析」是最容易產生靜默錯誤的地方。

處理的三種寫法都收斂為同一個正規形式 `法名第N條[之M]`：

    洗錢防制法§22                    → 洗錢防制法第22條
    洗錢防制法第 22 條第 1 項         → 洗錢防制法第22條
    訴願人違反空氣汙染防制法第15條之2  → 空氣污染防制法第15條之2

設計要點：
  - 法名用「已登錄法規最長後綴匹配」切掉黏在前面的主詞動詞，未登錄者退回啟發式。
  - 異體字正規化（汙→污、台→臺），對應架構 §4.3 第 3 點。
  - 指稱性法名（本法/該法）回 None——引用須寫全名，否則失去追溯性。
"""

from __future__ import annotations

import re

#: 法規名稱結尾（用於從自由文字中辨識法規引用）
LAW_SUFFIX = r"(?:法|條例|細則|辦法|規則|準則|通則|標準|要點|自治條例)"

#: 條號引用樣式。同時吃「§22」「第22條」「第15條之2」「第 22 條」等寫法。
CITE_RE = re.compile(
    r"(?P<law>[\u4e00-\u9fff]{1,20}?" + LAW_SUFFIX + r")\s*"
    r"(?:"
    r"§\s*(?P<a1>\d+)(?:\s*[之\-]\s*(?P<s1>\d+))?"
    r"|第\s*(?P<a2>\d+)\s*條(?:\s*之\s*(?P<s2>\d+))?"
    r")"
)

#: 條號寫壞的樣式（「第15條之」缺數字、「第條」缺條號）——路線 A 語意檢索的典型殘留
MALFORMED_CITE_RE = re.compile(r"第\s*\d+\s*條\s*之\s*(?!\d)|第\s*條|§\s*(?!\d)")

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

#: 指稱性法名（非正式名稱）。引用欄位應寫全名，寫這些等於失去追溯性。
IGNORABLE_LAW_NAMES = frozenset({"本法", "該法", "同法", "前法", "新法", "舊法", "母法", "此法"})


def parse_citation(cite: str) -> dict | None:
    """解析單一引用字串，回傳結構化欄位；無法解析回 None。

    回傳: {"law": "洗錢防制法", "article": "22", "sub_article": "2"|None,
           "citation": "洗錢防制法第22條之2"}
    """
    m = CITE_RE.match((cite or "").strip())
    return _to_parts(m) if m else None


def normalize_citation(cite: str) -> str | None:
    """把單一引用字串正規化為「法名第N條[之M]」；無法解析回 None。

    需自字串開頭匹配（允許後綴項/款），避免「第15條之2」被截成「第15條」。
    """
    parts = parse_citation(cite)
    return parts["citation"] if parts else None


def extract_citations(text: str) -> set[str]:
    """從自由文字抽出所有條號引用，正規化後回傳。指稱性法名（本法/該法）略過。"""
    out: set[str] = set()
    for m in CITE_RE.finditer(text or ""):
        parts = _to_parts(m)
        if parts:
            out.add(parts["citation"])
    return out


def extract_parsed(text: str) -> list[dict]:
    """從自由文字抽出所有引用並回傳結構化結果（保留出現順序、去重）。"""
    seen: set[str] = set()
    out: list[dict] = []
    for m in CITE_RE.finditer(text or ""):
        parts = _to_parts(m)
        if parts and parts["citation"] not in seen:
            seen.add(parts["citation"])
            out.append(parts)
    return out


def find_malformed(text: str, width: int = 12) -> list[str]:
    """找出寫壞的條號，回傳問題位置前後片段供報告顯示。"""
    hits: list[str] = []
    for m in MALFORMED_CITE_RE.finditer(text or ""):
        start = max(0, m.start() - width)
        hits.append((text[start:m.start() + width]).replace("\n", " "))
    return hits


def normalize_variants(text: str) -> str:
    """法規名稱異體字正規化：汙→污、台→臺。"""
    return (text or "").replace("汙", "污").replace("台", "臺")


def clean_law_name(raw: str) -> str:
    """把 regex 抓到的法名字串收斂為正式法規名稱。

    兩段策略：
      1. 已登錄法規 → 最長後綴匹配（「訴願人違反洗錢防制法」→「洗錢防制法」）
      2. 未登錄法規 → 切除最後一個主詞/動詞之後的部分（啟發式）
    """
    law = normalize_variants(re.sub(r"\s+", "", raw or ""))
    if not law:
        return ""

    matched = [k for k in KNOWN_LAWS if law.endswith(k)]
    if matched:
        return max(matched, key=len)

    return _strip_leading_noise(law)


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


def _to_parts(m: re.Match) -> dict | None:
    """由 regex match 組出結構化引用；指稱性法名回 None。"""
    law = clean_law_name(m.group("law"))
    if not law or law in IGNORABLE_LAW_NAMES:
        return None
    article = m.group("a1") or m.group("a2")
    sub = m.group("s1") or m.group("s2")
    citation = f"{law}第{int(article)}條" + (f"之{int(sub)}" if sub else "")
    return {
        "law": law,
        "article": str(int(article)),
        "sub_article": str(int(sub)) if sub else None,
        "citation": citation,
    }
