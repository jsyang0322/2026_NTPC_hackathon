"""單體審閱介面 v1.3（§14）。呼叫 core.pipeline，不含業務邏輯。

流程（v1.3：全自動端到端，無人工確認步驟）：
  貼入前段 JSON → 一鍵執行（路由 → KB 檢索 → 自動判定主文 → 撰稿 → 驗證 → 對抗式審查）
  → 呈現草稿 + 檢核報告（承辦人事後審閱修改，非流程閘門）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import streamlit as st  # noqa: E402

from core.pipeline import process_case, _demo_payload  # noqa: E402
from core.schemas import validate_input  # noqa: E402
from core.bedrock_client import get_client  # noqa: E402


#: 檢核狀態 → 顯示文字
_STATUS_LABEL = {
    ("pass", "green"): "通過",
    ("skipped", "gray"): "略過",
    ("fail", "red"): "紅燈",
    ("fail", "amber"): "黃燈",
}


def _status_text(check: dict) -> str:
    return _STATUS_LABEL.get((check["status"], check["display_level"]), check["status"])


st.set_page_config(page_title="新北市訴願審查工作台", layout="wide")
st.title("新北市訴願案件審理 AI 輔助系統")
st.caption("v1.3：接前段 JSON → 路由 → KB 檢索 → 自動判定主文 → 撰稿 → 規則驗證 + 對抗式審查")

with st.sidebar:
    st.header("設定")
    dry_run = st.toggle("Dry-run（不呼叫 Bedrock/KB）", value=True)
    st.divider()
    st.caption("競賽規範：Bedrock ≤ 1 RPS，由 bedrock_client 全域限流")
    st.caption("單件呼叫預算：檢索 1 + 撰稿 1 + 審查 1–2 ≈ 3–4 次；不受理案 0 次")

tab_run, tab_result = st.tabs(["1. 輸入與執行", "2. 草稿與檢核報告"])

# --- 1. 輸入與執行（一鍵端到端）---
with tab_run:
    st.subheader("貼入前段交接 JSON（schema v1.0）")
    default_json = json.dumps(_demo_payload(), ensure_ascii=False, indent=2)
    raw = st.text_area("交接 JSON", value=default_json, height=300)

    if st.button("一鍵產生決定書草稿", type="primary"):
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as e:
            st.error(f"JSON 解析失敗：{e}")
            payload = None

        if payload is not None:
            problems = validate_input(payload)
            if problems:
                st.error("介面不符契約：" + "；".join(problems))
            else:
                client = get_client(dry_run=dry_run)
                with st.spinner("執行中：KB 檢索 → 判定主文 → 撰稿 → 驗證 → 對抗式審查"):
                    st.session_state["result"] = process_case(payload, client=client)
                st.success("已產出草稿，請至「草稿與檢核報告」查看")

    if "result" in st.session_state:
        r = st.session_state["result"]
        col1, col2, col3 = st.columns(3)
        col1.metric("route_key", r.get("route_key", "-"))
        col2.metric("主文", r.get("disposition", "-"))
        col3.metric("驗證紅燈", len(r.get("verification", {}).get("blocking", [])))

# --- 2. 草稿與檢核報告 ---
with tab_result:
    if "result" not in st.session_state:
        st.warning("請先於「輸入與執行」執行一次。")
    else:
        r = st.session_state["result"]
        if r.get("error"):
            st.error(f"{r['error']}：{r.get('problems')}")
            st.stop()

        d = r.get("draft", {})
        report = r.get("verification", {})
        review = r.get("adversarial", {})

        st.subheader("主文與判定依據")
        st.info(f"**{d.get('main', '')}**")
        st.caption(f"判定依據：{r.get('decision_basis', '')}（decided_by={r.get('decided_by', '')}）")

        flags = r.get("quality_flags") or []
        if flags:
            st.warning("需注意訊號：\n" + "\n".join(f"- {f}" for f in flags))
        else:
            st.success("無異常訊號")

        st.divider()
        st.subheader("理由欄草稿")
        reasons = d.get("reasons") or []
        if not reasons:
            st.caption("（無理由段落：不受理快速通道或骨架資料）")
        for i, p in enumerate(reasons, 1):
            with st.container(border=True):
                st.markdown(f"**段落 {i}**　`source={p.get('source', '')}`")
                st.write(p.get("text", ""))
                meta = []
                if p.get("responds_to"):
                    meta.append(f"回應主張：{', '.join(p['responds_to'])}")
                if p.get("cites"):
                    meta.append(f"引用：{', '.join(p['cites'])}")
                if p.get("based_on_case"):
                    meta.append(f"參考案例：{p['based_on_case']}")
                if meta:
                    st.caption("　|　".join(meta))

        st.divider()
        st.subheader("檢核報告（純規則，0 次 Bedrock 呼叫）")
        summary = report.get("summary", {})
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("通過", summary.get("pass", 0))
        c2.metric("紅燈", summary.get("red", 0))
        c3.metric("黃燈", summary.get("amber", 0))
        c4.metric("略過", summary.get("skipped", 0))

        rows = [{
            "項次": c["id"],
            "檢核項": c["name"],
            "狀態": _status_text(c),
            "說明": c["detail"],
            "證據": "; ".join(str(e) for e in c.get("evidence", [])) or "-",
        } for c in report.get("checks", [])]
        if rows:
            st.dataframe(rows, use_container_width=True, hide_index=True)

        st.divider()
        st.subheader("對抗式審查（扮行政法院法官挑毛病）")
        st.caption(f"撤銷風險：{review.get('revocation_risk', 'unknown')}")
        attacks = review.get("attacks") or []
        if not attacks:
            st.caption(review.get("note", "（尚無挑戰點：骨架階段或不受理案件）"))
        for a in attacks:
            st.markdown(f"- **[{a.get('severity', '')}]** {a.get('point', '')}")

        with st.expander("完整輸出 JSON"):
            st.json(r)
