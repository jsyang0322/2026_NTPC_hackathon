# S3 事件觸發設定（④ 全自動 pipeline）

> 上傳案卷 JSON 到 S3 → 自動啟動 Step Functions 狀態機,跑完整條草稿 pipeline。

## 資料流

```
承辦人上傳 input/xxx.json 到 S3
      │ S3 事件通知（s3:ObjectCreated:*，prefix=input/，suffix=.json）
      ▼
Lambda: ntpc-s3-trigger（s3_trigger_handler.py）
      │ sfn.start_execution
      ▼
Step Functions 狀態機（state_machine.asl.json）
  Analyze → [不受理?] → Draft → Verify
      ▼
結果寫回 S3 result/xxx.json
```

## 部署步驟（開賽時做）

1. **打包 Lambda**：`core/` + `lambda_handlers/` 一起打包（或用 Layer 放 core）。
2. **建 4 個 Lambda 函式**（區域 us-west-2）：
   - `ntpc-analyze`、`ntpc-draft`、`ntpc-verify`、`ntpc-s3-trigger`
   - 執行角色需權限：`bedrock:InvokeModel`、`bedrock:Retrieve`、`s3:GetObject/PutObject`、`states:StartExecution`
   - 環境變數：`PIPELINE_BUCKET`、`STATE_MACHINE_ARN`、`BEDROCK_KB_ID`
3. **建狀態機**：用 `state_machine.asl.json`，把 `ACCOUNT_ID` 換成實際帳號。
4. **設 S3 事件通知**：
   - Bucket → Properties → Event notifications → Create
   - Event types：`s3:ObjectCreated:*`
   - Prefix：`input/`，Suffix：`.json`
   - Destination：Lambda `ntpc-s3-trigger`
5. **測試**：上傳一份 `input/sim-114-001.json`，到 Step Functions 主控台看執行圖一格格亮。

## 競賽定位

- **Demo 用單體應用**（Streamlit，穩定好除錯）。
- **此全自動架構畫進簡報**，回答「如何規模化」：各步驟獨立 Lambda 可水平擴展，≤1 RPS 由狀態機序列化 + 各 Lambda 內限流共同保證。
- 服務皆在支援清單內：S3、Lambda、Step Functions、Bedrock。不使用 App Runner。

## 全自動判定主文（v1.3）

`analyze_handler` 呼叫 `core.pipeline.analyze_case`,由 `_decide_disposition()` 依
「健檢紅燈 → 相似案例主文多數決 → 預設駁回」自動判定主文,寫入 `analysis.decided_disposition`；
`draft_handler` 直接讀取該值撰稿,狀態機不需暫停等待人工確認。

品質把關改由機器承擔:`verify_handler` 的規則驗證（V1–V10)與對抗式審查結果一併寫入
`result/xxx.json`,`verification.blocking` 非空即代表該件需事後抽查,但不阻斷流程。
