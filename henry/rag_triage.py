# -*- coding: utf-8 -*-
"""
RAG + LLM 訴願書初步受理判定 pipeline
=========================================================
1. 知識庫: 訴願法第77條各款 + 相關要件條文,切塊。
2. 檢索:  cohere.embed-multilingual-v3 對訴願書全文檢索最相關條款。
3. 判定:  amazon.nova-lite-v1:0 讀「訴願書全文 + 檢索到的條文」,
          分辨事實 vs 當事人主張,判 受理(1)/不受理(0)+款次+法源理由。
4. 評分:  對 appeals_structured.jsonl 的 受理與否 算正確率、混淆矩陣。

判定僅根據 PDF 內文與法條;答案 JSON 只在最後評分時讀取。
用法:
  python rag_triage.py build     # 建知識庫向量索引 (kb_index.json)
  python rag_triage.py run [N]    # 對前 N 筆(預設全部) v2 PDF 判定+評分
"""
import io, os, json, sys, time, threading
from pathlib import Path
import boto3
import numpy as np
from pypdf import PdfReader

BASE_DIR = Path(__file__).resolve().parent
REGION = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-west-2"))
BUCKET = os.environ.get("APPEAL_S3_BUCKET", "bucket-jahseh")
PDF_PREFIX = os.environ.get("APPEAL_PDF_PREFIX", "原始訴願書v2_pdf/")
ANSWER_KEY = os.environ.get("APPEAL_ANSWER_KEY", "appeals_structured.jsonl")
LAW_KEY = os.environ.get("APPEAL_LAW_KEY", "資料集/相關法規/訴願法.pdf 的副本.pdf")
EMBED_MODEL = os.environ.get("BEDROCK_EMBED_MODEL", "cohere.embed-multilingual-v3")
# LLM 可用環境變數 LLM_MODEL 覆蓋。預設 Nova Lite。
#   Nova Lite: amazon.nova-lite-v1:0
#   Nova Pro : us.amazon.nova-pro-v1:0
#   Claude   : us.anthropic.claude-sonnet-4-5-20250929-v1:0
LLM_MODEL = os.environ.get("LLM_MODEL", "amazon.nova-lite-v1:0")
KB_FILE = BASE_DIR / "kb_index.json"
# 判定/預測輸出檔名帶模型標記,避免覆蓋其他模型結果。
TAG = os.environ.get("RUN_TAG", "novalite")
PRED_FILE = BASE_DIR / f"rag_predictions_{TAG}.jsonl"
REPORT_FILE = BASE_DIR / f"rag_report_{TAG}.txt"

sess = boto3.Session(region_name=REGION)
s3 = sess.client("s3")
rt = sess.client("bedrock-runtime")

# 限流統一交給 core/bedrock_client 的跨行程閘門（競賽規範 ≤ 1 RPS）。
# 本模組因使用 invoke_model（Cohere embed / Nova 的 body 格式與 converse 不同）
# 而保留自己的 boto3 client，但速率控制不自己算——否則本行程與 core 各守 1 RPS，
# 同時執行時合計 2 RPS，會違反規範。
sys.path.insert(0, str(BASE_DIR.parent))
from core.bedrock_client import throttle as _bedrock_throttle  # noqa: E402

_bedrock_lock = threading.Lock()
_min_bedrock_interval = float(os.environ.get("BEDROCK_MIN_INTERVAL", "1.05"))


def _invoke_model(**kwargs):
    """本模組所有 Bedrock 呼叫的唯一出口。

    行程內以 _bedrock_lock 序列化；跨行程速率由 core 的共用閘門保證。
    """
    with _bedrock_lock:
        _bedrock_throttle(_min_bedrock_interval)
        return rt.invoke_model(**kwargs)


def set_model(model_id, read_timeout=90, connect_timeout=10, max_attempts=2):
    """切換本模組使用的 LLM，並重建 client（為 Claude 加逾時避免卡住）。

    bedrock-runtime client 只在本模組建立，其他模組請呼叫本函式而非自行建 client——
    否則呼叫會繞過 _invoke_model 的限流閘門，可能超過競賽規範的 1 RPS。
    """
    global LLM_MODEL, rt
    from botocore.config import Config

    LLM_MODEL = model_id
    rt = sess.client(
        "bedrock-runtime",
        config=Config(read_timeout=read_timeout, connect_timeout=connect_timeout,
                      retries={"max_attempts": max_attempts}),
    )


