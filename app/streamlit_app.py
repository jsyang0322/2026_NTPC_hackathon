"""單體審閱介面 v1.2（§14）。呼叫 core.pipeline，不含業務邏輯。

流程（v1.2：先生成草稿，最後才確認主文）：
  貼入前段 JSON → 案件分析（路由 + KB 檢索）→ 生成草稿（用建議主文）→
  ★ 承辦人審閱草稿並確認主文 ★（維持則定稿；改主文則重生理由）。

介面：法制單位風格（深藍/金），步驟導引 + 頁面跳轉（Next/Back）。
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


# ============ 頁面設定 ============
st.set_page_config(page_title="新北市訴願審查工作台", page_icon="⚖️", layout="wide")

ROUTE_LABELS = {
    "money_laundering": "洗錢防制法", "waste": "廢棄物清理法",
    "air_pollution": "空氣污染防制法", "building": "建築法",
    "noise": "噪音管制法", "general": "通用（其他案由）",
}

# ============ 樣式（法制單位風格：深藍 #1a2a4a / 金 #b8860b）============
st.markdown(
    """
    <style>
      :root { --ink:#1a2a4a; --gold:#b8860b; --paper:#faf8f3; --line:#d8cfbe; }
      .stApp { background: var(--paper); }
      /* 頁首橫幅 */
      .gov-header {
        background: linear-gradient(135deg,#1a2a4a 0%,#26406e 100%);
        color:#fff; padding:22px 28px; border-radius:10px;
        border-bottom:4px solid var(--gold); margin-bottom:8px;
      }
      .gov-header h1 { color:#fff; font-size:1.55rem; margin:0; letter-spacing:2px; font-weight:700; }
      .gov-header .sub { color:#d9e2f2; font-size:0.9rem; margin-top:6px; letter-spacing:1px; }
      .gov-seal { font-size:2.2rem; margin-right:6px; }
      /* 步驟列 */
      .steps { display:flex; gap:10px; margin:18px 0 10px; }
      .step {
        flex:1; text-align:center; padding:12px 6px; border-radius:8px;
        background:#efe9db; color:#8a8172; font-size:0.92rem; border:1px solid var(--line);
      }
      .step.active { background:var(--ink); color:#fff; border-color:var(--ink); font-weight:700; }
      .step.done { background:#e8efe4; color:#3c6e47; border-color:#bcd4bf; }
      .step .num { display:inline-block; width:22px; height:22px; line-height:22px; border-radius:50%;
        background:rgba(255,255,255,.25); margin-right:6px; font-size:0.8rem; }
      .step.active .num { background:var(--gold); }
      /* 卡片 */
      .law-card {
        background:#fff; border:1px solid var(--line); border-left:4px solid var(--gold);
        border-radius:8px; padding:16px 20px; margin:10px 0;
      }
      .badge {
        display:inline-block; background:var(--ink); color:#fff; padding:3px 12px;
        border-radius:14px; font-size:0.82rem; letter-spacing:1px;
      }
      .badge.gold { background:var(--gold); }
      /* 決定書預覽（公文感）*/
      .doc-preview {
        background:#fff; border:1px solid var(--line); border-radius:6px; padding:28px 34px;
        font-family:"KaiTi","DFKai-SB","BiauKai",serif; line-height:2.0; color:#1a1a1a;
        box-shadow:0 2px 8px rgba(26,42,74,.06);
      }
      .doc-preview .main-text { font-size:1.15rem; font-weight:700; text-align:center;
        letter-spacing:3px; margin:10px 0 22px; }
      .doc-preview .reason-no { color:var(--gold); font-weight:700; }
      h2, h3 { color:var(--ink); }
      .stButton>button { border-radius:6px; }
    </style>
    """,
    unsafe_allow_html=True,
)


# ============ 狀態與導覽 ============
if "step" not in st.session_state:
    st.session_state["step"] = 1


def goto(step: int):
    st.session_state["step"] = step


STEP_NAMES = ["案件受理與分析", "草稿研擬", "主文核定與檢核"]


def _cn_num(n: int) -> str:
    cn = "一二三四五六七八九十"
    return cn[n - 1] if 1 <= n <= 10 else str(n)


def _render_doc(draft: dict):
    """以公文感版面預覽訴願決定書草稿。"""
    reasons_html = ""
    for i, p in enumerate(draft.get("reasons", []), start=1):
        reasons_html += f'<p><span class="reason-no">{_cn_num(i)}、</span>{p.get("text","")}</p>'
    if not reasons_html:
        reasons_html = "<p>（尚無理由段落）</p>"
    st.markdown(
        f'<div class="doc-preview">'
        f'<div style="text-align:center;letter-spacing:6px;font-size:1.05rem;">訴 願 決 定 書（草稿）</div>'
        f'<div class="main-text">主文：{draft.get("main","")}</div>'
        f'<div style="border-top:1px dashed #cfc6b4;padding-top:12px;">'
        f'<b>事實及理由</b>{reasons_html}</div>'
        f'</div>', unsafe_allow_html=True,
    )


def render_steps(current: int):
    cells = ""
    for i, name in enumerate(STEP_NAMES, start=1):
        cls = "active" if i == current else ("done" if i < current else "")
        cells += f'<div class="step {cls}"><span class="num">{i}</span>{name}</div>'
    st.markdown(f'<div class="steps">{cells}</div>', unsafe_allow_html=True)


# ============ 頁首 ============
st.markdown(
    """
    <div class="gov-header">
      <h1><span class="gov-seal">⚖️</span>新北市政府訴願審議 AI 輔助工作台</h1>
      <div class="sub">法制局 · 訴願案件審理輔助系統　|　先審查、後撰稿　每句法律依據可追溯</div>
    </div>
    """,
    unsafe_allow_html=True,
)

with st.sidebar:
    st.markdown("### ⚙️ 系統設定")
    dry_run = st.toggle("Dry-run 模式（不呼叫 Bedrock/KB）", value=True,
                        help="開啟時用假資料驗證流程；關閉才呼叫真實模型")
    st.markdown("---")
    st.markdown("#### 📋 審理進度")
    for i, name in enumerate(STEP_NAMES, start=1):
        mark = "✅" if i < st.session_state["step"] else ("🔵" if i == st.session_state["step"] else "⚪")
        st.markdown(f"{mark} 第 {i} 階段　{name}")
    st.markdown("---")
    st.caption("競賽規範：Amazon Bedrock ≤ 1 RPS，由 bedrock_client 全域限流保證。")

render_steps(st.session_state["step"])


# ============ 第 1 階段：案件受理與分析 ============
if st.session_state["step"] == 1:
    st.markdown("### 　一、案件受理與分析")
    st.caption("接收前段交接之案件 JSON，進行案由路由與法規／案例檢索（RAG）。")

    default_json = json.dumps(_demo_payload(), ensure_ascii=False, indent=2)
    raw = st.text_area("案件交接資料（JSON，schema v1.0）", value=default_json, height=240)

    if st.button("⚖️ 受理並執行分析", type="primary", use_container_width=True):
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as e:
            st.error(f"JSON 解析失敗：{e}")
            payload = None
        if payload is not None:
            problems = validate_input(payload)
            if problems:
                st.error("交接資料不符契約：" + "；".join(problems))
            else:
                with st.spinner("正在路由分類並檢索相關法規與案例…"):
                    st.session_state["analysis"] = analyze_case(payload, client=get_client(dry_run=dry_run))
                st.session_state.pop("draft_result", None)
                st.session_state.pop("final", None)
                st.rerun()

    if "analysis" in st.session_state:
        a = st.session_state["analysis"]
        c1, c2, c3 = st.columns(3)
        c1.markdown(f'<div class="law-card"><div class="badge gold">案由分類</div>'
                    f'<h3 style="margin:8px 0 0">{ROUTE_LABELS.get(a["route_key"], a["route_key"])}</h3></div>',
                    unsafe_allow_html=True)
        c2.markdown(f'<div class="law-card"><div class="badge">建議主文</div>'
                    f'<p style="margin:8px 0 0">{a.get("suggested_disposition","")}</p></div>',
                    unsafe_allow_html=True)
        hr = "需人工複核" if a.get("need_human_review") else "信心足夠"
        c3.markdown(f'<div class="law-card"><div class="badge">分類信心</div>'
                    f'<h3 style="margin:8px 0 0">{hr}</h3></div>', unsafe_allow_html=True)

        with st.expander("📚 檢索到的推薦法規", expanded=True):
            for law in a.get("recommended_laws", []) or [{"text": "（尚未接 KB，暫無資料）"}]:
                st.markdown(f"- {law.get('text','')}")
        with st.expander("📂 相似歷史案例"):
            for s in a.get("similar_cases", []) or [{"text": "（尚未接 KB，暫無資料）"}]:
                st.markdown(f"- {s.get('text','')}")

        st.markdown("---")
        _, nav = st.columns([3, 1])
        nav.button("下一步：草稿研擬 ▶", type="primary", use_container_width=True,
                   on_click=goto, args=(2,))


# ============ 第 2 階段：草稿研擬 ============
elif st.session_state["step"] == 2:
    st.markdown("### 　二、草稿研擬")
    st.caption("依系統建議主文與檢索結果，生成事實欄與理由欄初稿。")

    if "analysis" not in st.session_state:
        st.warning("請先完成第一階段分析。")
        st.button("◀ 返回第一階段", on_click=goto, args=(1,))
    else:
        a = st.session_state["analysis"]
        st.info(f"　建議主文：**{a.get('suggested_disposition','')}**　（承辦人可於下一階段調整）")

        if st.button("✍️ 生成決定書草稿", type="primary", use_container_width=True):
            with st.spinner("正在研擬事實與理由欄…（呼叫 Bedrock Claude）"):
                st.session_state["draft_result"] = generate_case_draft(a, client=get_client(dry_run=dry_run))
            st.session_state.pop("final", None)
            st.rerun()

        if "draft_result" in st.session_state:
            _render_doc(st.session_state["draft_result"]["draft"])
            st.success("草稿已生成，請至下一階段核定主文。")

        st.markdown("---")
        b1, _, b2 = st.columns([1, 2, 1])
        b1.button("◀ 上一步", use_container_width=True, on_click=goto, args=(1,))
        if "draft_result" in st.session_state:
            b2.button("下一步：主文核定 ▶", type="primary", use_container_width=True,
                      on_click=goto, args=(3,))


# ============ 第 3 階段：主文核定與檢核 ============
elif st.session_state["step"] == 3:
    st.markdown("### 　三、主文核定與檢核")
    st.caption("承辦人審閱草稿後核定主文。若變更主文，系統將重新研擬理由以維持一致。")

    if "draft_result" not in st.session_state:
        st.warning("請先完成第二階段草稿研擬。")
        st.button("◀ 返回第二階段", on_click=goto, args=(2,))
    else:
        used = st.session_state["draft_result"].get("used_disposition", "")
        st.markdown(f'<div class="law-card"><div class="badge gold">草稿現行主文</div>'
                    f'<p style="margin:8px 0 0">{used}</p></div>', unsafe_allow_html=True)

        options = ["訴願駁回", "原處分撤銷，另為適法之處分", "訴願不受理"]
        confirmed = st.selectbox("　核定主文（若與草稿不同，將重新研擬理由）", options)

        if st.button("🖋️ 核定並定稿", type="primary", use_container_width=True):
            with st.spinner("正在核定並執行品質檢核…"):
                st.session_state["final"] = confirm_disposition(
                    st.session_state["analysis"], st.session_state["draft_result"],
                    confirmed, client=get_client(dry_run=dry_run),
                )
            if st.session_state["final"].get("regenerated"):
                st.warning("主文已變更，理由已重新研擬以維持主文與理由一致。")
            else:
                st.success("主文與草稿一致，直接定稿。")

        if "final" in st.session_state:
            f = st.session_state["final"]
            _render_doc(f["draft"])
            with st.expander("🔍 品質檢核報告", expanded=True):
                v = f.get("verification", {})
                st.markdown(f"- 自動驗證：{'✅ 全數通過' if v.get('all_passed') else '⚠️ 有項目待處理'}")
                adv = f.get("adversarial", {})
                st.markdown(f"- 對抗式審查（撤銷風險）：{adv.get('revocation_risk','—')}")

        st.markdown("---")
        b1, _, b2 = st.columns([1, 2, 1])
        b1.button("◀ 上一步", use_container_width=True, on_click=goto, args=(2,))
        b2.button("🔄 審理新案件", use_container_width=True, on_click=goto, args=(1,))
