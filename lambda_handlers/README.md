# lambda_handlers/（階段 3：Lambda 外殼，預留）

> 主線（單體 Streamlit）穩定後才做。每個 handler 只負責「從 S3 拿輸入 → 呼叫同一批 core 函式 → 結果寫回 S3」，**不重寫任何業務邏輯**。

## 對應關係

| Lambda handler | 呼叫的 core 函式 | Bedrock |
|---|---|---|
| `extract_handler` | `core.extract.extract_fields` + `core.classify.classify_case_type` | ✓ |
| `analyze_handler` | `core.timeline` + `core.procedure` + `core.defects.health_check` | 部分 |
| `retrieve_handler` | `core.recommend_laws` + `core.similar_cases.find_similar` | ✓ |
| `draft_handler` | `core.draft.generate_draft` | ✓ |
| `verify_handler` | `core.verify.verify_draft` + `core.critic.adversarial_review` | 部分 |

## Handler 通用形狀

```python
# lambda_handlers/extract_handler.py（範例，尚未實作）
import json, boto3
from core.extract import extract_fields
from core.classify import classify_case_type
from core.bedrock_client import get_client

def handler(event, context):
    s3 = boto3.client("s3")
    obj = s3.get_object(Bucket=event["bucket"], Key=event["key"])
    case_text = json.loads(obj["Body"].read())

    client = get_client()                      # 同一個 ≤1 RPS 限流器
    fields = extract_fields(case_text, client) # ← 與單體共用的 core 函式
    fields["case_type"] = classify_case_type(case_text)["case_type"]

    out_key = event["key"].replace("input/", "fields/")
    s3.put_object(Bucket=event["bucket"], Key=out_key,
                  Body=json.dumps(fields, ensure_ascii=False))
    return {"bucket": event["bucket"], "key": out_key, "next": "analyze"}
```

## 打包注意

- `core/` 需一併打包進 Lambda layer 或部署包。
- Bedrock ≤ 1 RPS 仍由 `bedrock_client` 保證；跨 Lambda 並行時，改由 Step Functions 序列化執行各步驟（見 `stepfunctions/`）。
- 部署區域 us-west-2；Lambda 執行角色需 `bedrock:InvokeModel`、`s3:GetObject/PutObject`。
