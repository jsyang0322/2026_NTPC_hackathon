# 競賽規範硬性限制（必須遵守）

來源：2026 NTPC Hackathon「一般性使用規範與限制」與「Amazon Bedrock 規範與限制」。
違反這些會直接影響評分，寫任何程式碼前先確認不牴觸。

## Amazon Bedrock

**請求速率 ≤ 1 RPS**（規範原文：每秒 1 個請求以下）。

實作上分兩層，缺一不可：

| 層次 | 機制 | 解決的問題 |
|---|---|---|
| 行程內 | `threading.Lock` | 同一行程的多執行緒 |
| 跨行程 | 檔案鎖 + 時間戳 | 多個 Python 行程同時呼叫 |

規則：

- 所有 LLM 呼叫走 `core/bedrock_client.py` 的 `BedrockClient.converse()`。
- 因 API 形狀不同而必須自行 `boto3` 呼叫者（KB `retrieve`、
  `henry/rag_triage.py` 的 `invoke_model`），**呼叫前必須先呼叫
  `core.bedrock_client.throttle()`**，共用同一個閘門。
- 不要在任何模組自建限流器。兩個各守 1 RPS 的限流器並跑等於 2 RPS，違反規範。
- 最小間隔預設 1.05 秒（約 0.95 RPS），留安全邊際避免踩邊界。

**模型存取權**：只申請專案實際用到的模型，不要全開。目前使用：

- Claude Sonnet 4.5（撰稿、對抗式審查）
- Claude Haiku 4.5（輕量任務）
- Cohere Embed Multilingual v3（KB 向量化）
- Amazon Nova Lite（henry 的受理判定）

模型 ID 注意：Claude Sonnet 4.5 / Haiku 4.5 不支援裸模型 ID 的 on-demand 呼叫，
必須用 inference profile ID（加 `us.` 前綴），否則 `ValidationException`。

## AWS 資源

- **S3 不得公開**：建 bucket 後必須明確呼叫 `put_public_access_block`
  四項全開，不要只依賴帳戶層級預設。
- **部署區域**：us-east-1 或 us-west-2。本專案用 **us-west-2**。
- **EC2 安全群組**不得對外全開；不建立公開存取的 RDS / EMR。
- **不做大規模模型訓練**（規範建議）。分類模型用 TF-IDF + LogisticRegression 這種輕量作法。
- 只啟動必要的執行個體數量。

## 資料

**禁止把個人資料匯入 AWS 帳戶**（規範列舉 13 類，含個人資料、健康、財務等）。

本專案的風險點：歷史訴願決定書含訴願人姓名、地址。因此：

- `data/raw/` 已列入 `.gitignore`。
- 上傳 S3 或 Bedrock KB 前必須先去識別化（遮罩姓名、地址、身分證號）。
- 這一步在 `pipeline/kb_ingest.py` 的前處理階段做，不可略過。

## 程式碼交付

- **`.kiro/` 必須保留在 repo 根目錄**，展示 specs、hooks、steering 的使用。
  絕對不要把 `.kiro/` 或其子目錄加入 `.gitignore`。
- 不得把憑證推上公開儲存庫（AWS Access Key、API Token、資料庫密碼）。
  用環境變數管理，`.gitignore` 排除 `.env`。
