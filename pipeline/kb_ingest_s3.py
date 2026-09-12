"""三案由專責 RAG 的 KB 來源前處理（洗錢防制 / 廢棄物 / 空汙）。[離線，不呼叫 LLM]

為什麼需要這一步：資料目前以 **JSON** 存在 S3，而 Bedrock Knowledge Bases 的
S3 資料來源**不吃 .json 當內容檔**（支援 .txt/.md/.html/.doc(x)/.csv/.xls(x)/.pdf；
.json 只被當作 `.metadata.json` 旁掛檔）。因此直接把 bucket 掛上 KB 會索引到 0 筆。

本模組把每份 JSON 轉成一份 `.txt` + 一份 `.txt.metadata.json`：

    data/kb_source/<route_key>/decisions/<案號>.txt(+.metadata.json)
    data/kb_source/<route_key>/laws/<法規名>.txt(+.metadata.json)

metadata 欄位刻意與 `core.kb._build_filter` 的過濾鍵對齊，改一邊要改兩邊：
    case_type   route_key 英文枚舉（schemas.ROUTE_KEYS）
    doc_type    decision | law | common_law | interpretation | judgment
    year        民國年（similar_cases._year 讀這個）
    disposition 主文（pipeline._decide_disposition 多數決讀這個，必須有）

法規條文的寫法有硬性要求：每條前面要冠上法規全名（「洗錢防制法第 22 條」），
因為 `core.citations.CITE_RE` 要求法名緊接條號才認得。只寫「第 22 條」會導致
`recommend_laws` 抽不到 citation，`verify` 的 V1 白名單就會全空。

去識別化（競賽規範：個資不得匯入 AWS）：來源資料的姓名多已遮成「游○真」，
本模組再補遮地址、身分證號、電話與長串數字，並回報疑似殘留供人工確認。

轉檔結果只是中間產物：KB 真正索引的是上傳到 S3 的那份，本地不必保留。
預設把 .txt/.metadata.json 寫到系統暫存目錄、上傳到 KB source bucket 後清除，
不在專案內留下與雲端重複的資料。要人工檢視轉出長相時再用 --out / --keep 保留。

用法：
    # 轉四案由 → 上傳 KB source bucket → 清暫存（一鍵，之後對 KB 起 ingestion）
    python -m pipeline.kb_ingest_s3 --routes money_laundering,waste,air_pollution
    python -m pipeline.kb_ingest_s3 --limit 3 --print-sample --upload ""  # 只看轉出長相
    python -m pipeline.kb_ingest_s3 --out data/kb_source --keep           # 保留本地供檢視
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from core.citations import normalize_variants

ACCOUNT_ID = "294564521054"

#: route_key → (來源 bucket, 決定書前綴)。bucket 名可用 --account 覆寫帳號段。
TOPIC_SOURCES: dict[str, dict[str, str]] = {
    "money_laundering": {"bucket": "kiro-law-money-laundering", "decisions": "money-laundering/"},
    "waste": {"bucket": "kiro-law-waste-disposal", "decisions": "waste-disposal/"},
    "air_pollution": {"bucket": "kiro-law-air-pollution", "decisions": "air-pollution/"},
    "general": {"bucket": "kiro-law-general", "decisions": "general/"},
}

#: 三案由專責 RAG 的預設範圍（general 需要時再自行加）
DEFAULT_ROUTES = ("money_laundering", "waste", "air_pollution")

#: KB source bucket（Bedrock KB 的 S3 資料來源）。轉檔預設直接上傳到這裡，
#: 之後對 KB 起 ingestion 即可索引；本地不留中間產物。
DEFAULT_UPLOAD_DEST = f"s3://kiro-kb-source-{ACCOUNT_ID}/kb-source/"

#: 法規類前綴 → doc_type
RELATED_PREFIX = "related-laws/"
RELATED_DOCTYPE = {
    "statutes": "law",
    "interpretations": "interpretation",
    "judicial": "judgment",
}

#: 共通法規（跨案由）。標 common_law 讓法規檢索能 OR 放行，不受案由過濾擋掉。
COMMON_LAWS = {
    "訴願法", "行政程序法", "行政罰法", "行政執行法", "行政訴訟法", "民法",
    "中華民國憲法", "中華民國刑法", "中央法規標準法", "個人資料保護法",
    "行政院及各級行政機關訴願審議委員會審議規則", "訴願扣除在途期間辦法",
}


# ---------- 去識別化 ----------

#: 地址（縣市 → 路街巷弄號樓）。來源資料的姓名已遮罩，地址未必。
_ADDR_RE = re.compile(
    r"[\u4e00-\u9fff]{2,4}[縣市][\u4e00-\u9fff]{1,4}?[區鄉鎮市]"
    r"[\u4e00-\u9fff0-9０-９]{0,20}?(?:路|街|大道)"
    r"[\u4e00-\u9fff0-9０-９之\-]{0,24}?號"
    r"(?:之\s*\d+)?(?:\s*\d+\s*樓)?(?:\s*之\s*\d+)?"
)
#: 身分證號與行動電話不能用 \b：Python 的 \w 含中文，「電話0912…」的邊界不成立
_ROC_ID_RE = re.compile(r"(?<![A-Z0-9])[A-Z][12]\d{8}(?![0-9])")
_MOBILE_RE = re.compile(r"(?<!\d)09\d{2}[-\s]?\d{3}[-\s]?\d{3}(?!\d)")

MASK = "○○○"

#: 當事人身分標籤。判解/函釋的表頭會直接寫出姓名（「訴訟代理人 劉杉涼」），
#: 訴願決定書的姓名雖已由來源遮成「游○真」，判解類卻沒遮。
_PARTY_LABELS = (
    "原告", "被告", "上訴人", "被上訴人", "訴訟代理人", "法定代理人",
    "代表人", "代理人", "參加人", "訴願人", "負責人", "聲請人", "相對人",
)

#: 職稱：表頭常寫成「訴訟代理人 劉杉涼 會計師」
_TITLES = r"(?:會計師|律師|記帳士|專利師|代理處長|處長|局長|分局長|主任|經理|科長)"

#: 只在「表頭行」遮罩，不動敘述內文。判斷條件三個都要成立：
#:   1. 標籤在行首（允許 PDF 抽出的行號前綴，如「05 訴訟代理人」）
#:   2. 標籤字之間可有空白（「代 表 人」）
#:   3. 姓名為 2–4 連續中文字，且其後為行尾／全形括號／職稱
#: 第 3 點是關鍵：敘述句「訴願人 遂於 113 年…」後面接的是數字，不會被誤遮；
#: 機關名（新北市政府稅捐稽徵處）超過 4 字且後接中文，也不會被當成姓名。
#: 已遮罩的「游○真」因 ○ 不在中文範圍內而斷開，不會被二次處理。
_PARTY_NAME_RE = re.compile(
    r"(?m)^(?P<prefix>[ \t　]*(?:\d{1,3}[ \t　]+)?)"
    r"(?P<label>(?:" + "|".join(r"[ \t　]*".join(lb) for lb in _PARTY_LABELS) + r"))"
    r"(?P<gap>[ \t　]{1,8})"
    r"(?P<name>[\u4e00-\u9fff]{2,4})"
    r"(?=[ \t　]*$|[ \t　]*[（(]|[ \t　]+" + _TITLES + r")"
)


#: PDF 換行可能讓敘述句剛好斷在「訴願人 遂於」這種位置，被誤認成表頭。
#: 這些是敘述用詞，不是姓名，直接排除。
_NOT_NAMES = frozenset({
    "遂於", "主張", "係第", "將系", "前於", "嗣於", "另於", "業於", "即於",
    "乃於", "雖稱", "辯稱", "遲至", "復於", "旋於", "始於", "並於", "如有",
})


def _mask_name(name: str) -> str:
    """劉杉涼 → 劉○涼；王綉 → 王○（沿用來源資料的遮罩風格：留頭尾）。"""
    if len(name) <= 2:
        return name[0] + "○"
    return name[0] + "○" * (len(name) - 2) + name[-1]


#: 「這一行是當事人欄位」的判斷（姓名可能已遮罩，故不限制姓名形狀）
_PARTY_LINE_RE = re.compile(
    r"^[ \t　]*(?:\d{1,3}[ \t　]+)?"
    r"(?:" + "|".join(r"[ \t　]*".join(lb) for lb in _PARTY_LABELS) + r")[ \t　]"
)

#: 整行只有 2–4 個中文字（判解表頭列多位代理人時的第二行以後）
_ORPHAN_LINE_RE = re.compile(r"^(?P<pad>[ \t　]*)(?P<name>[\u4e00-\u9fff]{2,4})[ \t　]*$")


def mask_orphan_party_names(text: str) -> tuple[str, int]:
    """遮罩「緊接在當事人欄位之後、獨立成行」的姓名。

    判解表頭常把多位代理人分行列出，只有第一行帶標籤：

        訴訟代理人 郭燕玲
        陳錦英          ← 沒有標籤，_PARTY_NAME_RE 抓不到

    因此以「上一行是當事人欄位」當上下文，連續遮罩後續的孤行姓名。
    """
    lines = text.split("\n")
    count = 0
    in_party_block = False

    for i, line in enumerate(lines):
        if _PARTY_LINE_RE.match(line):
            in_party_block = True
            continue
        if not in_party_block:
            continue
        m = _ORPHAN_LINE_RE.match(line)
        if m and "○" not in line and m.group("name") not in _ORPHAN_STOPWORDS:
            lines[i] = m.group("pad") + _mask_name(m.group("name"))
            count += 1
            continue                      # 可能還有下一位代理人
        in_party_block = False

    return "\n".join(lines), count


def mask_party_names(text: str) -> tuple[str, int]:
    """遮罩當事人欄位的姓名，回傳 (結果, 遮罩筆數)。"""
    count = 0

    def repl(m: re.Match[str]) -> str:
        nonlocal count
        name = m.group("name")
        if name in _NOT_NAMES:
            return m.group(0)
        count += 1
        return m.group("prefix") + m.group("label") + m.group("gap") + _mask_name(name)

    return _PARTY_NAME_RE.sub(repl, text or ""), count

#: 刻意不做「連續 N 位數字一律遮罩」：案號（1131061233）、公文字號
#: （新北府訴決字第 1131914523 號）、函釋字號（法律字第 0930014628 號）都是
#: 10 位數，一律遮掉會毀掉引用追溯性，而帳號在來源資料已遮成末 4 碼。
#: 遮罩只針對規範明列的個資型別：身分證號、地址、行動電話。


def deidentify(text: str) -> tuple[str, dict[str, int]]:
    """遮罩身分證號、地址與行動電話，回傳 (遮罩後文字, 各類命中數)。

    只套用在敘述性內容（事實、理由、全文），不套用在案號/字號等識別欄位——
    那些是追溯依據，不是個資。
    """
    counts: dict[str, int] = {}

    def sub(pattern: re.Pattern[str], label: str, value: str) -> str:
        value, n = pattern.subn(MASK, value)
        if n:
            counts[label] = counts.get(label, 0) + n
        return value

    text = sub(_ROC_ID_RE, "id", text)
    text = sub(_ADDR_RE, "address", text)
    text = sub(_MOBILE_RE, "phone", text)

    for masker, label in ((mask_party_names, "party_name"),
                          (mask_orphan_party_names, "party_name_orphan")):
        text, n = masker(text)
        if n:
            counts[label] = counts.get(label, 0) + n
    return text, counts


#: 遮罩後的殘留檢查：獨立成行、2–4 個中文字且不含「○」的行，很可能是
#: 沒有標籤的第二位當事人（表頭常見「訴訟代理人 甲\n乙」的第二行）。
#: 只回報不自動改寫——誤遮法條或機關名比漏遮更難事後發現。
_ORPHAN_NAME_RE = re.compile(r"(?m)^[ \t　]*([\u4e00-\u9fff]{2,4})[ \t　]*$")

#: 常見的獨立成行短詞，不是姓名，排除以降低雜訊
_ORPHAN_STOPWORDS = frozenset({
    "主文", "事實", "理由", "附件", "說明", "要旨", "解釋文", "解釋爭點",
    "壹", "貳", "參", "肆", "全文", "教示", "決定書", "訴願人", "原處分機關",
})


def name_suspects(text: str) -> list[str]:
    return sorted({m for m in _ORPHAN_NAME_RE.findall(text or "")
                   if "○" not in m and m not in _ORPHAN_STOPWORDS})


# ---------- 文字組裝 ----------

def roc_year(publish_date: str) -> int:
    """2024-11-28 → 113（民國年）。解析不出來回 0。"""
    m = re.match(r"(\d{4})", publish_date or "")
    return int(m.group(1)) - 1911 if m else 0


def decision_to_text(doc: dict, mask: Callable[[str], str]) -> str:
    """歷史決定書 → 純文字。優先用結構化欄位，缺漏才退回 full_text。

    刻意把「主文」放在最前段：`similar_cases._disposition` 在 metadata 缺值時
    會從內文前 200 字找主文，順序錯了會判不出來。

    mask 只套用在敘述段落，案號/字號等識別欄位保留原樣（追溯用）。
    """
    parsed = doc.get("parsed") or {}
    head = [
        f"案號：{doc.get('case_number', '')}",
        f"案由：{doc.get('title', '')}",
        f"法規類別：{doc.get('law_category', '')}",
    ]
    # 通用類額外標出實際案由/法規，語意檢索才對得上（general 的 law_category 一律「通用」）
    source_law = normalize_variants((doc.get("source_law") or "").strip())
    if source_law:
        head.append(f"實際案由：{source_law}")
    head += [
        f"決定書字號：{doc.get('dispatch_number', '')}",
        f"決定日期：{doc.get('publish_date', '')}",
        f"主文：{parsed.get('main_ruling') or doc.get('ruling_label', '')}",
        f"原處分機關：{parsed.get('original_agency', '')}",
        f"相關法條：{'、'.join(doc.get('related_articles') or [])}",
    ]
    body = [
        ("事實", parsed.get("facts")),
        ("理由", parsed.get("reasons")),
        ("教示", parsed.get("remedy_instruction")),
    ]
    sections = [f"【{name}】\n{mask(value.strip())}" for name, value in body
                if isinstance(value, str) and value.strip()]
    if not sections:
        sections = [f"【全文】\n{mask((doc.get('full_text') or '').strip())}"]
    return "\n".join(head) + "\n\n" + "\n\n".join(sections) + "\n"


def statute_to_text(doc: dict, mask: Callable[[str], str]) -> str:
    """法規 → 純文字。每條冠上法規全名，否則 citations.CITE_RE 認不出條號。"""
    law_name = normalize_variants(doc.get("law_name") or doc.get("title") or "")
    lines = [
        f"法規名稱：{law_name}",
        f"最新修正：{doc.get('amended_date', '')}",
        "",
    ]
    for art in doc.get("articles") or []:
        no = re.sub(r"\s+", " ", str(art.get("article_no", "")).strip())
        content = (art.get("content") or "").strip()
        if not content:
            continue
        # 「洗錢防制法第 22 條」——法名 + 條號相連，CITE_RE 才抓得到
        lines.append(f"{law_name}{no}")
        lines.append(mask(content))
        lines.append("")
    if len(lines) <= 3:                       # 沒有 articles（例如只有 full_text）
        lines.append(mask((doc.get("full_text") or "").strip()))
    return "\n".join(lines).rstrip() + "\n"


def doc_to_text(doc: dict, mask: Callable[[str], str]) -> str:
    """函釋 / 判解 → 純文字（這兩類只有 title + full_text）。"""
    title = doc.get("title") or doc.get("source_file") or ""
    return f"標題：{title}\n\n{mask((doc.get('full_text') or '').strip())}\n"


# ---------- metadata ----------

def build_metadata(attrs: dict, fmt: str) -> dict:
    """產生 Bedrock KB 旁掛 metadata。

    fmt="typed"  managed KB 文件示範的形狀（{"value":{"type":...}}）
    fmt="simple" 傳統 S3 資料來源的扁平形狀（key → 值）
    兩種形狀不同 KB 型別接受度不同，故做成可切換；預設 typed（本專案用 managed KB）。
    """
    if fmt == "simple":
        return {"metadataAttributes": {k: v for k, v in attrs.items() if v not in (None, "")}}

    out: dict[str, dict] = {}
    for key, value in attrs.items():
        if value in (None, ""):
            continue
        if isinstance(value, bool):
            out[key] = {"value": {"type": "BOOLEAN", "booleanValue": value}}
        elif isinstance(value, (int, float)):
            out[key] = {"value": {"type": "NUMBER", "numberValue": value}}
        else:
            out[key] = {"value": {"type": "STRING", "stringValue": str(value)}}
    return {"metadataAttributes": out}


def decision_attrs(route_key: str, doc: dict) -> dict:
    # Bedrock KB 的 .metadata.json 硬限制 1024 bytes，只留過濾與溯源必要欄位。
    # source_url 不放（不參與過濾，溯源用 case_id + doc_id 已足夠），full_text
    # 已含完整資訊。這也是決定書 metadata 塞不進 1KB 的主因。
    attrs = {
        "case_type": route_key,
        "doc_type": "decision",
        "year": roc_year(doc.get("publish_date", "")),
        "disposition": doc.get("ruling_label", ""),      # 主文多數決要用
        "ruling_class": doc.get("ruling_class", ""),
        "doc_id": doc.get("dispatch_number") or doc.get("case_number", ""),
        "case_id": doc.get("case_number", ""),
    }
    # 通用類（catch-all）的實際案由/法規，讓 general KB 內還能再細分過濾與溯源。
    # 只有 general 案有此欄位。來源資料偶有把整個案由標題灌進 source_law（含換行），
    # 故正規化＋截短，避免撐爆 1KB 且保持可當過濾值。
    source_law = _clean_source_law(doc.get("source_law"))
    if source_law:
        attrs["source_law"] = source_law
    return attrs


def _clean_source_law(raw: str | None, limit: int = 30) -> str:
    """source_law 正規化：去換行/多餘空白、異體字，截短到 limit 字。"""
    if not raw:
        return ""
    value = normalize_variants(re.sub(r"\s+", "", raw))
    return value[:limit]


def law_attrs(route_key: str, doc: dict, doc_type: str) -> dict:
    law_name = normalize_variants(doc.get("law_name") or doc.get("title") or "")
    is_common = doc_type == "law" and law_name in COMMON_LAWS
    return {
        "case_type": route_key,
        "doc_type": "common_law" if is_common else doc_type,
        "law_name": law_name,
        "amended_date": doc.get("amended_date", ""),
        "year": 0,
    }


# ---------- 轉檔主流程 ----------

@dataclass
class Stats:
    decisions: int = 0
    laws: int = 0
    skipped: int = 0
    oversized: int = 0       # metadata 超過 1024 bytes，會被 Bedrock 略過
    masked: dict[str, int] = field(default_factory=dict)
    suspects: list[str] = field(default_factory=list)

    def add_mask(self, counts: dict[str, int]) -> None:
        for k, v in counts.items():
            self.masked[k] = self.masked.get(k, 0) + v


def bucket_name(route_key: str, account: str) -> str:
    return f"{TOPIC_SOURCES[route_key]['bucket']}-{account}"


def _list_keys(s3, bucket: str, prefix: str) -> list[str]:
    keys: list[str] = []
    token: str | None = None
    while True:
        kwargs = {"Bucket": bucket, "Prefix": prefix}
        if token:
            kwargs["ContinuationToken"] = token
        resp = s3.list_objects_v2(**kwargs)
        for obj in resp.get("Contents") or []:
            key = obj["Key"]
            if key.endswith(".json") and not key.endswith(".metadata.json"):
                keys.append(key)
        if not resp.get("IsTruncated"):
            return keys
        token = resp.get("NextContinuationToken")


#: Bedrock KB 對隨附 .metadata.json 的硬限制
METADATA_SIZE_LIMIT = 1024


def _write(out_dir: Path, stem: str, text: str, attrs: dict, fmt: str) -> int:
    """寫出 .txt 與 .metadata.json，回傳 metadata 位元組數（供超限檢查）。

    metadata 用 compact JSON（無縮排）：indent 會多出可觀空白，決定書欄位多，
    很容易因此超過 1024 bytes 被 Bedrock 整份略過。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^\w\u4e00-\u9fff.\-]+", "_", stem)[:120]
    (out_dir / f"{safe}.txt").write_text(text, encoding="utf-8")
    blob = json.dumps(build_metadata(attrs, fmt), ensure_ascii=False,
                      separators=(",", ":"))
    (out_dir / f"{safe}.txt.metadata.json").write_text(blob, encoding="utf-8")
    return len(blob.encode("utf-8"))


