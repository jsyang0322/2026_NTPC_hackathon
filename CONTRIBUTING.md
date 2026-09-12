# 分工認領表（隊友接手指南）

> 骨架已串通，`python -m core.pipeline --dry-run` 可從頭跑到尾。每個 `core/` 函式都是一個「洞」，
> 你只要把對應函式的 stub 換成真實實作，**保持輸入/輸出的 dict 形狀不變**，就不會影響別人。

## 定案設計（請勿更動架構）

1. **4 個案由專責 = 1 顆 Claude（`MODEL_WRITER`）+ 4 種 prompt/RAG 範圍**，不是 4 顆模型、不是 Bedrock Agents。
2. **RAG**：法規／函釋／判解可依案由縮小檢索；歷史案例用「共池 + 案由加權」，不硬過濾（§9.2）。
3. **寫草稿用 Bedrock** = `generate_draft()` 組 prompt 後**一次** `client.converse` 呼叫 Claude。
4. **所有 LLM 呼叫一律走 `core/bedrock_client.py`**（≤1 RPS 限流 + 快取）。禁止在其他檔案直接 `import boto3` 打 Bedrock。
5. 確定性工作（日期、時效、程序、教示）用規則，**不交給 LLM**。

## 三條規則（讓大家不衝突）

1. **只改自己認領的檔案**，不改別人的函式簽名。
2. **維持 dict 契約**：輸入輸出的 key 不變（見 `core/schemas.py`）。要加欄位先在群組講。
3. **改完跑一次** `python -m core.pipeline --dry-run`，確認整條流程沒被你改壞。

## 認領表

| 檔案 / 函式 | 職責（說明書節） | 輸入 → 輸出 | 用 Bedrock? | 認領人 |
|---|---|---|---|---|
| `pipeline/parse.py` `split_sections` | PDF 結構切分（§4.3） | 文字 → 段落角色 dict | 否 | ____ |
| `core/extract.py` `extract_fields` | 案卷欄位擷取（§5.2） | case_text → 欄位 dict | ✅（範本抄 draft.py） | ____ |
| `core/classify.py` `classify_case_type` | 案由分類（§5.3） | case_text → {case_type,confidence} | 否（sklearn 模型） | ____ |
| `core/issues.py` `tag_issues` | 爭點標註（§5.4） | fields → issues dict | ✅ | ____ |
| `core/timeline.py` `build_timeline` | 時間軸/期間（§6） | fields → timeline dict | 否（純規則） | ____ |
| `core/procedure.py` `procedure_check` | 閘門1 程序審查（§6.2） | timeline,fields → 結果 dict | 否（純規則） | ____ |
| `core/defects.py` `health_check` | 閘門2 原處分健檢（§7） | fields,timeline → CheckResult[] | 部分（D7–D13 合併1次） | ____ |
| `pipeline/build_index.py` + `core/similar_cases.py` | F3 相似案例 RAG（§9） | fields,index → SimilarCase[] | ✅ embedding+rerank | ____ |
| `pipeline/build_citations.py` + `core/recommend_laws.py` | F2 法規推薦（§8） | fields,issues → 推薦清單 | 否（共現）/部分 | ____ |
| `core/draft.py` `generate_draft` | F4 草稿生成（§10） | 主文,fields,laws,cases → 草稿 dict | ✅（已示範寫法） | ____ |
| `core/verify.py` `verify_draft` | 自動驗證（§11.1） | draft,... → 驗證報告 | 否（純規則） | ____ |
| `core/critic.py` `adversarial_review` | 對抗式審查（§11.2） | draft,fields → 風險報告 | ✅ | ____ |
| `app/streamlit_app.py` | 審閱介面（§14） | — | 否 | ____ |

## 怎麼在流程裡串 LLM（照抄 draft.py）

要用 Bedrock 的函式（extract / issues / critic…），照 `core/draft.py` 的 `generate_draft` 寫法：

```python
from .bedrock_client import get_client, MODEL_LIGHT

def your_function(data, client=None):
    client = client or get_client()
    prompt = f"..."                       # 組你的 prompt
    text = client.converse(               # ← 一次呼叫，自動 ≤1 RPS + 快取
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        model_id=MODEL_LIGHT,             # 輕任務用 LIGHT，寫作用 MODEL_WRITER
        temperature=0.2,
    )
    return parse(text)                    # 解析成 dict
```

## 本機驗證

```bash
pip install -r requirements.txt

# 不呼叫真實 Bedrock，驗證流程串接
python -m core.pipeline --dry-run

# 只跑檔名統計（不需 PyMuPDF）
python -m pipeline.analyze --raw data/raw

# 解析 PDF（需 pymupdf）
python -m pipeline.parse --raw data/raw --out data/parsed

# 介面
streamlit run app/streamlit_app.py
```

## 真的要打 Bedrock 時

- `get_client(dry_run=False)`，或設環境變數 `BEDROCK_DRY_RUN=0`。
- 憑證用 `~/.aws` 的 default profile（Workshop Studio 臨時憑證，過期換新）。
- 記得 ≤1 RPS 已由 client 保證；別自己另開 boto3 繞過它。
