# 設計：決定書草稿撰寫與檢查模組

對應需求：`requirements.md`。架構原則見 `.kiro/steering/architecture-decisions.md`。

## 模組結構

```
core/
├── bedrock_client.py  ★ 所有 Bedrock 呼叫唯一入口（限流/快取/重試）
├── schemas.py         ★ 交接契約（route_key 枚舉、驗證）
├── router.py            R1 讀 route_key 分派
├── kb.py              ★ R2 Bedrock KB 檢索（metadata 過濾）
├── recommend_laws.py    R2 法規推薦（案由 OR 共通法規）
├── similar_cases.py     R2 相似案例（共池 + 加權）
├── draft.py             R4 草稿生成（CASE_PROFILES + 重寫模式）
├── verify.py            R5 規則驗證（V1–V10，0 次呼叫）
├── critic.py            R6 對抗式審查（法官）
└── pipeline.py        ★ R3/R7 編排（判主文 + 審查迴圈）
```

## 資料流

```
前段 JSON
   │  validate_input()
   ▼
router.resolve_route() ──────────────► route_key
   │
   ├─ is_admissible = false ─► quick_template() ─► verify ─► 輸出（0 次呼叫）
   │
   ▼
_query_text()  組原處分書 + 訴願書 + 主張
   │
   ├─► recommend_laws()  KB retrieve：law/interpretation/judgment + common_law
   └─► find_similar()    KB retrieve：decision，不硬過濾案由
   │                                                    ↑ 1 次呼叫
   ▼
_decide_disposition(sims, defects) ──► (主文, 判定依據)
   │
   ▼
┌─ generate_draft() ──────────────────────────────── 1 次呼叫
│      │
│      ▼
│  verify_draft() ──► blocking（給人）+ issues（給機器）  0 次呼叫
│      │
│      ▼
│  adversarial_review() ──► passed / attacks / risk      1 次呼叫
│      │
│      ├─ 無問題 或 已重寫 1 次 ─► 跳出
│      │
│      └─ has_blocking_issue ─► collect_feedback()
│                                    │
└────────────────────────────────────┘ 重寫（+2 次：重寫 + 再審）
   │
   ▼
輸出：draft / verification / adversarial / quality_flags / needs_human_review
```

## 關鍵設計

### 限流：兩層閘門

`bedrock_client` 提供模組級 `throttle()`：

- **行程內**：`threading.Lock`
- **跨行程**：`os.mkdir` 原子鎖 + 時間戳檔（跨平台，Windows 無 `fcntl`）
  - 陳舊鎖 30 秒後回收（持有者崩潰時不卡死）
  - 取不到鎖逾時 60 秒 → 保守睡一個間隔，不直接放行

無法走 `converse()` 的呼叫（KB `retrieve`、henry 的 `invoke_model`）都須先呼叫 `throttle()`。

### CASE_PROFILES：一顆模型模擬多個專責 agent

```python
CASE_PROFILES[route_key] = {
    "label": ...,           # 顯示用
    "common_issues": [...], # 該案由常見爭點，塞進 prompt
    "writing_style": ...,   # 新北市決定書用語
}
```

未知 `route_key` 退回 `general`。

### verify：檢核項設計

每項回傳結構：

```python
{"id": "V1", "name": ..., "status": "pass|fail|skipped",
 "level": "red|amber",        # 失敗時的嚴重度
 "display_level": ...,        # pass→green, skipped→gray, fail→level
 "detail": ..., "evidence": [...]}
```

三個狀態的意義有別，不要混用：

- `pass` — 檢查過且通過
- `fail` — 檢查過且不通過
- `skipped` — **資料不足無法檢查**（KB 未結構化、`version_db` 未建、教示未接）

`skipped` 不算失敗，這是避免骨架階段假紅燈的關鍵。

### 條號正規化：白名單比對的基礎

問題：草稿可能寫 `洗錢防制法§22`，清單可能寫 `{"law":"洗錢防制法","article":"22"}`，
內文可能寫 `訴願人違反洗錢防制法第22條`。三種形狀要能比對成同一條。

作法：全部經 `_normalize_citation()` 收斂為 `法名第N條[之M]`：

1. regex 同時吃 `§22` / `第22條` / `第15條之2` / `第 22 條`
2. 法名用 **`KNOWN_LAWS` 註冊表最長後綴匹配**，切掉黏在前面的主詞動詞
   （「訴願人違反洗錢防制法」→「洗錢防制法」）
3. 未登錄法規退回啟發式（切在最後一個主詞/動詞之後）
4. 異體字正規化：汙→污、台→臺
5. 指稱性法名（本法/該法）回 `None`——`cites` 應寫全名，否則失去追溯性

### issues：規則層與重寫迴圈的接線

這是**最容易靜默失效**的地方。`critic.has_blocking_issue(review, issues)` 若拿到
空清單，重寫就只靠法官觸發，規則層等於失效**且不會報錯**。

因此 `verify_draft()` 必須回 `issues`，由 `_build_issues(fails)` 從紅燈項生成，
並附 `_FIX_HINTS` 的具體修正方向，讓重寫模型知道怎麼改。

## 錯誤處理

| 情境 | 處理 |
|---|---|
| 交接 JSON 不符契約 | `analyze_case` 回 `{"error", "problems"}`，不繼續 |
| 模型回傳非 JSON | `_parse_reasons` 容錯（去 ```json 圍籬），失敗則整段當一個 paragraph |
| KB 未設 `KB_ID` 或 dry-run | `kb.retrieve` 回 mock，流程可跑 |
| 可引用清單為空 | V1 回 skipped（不誤判所有引用為捏造） |
| `version_db` 未建 | V5 回 skipped |
| ThrottlingException | 指數退避重試（上限 5 次） |

## 測試策略

純規則部分可離線測，重點三情境：

1. **乾淨草稿** → 無紅燈
2. **問題草稿**（捏造引用 + 漏回應 + 主文矛盾 + 條號寫壞 + 逾期 + 已裁撤法院）→ 對應項報紅燈
3. **骨架空資料** → 全 skipped，零誤報

重寫迴圈用假 client 驗證：第一次回不合格草稿、第二次回合格，檢查 `rewrite_count == 1`
且呼叫數為 4。

跨行程限流用兩個獨立行程各呼叫數次，量測所有間隔 ≥ 1 秒。
