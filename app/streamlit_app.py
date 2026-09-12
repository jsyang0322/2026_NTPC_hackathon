"""單體審閱介面 v1.2（§14）。呼叫 core.pipeline，不含業務邏輯。

流程（v1.2：先生成草稿，最後才確認主文）：
  貼入前段 JSON → 案件分析（路由 + KB 檢索）→ 生成草稿（用建議主文）→
  ★ 承辦人審閱草稿並確認主文 ★（維持則定稿；改主文則重生理由）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import streamlit as st  # noqa: E402

from core.pipeline import analyze_case, generate_case_draft, confirm_disposition, _demo_payload  # noqa: E402
from core.schemas import validate_input  # noqa: E402
from core.bedrock_client import get_client  # noqa: E402


st.set_page_config(page_title="新北市訴願審查工作台", layout="wide")
st.title("新北市訴願案件審理 AI 輔助系統")
st.caption("v1.2：接前段 JSON → 路由 → KB 檢索 → 生成草稿 → 主文確認與檢核")

with st.sidebar:
    st.header("設定")
    dry_run = st.toggle("Dry-run（不呼叫 Bedrock/KB）", value=True)
    st.divider()
    st.caption("競賽規範：Bedrock ≤ 1 RPS，由 bedrock_client 全域限流")

tab_analyze, tab_draft, tab_confirm = st.tabs(
    ["1. 案件分析", "2. 草稿生成", "3. 主文確認與檢核"]
)

# --- 1. 案件分析（路由 + KB 檢索）---
with tab_analyze:
    st.subheader("貼入前段交接 JSON（schema v1.0）")
    default_json = json.dumps(_demo_payload(), ensure_ascii=False, indent=2)
    raw = st.text_area("交接 JSON", value=default_json, height=260)
    if st.button("執行分析", type="primary"):
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
                st.session_state["analysis"] = analyze_case(payload, client=client)
                st.session_state.pop("draft_result", None)
                st.session_state.pop("final", None)
                st.success(f"分析完成，route_key = {st.session_state['analysis']['route_key']}")
    if "analysis" in st.session_state:
        st.json(st.session_state["analysis"])

# --- 2. 草稿生成（用建議主文先生成）---
with tab_draft:
    st.subheader("生成草稿（採系統建議主文）")
    if "analysis" in st.session_state:
        st.info(f"建議主文：{st.session_state['analysis'].get('suggested_disposition','')}")
        if st.button("生成草稿", type="primary"):
            client = get_client(dry_run=dry_run)
            st.session_state["draft_result"] = generate_case_draft(
                st.session_state["analysis"], client=client
            )
            st.session_state.pop("final", None)
            st.success("草稿已生成，請至「主文確認與檢核」審閱")
        if "draft_result" in st.session_state:
            st.json(st.session_state["draft_result"])
    else:
        st.warning("請先於「案件分析」執行分析。")

# --- 3. 主文確認與檢核（最後才確認；改主文則重生）---
with tab_confirm:
    st.subheader("★ 承辦人審閱草稿後確認主文（系統不代為決定）")
    if "analysis" in st.session_state and "draft_result" in st.session_state:
        used = st.session_state["draft_result"].get("used_disposition", "")
        st.caption(f"草稿目前採用的主文：{used}")
        options = ["訴願駁回", "原處分撤銷，另為適法之處分", "訴願不受理"]
        confirmed = st.selectbox("確認主文（若與草稿不同將重生理由）", options)
        if st.button("確認定稿"):
            client = get_client(dry_run=dry_run)
            st.session_state["final"] = confirm_disposition(
                st.session_state["analysis"], st.session_state["draft_result"], confirmed, client=client
            )
            if st.session_state["final"].get("regenerated"):
                st.warning("主文已變更，理由已重新生成以保持一致。")
            else:
                st.success("主文與草稿一致，直接定稿（未重生）。")
        if "final" in st.session_state:
            st.json(st.session_state["final"])
    else:
        st.warning("請先完成分析並生成草稿。")
