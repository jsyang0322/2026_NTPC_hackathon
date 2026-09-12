# 新北市訴願案件審理 AI 輔助系統（ntpc-appeal-ai）

> 對應《訴願決定書AI輔助系統_實作說明書.md》。核心原則：**先審查、後撰稿**；確定性工作交給規則，LLM 只做理解與寫作，每句法律依據可追溯。

## 架構原則：一套核心，多種外殼

業務邏輯全部寫在 `core/` 的純函式（**dict 進、dict 出，不綁執行環境**）。四種自動化外殼共用同一份 `core/`：

| 外殼 | 觸發方式 | 定位 | 進度 |
|---|---|---|---|
| 單體應用（Streamlit） | 按鈕呼叫 `pipeline.process_case()` | Demo 主線 | 必做 |
| AWS Lambda | handler 呼叫同一批 core 函式 | 加分：可雲端化 | 主線穩後 |
| AWS Step Functions | 串接多個 Lambda 成狀態機 | 簡報「正式架構」 | 行有餘力 |
| S3 事件觸發 | 上傳案卷自動啟動 pipeline | 全自動展示 | 行有餘力 |

所有 LLM 呼叫一律走 `core/bedrock_client.py`（**全域 ≤ 1 RPS 限流 + 快取**，競賽硬規定）。

## 目錄結構

```
ntpc-appeal-ai/
├── core/                 # 純業務邏輯（不綁環境，四種外殼共用）
│   ├── bedrock_client.py # ★ 所有 LLM 呼叫的唯一入口（限流/快取/重試）
│   ├── schemas.py        # ★ v1.1 交接契約（route_key 枚舉、build_empty_input、validate_input）
│   ├── router.py         # 讀 route_key 分派案由設定
│   ├── kb.py             # ★ Bedrock Knowledge Bases 檢索（路線 A）
│   ├── extract.py        # F1 結構化擷取（前段/同學負責）[LLM]
│   ├── classify.py       # F1 案由分類（前段/同學負責）  [規則/模型]
│   ├── issues.py         # F1 爭點標註          [LLM]
│   ├── timeline.py       # 時間軸引擎（亮點 B） [純規則]
│   ├── procedure.py      # 閘門 1：程序審查     [純規則]
│   ├── defects.py        # 閘門 2：原處分健檢（亮點 A）[規則 + LLM]
│   ├── recommend_laws.py # F2 法規推薦          [KB 檢索]
│   ├── similar_cases.py  # F3 相似案例          [KB 檢索，共池+加權]
│   ├── draft.py          # F4 草稿生成          [LLM，CASE_PROFILES 按 route_key]
│   ├── verify.py         # 自動驗證            [純規則]
│   ├── critic.py         # 對抗式審查          [LLM]
│   └── pipeline.py       # ★ 單體主線（接 v1.1 JSON → 路由 → KB → 撰稿 → 檢核）
├── pipeline/             # 離線前處理（賽前跑一次）
│   ├── parse.py          # PDF 解析與結構切分
│   ├── analyze.py        # 資料實況統計
│   ├── kb_ingest.py      # ★ Bedrock KB 上傳前處理（正規化 + 產 metadata）
│   ├── build_citations.py# 引用與共現索引
│   ├── build_library.py  # 情境化段落庫
│   └── simulate_case.py  # 模擬案卷生成
├── app/
│   └── streamlit_app.py  # 單體介面（呼叫 pipeline）
├── lambda_handlers/      # 【階段3】Lambda 外殼（預留）
├── stepfunctions/        # 【階段4】狀態機定義（預留）
├── data/                 # raw/parsed/cache（.gitignore 排除，含個資）
├── models/               # 分類模型權重
├── prompts/              # Prompt 模板
└── requirements.txt
```

## 快速開始

```bash
python -m venv .venv
.venv\Scripts\Activate.ps1        # Windows PowerShell
pip install -r requirements.txt

# 乾跑主線（不呼叫真實 Bedrock，用 mock 資料驗證串接）
python -m core.pipeline --dry-run
```

## 競賽規範內建

- Bedrock ≤ 1 RPS：由 `bedrock_client` 全域限流強制
- 個資不上雲：`data/raw` 已 gitignore；上傳 S3 前須確認去識別化
- 不使用 Textract / App Runner / GPU 訓練
- `.kiro/` 保留於 repo（不加入 .gitignore）
