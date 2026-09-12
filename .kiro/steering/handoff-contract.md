---
inclusion: fileMatch
fileMatchPattern: 'core/{schemas,router,pipeline}.py'
---

# 前後段交接契約（改動需雙方確認）

前段（擷取、受理判斷、案由分類）與後段（路由、撰稿、檢查）以一份 JSON 交接。
契約定義在 `core/schemas.py`：`INPUT_SCHEMA_VERSION`、`build_empty_input()`、`validate_input()`。

## route_key 枚舉（六個，不是四個）

```python
ROUTE_KEYS = ("money_laundering", "waste", "air_pollution",
              "building", "noise", "general")
```

規則：

- **用英文枚舉當路由鍵**，中文 `label` 只作顯示。避免「汙/污」「防制/管制」
  名稱不一致造成路由失敗。
- 六個以外一律填 `general`，走通用設定。
- 未知值由 `router.resolve_route()` 安全退回 `general`，不擋流程。

`draft.CASE_PROFILES` 與 `classify.CASE_TYPES` 必須與這份枚舉一致，改一處要改三處。

## 欄位規則

- **缺值用空字串 / 空陣列，不要省略 key**。下游用 `.get()` 但仍以 key 齊全為契約。
- `extracted_fields` 和 `raw_text` **都要給**：前者供 prompt，後者供 KB 檢索與對抗式審查上下文。
- `confidence` 低於門檻時 `need_human_review = true`，但後段仍要能收、能路由
  （只反映在 `quality_flags`，不阻斷）。
- `admissibility.is_admissible = false` 會觸發不受理快速通道（不檢索、不撰稿、0 次呼叫）。
- 加 `schema_version`，改介面時兩邊不會默默對接失敗。

## v1.3 之後的輸出欄位（後段）

移除人工確認後，這些 key 有變，讀取端要跟上：

| 舊 | 新 |
|---|---|
| `suggested_disposition` | `decided_disposition`（+ `decision_basis`） |
| `used_disposition` | `disposition`（+ `decided_by="auto"`） |
| `confirmed_disposition` / `regenerated` | 已移除 |

新增：`rewrite_count`、`needs_human_review`、`quality_flags`、
`verification.issues`（重寫迴圈用）。

## 改契約的流程

1. 先改 `core/schemas.py` 並升 `INPUT_SCHEMA_VERSION`
2. 更新 `validate_input()` 的檢查
3. 通知前段負責人（不要只推 code）
4. 兩邊都跑一次 `python -m core.pipeline --dry-run` 確認
