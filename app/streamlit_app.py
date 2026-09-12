"""單體審閱介面 v1.1（§14）。呼叫 core.pipeline，不含業務邏輯。

接收前段（同學）的 JSON 交接契約（schemas.build_empty_input）；
流程：貼入/載入案卷 JSON → analyze_case（路由 + KB 檢索）→
★ 承辦人確認主文 ★ → finalize_draft（撰稿 + 檢核）→ 匯出。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import streamlit as st  # noqa: E402

from core.pipeline import analyze_case, finalize_draft, _demo_payload  # noqa: E402
from core.schemas import validate_input  # noqa: E402
from core.bedrock_client import get_client  # noqa: E402


st.set_page_config(page_title="新北市訴願審查工作台", layout="wide")
st.title("新北市訴願案件審理 AI 輔助系統")
st.caption("v1.1：接前段 JSON → 路由 → KB 檢索 → 主文確認 → 草稿（骨架版）")

with st.sidebar:
    st.header("設定")
    dry_run = st.toggle("Dry-run（不呼叫 Bedrock/KB）", value=True)
    st.divider()
    st.caption("競賽規範：Bedrock ≤ 1 RPS，由 bedrock_client 全域限流")

tab_in, tab_confirm, tab_draft = st.tabs(["1. 案件分析", "2. 主文確認", "3. 草稿與檢核"])

with tab_in:
    st.subheader("貼入前段交接 JSON（schema v1.0）")
    default_json = json.dumps(_demo_payload(), ensure_ascii=False, indent=2)
    raw = st.text_area("交接 JSON", value=default_json, height=280)
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
                st.success(f"分析完成，route_key = {st.session_state['analysis']['route_key']}")
    if "analysis" in st.session_state:
        st.json(st.session_state["analysis"])

with tab_confirm:
    st.subheader("★ 承辦人確認主文（系統不代為決定）")
    if "analysis" in st.session_state:
        st.info(f"系統建議：{st.session_state['analysis'].get('suggested_disposition','')}")
        confirmed = st.selectbox("確認主文", ["訴願駁回", "原處分撤銷，另為適法之處分", "訴願不受理"])
        if st.button("確認並生成草稿"):
            st.session_state["confirmed"] = confirmed
            st.success(f"已確認主文：{confirmed}")
    else:
        st.warning("請先於「案件分析」執行分析。")

with tab_draft:
    st.subheader("草稿與檢核報告")
    if "analysis" in st.session_state and "confirmed" in st.session_state:
        client = get_client(dry_run=dry_run)
        final = finalize_draft(st.session_state["analysis"], st.session_state["confirmed"], client=client)
        st.json(final)
    else:
        st.warning("請先完成分析並確認主文。")