# ---------- Bedrock helpers ----------
def embed(texts, input_type):
    """Cohere embed. input_type: search_document | search_query."""
    body = {"texts": texts, "input_type": input_type}
    r = _invoke_model(modelId=EMBED_MODEL, body=json.dumps(body))
    return json.loads(r["body"].read())["embeddings"]


def nova(prompt, system=None, max_tokens=800):
    """呼叫 LLM。依 LLM_MODEL 自動選用 Anthropic(Claude)或 Nova 的 body 格式。"""
    if "anthropic" in LLM_MODEL:
        body = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": max_tokens,
            "temperature": 0,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            body["system"] = system
        r = _invoke_model(modelId=LLM_MODEL, body=json.dumps(body))
        payload = json.loads(r["body"].read())
        return payload["content"][0]["text"]
    else:
        body = {
            "messages": [{"role": "user", "content": [{"text": prompt}]}],
            "inferenceConfig": {"maxTokens": max_tokens, "temperature": 0},
        }
        if system:
            body["system"] = [{"text": system}]
        r = _invoke_model(modelId=LLM_MODEL, body=json.dumps(body))
        payload = json.loads(r["body"].read())
        return payload["output"]["message"]["content"][0]["text"]


def pdf_text(key):
    raw = s3.get_object(Bucket=BUCKET, Key=key)["Body"].read()
    return "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(raw)).pages)


# ---------- 1. Knowledge base ----------
# 訴願法第77條各款(逐字)+ 相關要件條文,切成可檢索知識塊。
KB_CHUNKS = [
    {"id": "77", "label": "訴願法第77條(不受理總則)",
     "text": "訴願法第77條:訴願事件有左列各款情形之一者,應為不受理之決定。"},
    {"id": "77-1", "款": "77(1)", "label": "訴願法第77條第1款",
     "text": "訴願法第77條第1款:訴願書不合法定程式不能補正,或經通知補正逾期不補正者,應不受理。判斷重點:訴願書欠缺法定應記載事項(如簽名蓋章、住居所),經受理機關通知補正而屆期仍未補正。"},
    {"id": "77-2", "款": "77(2)", "label": "訴願法第77條第2款",
     "text": "訴願法第77條第2款:提起訴願逾法定期間(自收受或知悉行政處分之次日起30日內),或未於第57條但書所定期間內補送訴願書者,應不受理。判斷重點:計算收受處分日到提起訴願日是否超過30日。"},
    {"id": "77-3", "款": "77(3)", "label": "訴願法第77條第3款(當事人適格)",
     "text": "訴願法第77條第3款:訴願人不符合第18條之規定者,應不受理。第18條:自然人、法人、非法人團體或其他受行政處分之相對人及利害關係人,得提起訴願。判斷重點:提起訴願之人並非行政處分之相對人,亦非法律上利害關係人(例如處分之受處分人為他人、訴願人僅因鄰近而知悉),即當事人不適格。"},
    {"id": "77-4", "款": "77(4)", "label": "訴願法第77條第4款(訴願能力)",
     "text": "訴願法第77條第4款:訴願人無訴願能力而未由法定代理人代為訴願行為,經通知補正逾期不補正者,應不受理。判斷重點:訴願人為未成年人(未滿18歲/未成年),未由法定代理人代為或同意提起訴願。"},
    {"id": "77-6", "款": "77(6)", "label": "訴願法第77條第6款(原處分不存在)",
     "text": "訴願法第77條第6款:行政處分已不存在者,應不受理。判斷重點:原行政處分已經原機關自行撤銷、已執行完畢而效力消滅、因期間屆至而失效、或已廢止,提起訴願時處分已不存在。"},
    {"id": "77-7", "款": "77(7)", "label": "訴願法第77條第7款(重複提起)",
     "text": "訴願法第77條第7款:對已決定或已撤回之訴願事件重行提起訴願者,應不受理。判斷重點:就同一行政處分/書面告誡,先前已提起訴願並經作成決定或撤回,又再次提起。"},
    {"id": "77-8", "款": "77(8)", "label": "訴願法第77條第8款(非行政處分)",
     "text": "訴願法第77條第8款:對於非行政處分,或其他依法不屬訴願救濟範圍內之事項提起訴願者,應不受理。判斷重點:所爭執之標的非屬行政處分(例如檢舉處理情形回覆函、說明性質函文、觀念通知),未對訴願人科處義務、限制或剝奪權利、亦未准駁申請,不生法律效果。"},
    {"id": "accept", "款": None, "label": "受理(程序合法)",
     "text": "訴願受理:若訴願未逾30日法定期間,訴願人為處分相對人或利害關係人且有訴願能力,標的為有效存在之行政處分,程式完備,且非重複提起,則程序要件具備,應予受理並進入實體審查(縱使訴願人於理由中所述實體主張是否有理由,屬受理後之實體判斷)。"},
]


