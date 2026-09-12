# 實作任務：決定書草稿撰寫與檢查模組

順序原則：**先做不吃 Bedrock 配額、可離線測的部分**，最後才接雲。

## 已完成

- [x] 1. 交接契約與路由
  - [x] 1.1 `schemas.py`：`ROUTE_KEYS` 六枚舉、`build_empty_input()`、`validate_input()`
  - [x] 1.2 `router.py`：`resolve_route()` 未知值退回 `general`、`needs_human_review()`
  - _需求：R1.1, R1.2, R1.3_

- [x] 2. 限流與呼叫入口
  - [x] 2.1 `bedrock_client.py`：`converse()` + 快取 + 指數退避
  - [x] 2.2 行程內限流（`threading.Lock`）
  - [x] 2.3 跨行程閘門 `throttle()`：`os.mkdir` 原子鎖 + 時間戳、陳舊鎖回收
  - [x] 2.4 `henry/rag_triage.py` 改用同一個閘門（消除兩套限流器）
  - [x] 2.5 實測：兩行程各 4 次，7 個間隔全部 ≥ 1.05 秒
  - _需求：N1.1, N1.2；驗收：A6_

- [x] 3. KB 檢索（路線 A）
  - [x] 3.1 `kb.py`：`retrieve()` + `_build_filter()` metadata 過濾
  - [x] 3.2 `recommend_laws.py`：法規層帶入共通法規
  - [x] 3.3 `similar_cases.py`：歷史決定書共池不硬過濾
  - [x] 3.4 dry-run / 未設 `KB_ID` 時回 mock
  - _需求：R2.1, R2.2, R2.3, N3.1_

- [x] 4. 草稿生成
  - [x] 4.1 `CASE_PROFILES` 六案由設定
  - [x] 4.2 `build_draft_prompt()`：主張逐項回應、只能引用清單、不得新增事實
  - [x] 4.3 `generate_draft()`：一次 `converse` 呼叫
  - [x] 4.4 重寫模式（`prev_draft` + `feedback`，僅修正被指出段落）
  - [x] 4.5 `_parse_reasons()` 容錯 ```json 圍籬
  - _需求：R4.1–R4.5, R7.2_

- [x] 5. 規則驗證 V1–V10
  - [x] 5.1 V1 引用在白名單內、V2 內文條號已宣告、V3 條號格式（含之N）
  - [x] 5.2 V4 主張回應覆蓋
  - [x] 5.3 V6 日期溯源、V7 訴願期間重算
  - [x] 5.4 V8 教示完備 + 已裁撤法院偵測、V9 主文理由一致、V10 健檢紅燈已處理
  - [x] 5.5 條號正規化：`KNOWN_LAWS` 最長後綴匹配 + 異體字
  - [x] 5.6 三級分類 + `skipped` 狀態（避免假紅燈）
  - [x] 5.7 `issues` 產出 + `_FIX_HINTS`（接重寫迴圈）
  - _需求：R5.1–R5.3, N2.2；驗收：A1, A2, A3_

- [x] 6. 對抗式審查與重寫迴圈
  - [x] 6.1 `critic.adversarial_review()`：法官 prompt、輸出 passed/attacks/risk
  - [x] 6.2 `has_blocking_issue()` / `collect_feedback()`
  - [x] 6.3 `pipeline.generate_case_draft()` 迴圈，重寫上限 1 次
  - [x] 6.4 實測觸發：紅燈 → 重寫 → 紅燈清空，呼叫 4 次
  - _需求：R6.1–R6.3, R7.1, R7.3；驗收：A4_

- [x] 7. 全自動編排
  - [x] 7.1 `_decide_disposition()`：紅燈 → 撤銷；否則多數決；預設駁回
  - [x] 7.2 不受理快速通道（0 次呼叫）
  - [x] 7.3 `_quality_flags()` 彙整注意訊號
  - [x] 7.4 移除人工確認（pipeline / streamlit / lambda / step functions 同步）
  - _需求：R3.1–R3.4；驗收：A5_

- [x] 8. 介面與外殼
  - [x] 8.1 Streamlit 三階段（分析 → 撰稿 → 檢核報告）
  - [x] 8.2 Lambda handlers 五支
  - [x] 8.3 Step Functions 狀態機
  - _需求：R1–R7 的呈現_

## 待辦

- [ ] 9. 檢索結果結構化（**優先，直接影響 V1 品質**）
  - [ ] 9.1 `recommend_laws`：把 KB hits 整理成 `{law, article, status, reason, source}`
  - [ ] 9.2 `similar_cases`：同 `route_key` 加權排序，補 `disposition` / `shared_issues`
  - _需求：R2.4；影響：V1 白名單目前靠從 raw text 硬撈_

- [ ] 10. 法規版本庫（啟用 V5）
  - [ ] 10.1 建條文 + 修正日期 + 條號移列對照，存 S3
  - [ ] 10.2 實作 `version_db.is_effective(citation, on_date)`
  - [ ] 10.3 V5 由 skipped 轉為實際檢查
  - _需求：R5「引用條文於行為時有效」_

- [ ] 11. 時間軸與程序審查（啟用 V7 完整判斷）
  - [ ] 11.1 `timeline.build_timeline()`：寄存送達 10 日生效、例假日順延
  - [ ] 11.2 `procedure.procedure_check()`：訴願法§77 各款判定
  - [ ] 11.3 `quick_template()` 依款次填入對應理由模板
  - _需求：R3.1, R5「日期期間一致」_

- [ ] 12. 原處分健檢（啟用 R3.2 撤銷判定）
  - [ ] 12.1 D1–D6 純規則（應記載事項、裁處權時效、累犯、行為時法、前置程序、送達）
  - [ ] 12.2 D7–D13 合併為一次 LLM 呼叫
  - _需求：R3.2, R5「健檢紅燈已處理」_

- [ ] 13. KB 實際建置
  - [ ] 13.1 141 份 PDF 檔名與內文正規化
  - [ ] 13.2 **去識別化**（遮罩姓名、地址）— 規範要求，不可略過
  - [ ] 13.3 產 metadata（`case_type` / `doc_type` / `year`）
  - [ ] 13.4 建 KB 並確認區域支援 S3 Vectors 與所選 embedding 模型
  - _需求：R2.1, N1.4_

- [ ] 14. 教示決定表
  - [ ] 14.1 依主文與管轄填 `remedy_notice`
  - [ ] 14.2 更新 `DEPRECATED_COURTS` 並請法制同仁複核
  - _需求：R5「教示完備且法院名稱有效」_

## 已知待釐清

- `bedrock_client.embed()` 仍是 `NotImplementedError`（路線 A 由 KB 內部做 embedding，
  僅在自建索引時才需要）。若確定不自建，可移除此方法。
- `henry/` 為平行實作且非 package（扁平 import），無法被 `core/` 重用。
  若要整合需加 `__init__.py` 並改相對匯入。