def _fetch_docs(s3, bucket: str, keys: list[str], workers: int):
    """並行抓取並解析 JSON，逐筆 yield (key, doc|None)。

    每個案由有數百份文件，逐筆序列 GET 要好幾分鐘。S3 不受 Bedrock 的 1 RPS 限制，
    這裡放心開執行緒（boto3 client 本身可多執行緒共用）。
    """
    from concurrent.futures import ThreadPoolExecutor

    def one(key: str):
        try:
            body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
            return key, json.loads(body.decode("utf-8"))
        except Exception as exc:                           # noqa: BLE001
            print(f"  ! 讀取失敗 {key}: {exc}")
            return key, None

    with ThreadPoolExecutor(max_workers=workers) as pool:
        yield from pool.map(one, keys)


def convert_route(s3, route_key: str, out_root: Path, account: str, fmt: str,
                  limit: int = 0, print_sample: bool = False,
                  workers: int = 16) -> Stats:
    """轉換單一案由的決定書與相關法規。"""
    stats = Stats()
    bucket = bucket_name(route_key, account)
    src = TOPIC_SOURCES[route_key]

    def mask(text: str) -> str:
        masked, counts = deidentify(text)
        stats.add_mask(counts)
        return masked

    groups = [(src["decisions"], "decisions"), (RELATED_PREFIX, "laws")]
    for prefix, kind in groups:
        keys = _list_keys(s3, bucket, prefix)
        if limit:
            keys = keys[:limit]
        for key, doc in _fetch_docs(s3, bucket, keys, workers):
            if doc is None:
                stats.skipped += 1
                continue

            if kind == "decisions":
                text = decision_to_text(doc, mask)
                attrs = decision_attrs(route_key, doc)
                stem = doc.get("case_number") or Path(key).stem
            else:
                sub = key[len(RELATED_PREFIX):].split("/", 1)[0]
                doc_type = RELATED_DOCTYPE.get(sub)
                if not doc_type:
                    stats.skipped += 1
                    continue
                text = (statute_to_text(doc, mask) if doc_type == "law"
                        else doc_to_text(doc, mask))
                attrs = law_attrs(route_key, doc, doc_type)
                stem = attrs["law_name"] or Path(key).stem

            stats.suspects.extend(name_suspects(text))

            if print_sample and (stats.decisions + stats.laws) == 0:
                print(f"\n----- 轉出範例 {key} -----\n{text[:800]}\n"
                      f"----- metadata -----\n"
                      f"{json.dumps(build_metadata(attrs, fmt), ensure_ascii=False, indent=2)}\n")

            size = _write(out_root / route_key / kind, stem, text, attrs, fmt)
            if size > METADATA_SIZE_LIMIT:
                stats.oversized += 1     # 會被 Bedrock 以「metadata 超限」略過
            if kind == "decisions":
                stats.decisions += 1
            else:
                stats.laws += 1

    stats.suspects = sorted(set(stats.suspects))
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(
        description="把 S3 上的案由 JSON 轉成 Bedrock KB 可索引的 .txt + metadata")
    parser.add_argument("--routes", default=",".join(DEFAULT_ROUTES),
                        help=f"逗號分隔，可選 {list(TOPIC_SOURCES)}")
    parser.add_argument("--out", default="",
                        help="轉檔輸出目錄。留空則用系統暫存目錄（上傳後自動清除）；"
                             "指定路徑則保留供檢視，不清除。")
    parser.add_argument("--account", default=ACCOUNT_ID)
    parser.add_argument("--region", default="us-west-2")
    # 預設 simple：typed 形狀約大 2.5 倍，決定書 metadata 會超過 Bedrock 的 1KB 限制
    parser.add_argument("--metadata-format", choices=("typed", "simple"), default="simple")
    parser.add_argument("--limit", type=int, default=0, help="每個前綴只取前 N 筆（試轉用）")
    parser.add_argument("--print-sample", action="store_true", help="印出第一筆轉出結果")
    parser.add_argument("--workers", type=int, default=16, help="S3 下載並行數")
    parser.add_argument("--upload", default=DEFAULT_UPLOAD_DEST,
                        help="轉完後上傳到 s3://bucket/prefix/。預設為 KB source bucket；"
                             "設為空字串可只轉檔不上傳。")
    parser.add_argument("--keep", action="store_true",
                        help="即使用暫存目錄也保留轉檔結果，不自動清除")
    args = parser.parse_args()

    import boto3

    s3 = boto3.client("s3", region_name=args.region)
    routes = [r.strip() for r in args.routes.split(",") if r.strip()]

    # 輸出目錄策略：預設用系統暫存目錄，上傳後清掉——轉檔結果只是中間產物，
    # KB 真正吃的是上傳到 S3 的那份，本地不必長期保留（也避免與雲端資料重複）。
    # 使用者若明確指定 --out 或 --keep，則保留供人工檢視。
    using_temp = not args.out
    out_root = Path(tempfile.mkdtemp(prefix="ntpc_kb_source_")) if using_temp else Path(args.out)
    cleanup = using_temp and not args.keep

    total = Stats()
    for route in routes:
        if route not in TOPIC_SOURCES:
            print(f"跳過未知 route_key：{route}")
            continue
        print(f"[{route}] 來源 s3://{bucket_name(route, args.account)}")
        st = convert_route(s3, route, out_root, args.account, args.metadata_format,
                           limit=args.limit, print_sample=args.print_sample,
                           workers=args.workers)
        print(f"  決定書 {st.decisions} 筆、法規 {st.laws} 筆、略過 {st.skipped} 筆")
        if st.oversized:
            print(f"  ⚠ metadata 超過 {METADATA_SIZE_LIMIT} bytes：{st.oversized} 筆"
                  f"（會被 Bedrock 略過，需再精簡）")
        if st.masked:
            print(f"  已遮罩：{st.masked}")
        if st.suspects:
            print(f"  ⚠ 疑似未遮罩姓名（請人工確認）：{st.suspects[:10]}")
        total.decisions += st.decisions
        total.laws += st.laws
        total.skipped += st.skipped
        total.oversized += st.oversized
        total.add_mask(st.masked)
        total.suspects.extend(st.suspects)

    print(f"\n合計：決定書 {total.decisions}、法規 {total.laws}，輸出於 {out_root}")
    if total.masked:
        print(f"遮罩統計：{total.masked}")
    if total.suspects:
        print(f"⚠ 全部疑似殘留姓名：{sorted(set(total.suspects))}")

    try:
        if args.upload:
            _upload(out_root, args.upload, args.region)
        else:
            print("（未指定 --upload，僅轉檔未上傳）")
    finally:
        # 用暫存目錄且未要求保留時清除；上傳失敗也照清（原始資料仍在 S3，可重跑）
        if cleanup:
            shutil.rmtree(out_root, ignore_errors=True)
            print(f"已清除暫存目錄 {out_root}")
        elif using_temp:
            print(f"（--keep）保留暫存轉檔於 {out_root}")


def _upload(out_root: Path, dest: str, region: str) -> None:
    """上傳到 S3。走 aws s3 sync，比逐檔 put_object 快得多。"""
    import subprocess

    dest = dest if dest.endswith("/") else dest + "/"
    print(f"上傳 {out_root} → {dest}")
    subprocess.run(["aws", "s3", "sync", str(out_root), dest, "--region", region],
                   check=True)


if __name__ == "__main__":
    main()