def build_kb():
    texts = [c["text"] for c in KB_CHUNKS]
    vecs = embed(texts, "search_document")
    data = {"chunks": KB_CHUNKS, "vectors": vecs}
    with open(KB_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    print(f"KB built: {len(KB_CHUNKS)} chunks -> {KB_FILE}")


def load_kb():
    with open(KB_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data["chunks"], np.array(data["vectors"], dtype=np.float32)


# ---------- 2. Retrieval ----------
def retrieve(query_text, chunks, mat, k=5):
    qv = np.array(embed([query_text], "search_query")[0], dtype=np.float32)
    # cosine
    sims = mat @ qv / (np.linalg.norm(mat, axis=1) * np.linalg.norm(qv) + 1e-9)
    order = np.argsort(-sims)[:k]
    return [(chunks[i], float(sims[i])) for i in order]


# ---------- 3. LLM triage ----------
SYSTEM = (
    "你是台灣新北市政府訴願審議的承辦助理。你的任務是做『初步受理審查』:"
    "只判斷程序上應否受理,不做實體有無理由的判斷。"
    "重要原則:要嚴格分辨訴願書中的『事實陳述』(客觀發生的程序事實)與訴願人的"
    "『主張/請求』(訴願人自我辯護、請求受理的說法)。不受理與否取決於客觀程序事實,"
    "不受訴願人主張影響。例如訴願人主張自己是利害關係人,但事實顯示處分相對人是他人,"
    "仍屬當事人不適格。請依訴願法第77條各款認定。"
)

PROMPT_TMPL = """以下為一份訴願書全文(由 PDF 擷取):
---訴願書開始---
{doc}
---訴願書結束---

以下為檢索到最相關的訴願法條文(供你認定法源):
{laws}

請做初步受理審查,只輸出一個 JSON 物件,格式如下(不要多餘文字):
{{"受理與否": "受理" 或 "不受理", "款次": "77(x)" 或 null, "信心": "高" 或 "中" 或 "低", "理由": "簡要說明,須指出關鍵程序事實與法源"}}

判斷步驟:
1. 從『事實』段找客觀程序事實:收受處分日、提起訴願日(算是否逾30日)、訴願人是否為處分相對人/利害關係人、是否成年及有無法定代理人、標的是否為行政處分、原處分是否仍存在、是否重複提起、程式是否完備且經通知補正。
2. 忽略訴願人在『理由』段為自己受理所做的主張。
3. 只要有任一不受理事由即為「不受理」並指出款次;全部要件具備才「受理」(款次 null)。
4. 「信心」:若程序事實明確、認定無疑義填「高」;若事實需權衡或標的性質(是否行政處分/原處分是否仍存在)不易判斷填「中」或「低」。
"""


def parse_json_loose(s):
    """從 LLM 回覆中抽出 JSON。"""
    s = s.strip()
    a, b = s.find("{"), s.rfind("}")
    if a >= 0 and b > a:
        try:
            return json.loads(s[a:b+1])
        except Exception:
            pass
    return None


def triage_one(doc_text, chunks, mat):
    """單筆判定。回傳 (label, 款次, 理由, raw, 信心)。label:1=受理,0=不受理。"""
    hits = retrieve(doc_text, chunks, mat, k=5)
    laws = "\n".join(f"- {c['label']}: {c['text']}" for c, _ in hits)
    prompt = PROMPT_TMPL.format(doc=doc_text[:6000], laws=laws)
    raw = nova(prompt, system=SYSTEM, max_tokens=500)
    obj = parse_json_loose(raw)
    if not obj:
        return None, None, "LLM 回覆無法解析: " + raw[:200], raw, None
    label = 1 if obj.get("受理與否") == "受理" else 0
    return label, obj.get("款次"), obj.get("理由", ""), raw, obj.get("信心")


# ---------- 4. Run + evaluate ----------
def run(limit=None):
    chunks, mat = load_kb()

    keys = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix=PDF_PREFIX):
        for o in page.get("Contents", []):
            if o["Key"].lower().endswith(".pdf"):
                keys.append(o["Key"])
    keys.sort()
    if limit:
        keys = keys[:limit]

    # answers (only for scoring)
    ans_txt = s3.get_object(Bucket=BUCKET, Key=ANSWER_KEY)["Body"].read().decode("utf-8")
    truth = {}
    for l in ans_txt.splitlines():
        if l.strip():
            r = json.loads(l)
            truth[r["案號"]] = (1 if r.get("受理與否") == "受理" else 0, r.get("不受理款次"))

    preds = {}
    for i, key in enumerate(keys, 1):
        cid = key.split("/")[-1].rsplit(".", 1)[0]
        for attempt in range(3):
            try:
                text = pdf_text(key)
                label, ground, reason, raw, conf = triage_one(text, chunks, mat)
                preds[cid] = {"label": label, "款次": ground, "理由": reason, "信心": conf}
                break
            except Exception as e:
                if attempt == 2:
                    preds[cid] = {"label": None, "款次": None, "理由": "ERROR:" + repr(e)}
                else:
                    time.sleep(2 * (attempt + 1))  # backoff for throttling
        t = truth.get(cid)
        mark = "?" if preds[cid]["label"] is None else preds[cid]["label"]
        ok = "" if t is None or preds[cid]["label"] is None else ("OK" if preds[cid]["label"] == t[0] else "XX")
        print(f"[{i}/{len(keys)}] {cid} -> {mark} (真:{t[0] if t else '?'}) {ok}", flush=True)
        # incremental save
        with open(PRED_FILE, "w", encoding="utf-8") as f:
            for c, p in preds.items():
                tt = truth.get(c)
                f.write(json.dumps({"案號": c, **p,
                                    "正解": (tt[0] if tt else None),
                                    "正解款次": (tt[1] if tt else None)}, ensure_ascii=False) + "\n")

    # score
    correct = wrong = skipped = 0
    tp = tn = fp = fn = 0
    wrong_list = []
    for cid, p in preds.items():
        t = truth.get(cid)
        if t is None or p["label"] is None:
            skipped += 1
            continue
        pl, tl = p["label"], t[0]
        if pl == tl:
            correct += 1
        else:
            wrong += 1
            wrong_list.append({"案號": cid, "預測": pl, "正解": tl,
                               "預測款次": p["款次"], "正解款次": t[1], "理由": p["理由"]})
        if pl == 1 and tl == 1: tp += 1
        elif pl == 0 and tl == 0: tn += 1
        elif pl == 1 and tl == 0: fp += 1
        elif pl == 0 and tl == 1: fn += 1

    n = correct + wrong
    lines = []
    lines.append("=== RAG + Nova Lite 判定結果 ===")
    lines.append(f"評分筆數: {n} (跳過/錯誤: {skipped})")
    lines.append(f"判斷正確: {correct}")
    lines.append(f"判斷錯誤: {wrong}")
    if n:
        lines.append(f"正確率: {correct/n*100:.1f}%   (規則式 baseline 57.6% / Nova Lite RAG 87.1%)")
        lines.append(f"模型: {LLM_MODEL}")
    lines.append("")
    lines.append("混淆矩陣:")
    lines.append(f"  TP 正確受理:{tp}  TN 正確不受理:{tn}  FP 誤判受理:{fp}  FN 誤判不受理:{fn}")
    lines.append("")
    if wrong_list:
        lines.append("=== 錯誤清單 ===")
        for w in wrong_list:
            lines.append(json.dumps(w, ensure_ascii=False))
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print("\n".join(lines[:8]))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "build":
        build_kb()
    else:
        lim = int(sys.argv[2]) if len(sys.argv) > 2 else None
        run(lim)
