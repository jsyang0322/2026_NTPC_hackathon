# stepfunctions/（階段 4：全自動 pipeline，預留）

> 簡報「正式上線架構」用。Demo 不一定要真跑；若做出來，現場點 S3 上傳、看狀態機一格格亮綠燈是強力展示。

## 全自動流程

```
承辦人上傳案卷到 S3 (input/)
      │ S3 事件通知
      ▼
Lambda: start → 啟動 Step Functions 狀態機
      │
      ▼
狀態機：
  ├─ Extract        (extract_handler，呼叫 Bedrock)
  ├─ Analyze        (analyze_handler：時間軸 + 程序審查 + 健檢)
  │     └─ Choice：不受理 → QuickTemplate → 存 S3 → 結束
  ├─ Retrieve       (retrieve_handler：法規推薦 + 相似案例)
  ├─ Draft          (draft_handler，呼叫 Bedrock)
  └─ Verify         (verify_handler：驗證 + 對抗式審查)
      │
      ▼
結果存 S3 (output/) → 通知承辦人
```

## 檔案（待實作）

- `state_machine.asl.json`：Amazon States Language 定義
- `s3_trigger.md`：S3 事件通知 → 啟動 Lambda 的設定說明

## 競賽定位

- **Demo 用單體應用**（穩定、好除錯）。
- **此資料夾產出畫進簡報**，回答評審「如何規模化」：各步驟為獨立 Lambda，可水平擴展；≤1 RPS 由狀態機序列化與各 Lambda 內限流共同保證。
- 服務皆在支援清單內（S3、Lambda、Step Functions、Bedrock）；不使用 App Runner。
