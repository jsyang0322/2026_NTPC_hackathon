"""單體審閱介面（§14）。呼叫 core.pipeline，不含業務邏輯。

流程（v2.0：全自動二階段）：
  上傳訴願書/原處分書 → 案件分析（擷取 + 路由 + KB 檢索 + 自動判定主文）→
  決定書草稿（撰稿 → 規則檢查 → 法官審查 → 必要時自動重寫，全在系統內部）。

品質檢核在系統端執行，僅於有確定問題時提示人工複核。草稿可下載為 PDF。
介面：法制單位風格（深藍/金），步驟導引 + 頁面跳轉。
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import streamlit as st  # noqa: E402

from core.pipeline import analyze_case, generate_case_draft, intake  # noqa: E402
from core.bedrock_client import get_client  # noqa: E402
from core.law_lookup import lookup_from_text  # noqa: E402
from pipeline.kb_ingest_s3 import deidentify  # noqa: E402


def _read_upload(uploaded) -> str:
    """把上傳的檔案（PDF / txt）讀成純文字。"""
    name = (uploaded.name or "").lower()
    data = uploaded.read()
    if name.endswith(".pdf"):
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        return "\n".join((p.extract_text() or "") for p in reader.pages)
    # 純文字檔：容錯 UTF-8 / Big5
    for enc in ("utf-8", "utf-8-sig", "big5", "cp950"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="ignore")


# ============ 頁面設定 ============
st.set_page_config(page_title="新北市訴願審查工作台", page_icon="⚖️", layout="wide",
                   initial_sidebar_state="expanded")

ROUTE_LABELS = {
    "money_laundering": "洗錢防制法", "waste": "廢棄物清理法",
    "air_pollution": "空氣污染防制法", "building": "建築法",
    "noise": "噪音管制法", "general": "通用（其他案由）",
}


# ============ 樣式（法制單位風格：深藍 #1a2a4a / 金 #b8860b）============
st.markdown(
    """
    <style>
      :root {
        --ink:#1a2a4a; --ink2:#26406e; --gold:#b8860b;
        --paper:#faf8f3; --panel:#ffffff; --line:#e0d8c6;
        --muted:#5a6273;
      }

      /* ---- 全域底色與字色（強制淺色，避免深色主題撞色）---- */
      .stApp { background:var(--paper); }
      .stApp, .stApp p, .stApp li, .stApp span, .stApp label,
      .stMarkdown, [data-testid="stMarkdownContainer"] { color:var(--ink); }
      /* 主內容區標題深藍；用 :not(.gov-header ...) 避免蓋掉深底橫幅的白字 */
      .block-container h1, .block-container h2, .block-container h3,
      .block-container h4, .block-container h5, .block-container h6 {
        color:var(--ink); font-weight:700;
      }

      /* 主內容區留白 */
      .block-container { padding-top:2.5rem; max-width:1080px; }

      /* 隱藏 Streamlit 頂端工具列（Deploy 按鈕、漢堡選單）與 footer —— 使用者用不到 */
      header[data-testid="stHeader"] { background:transparent; }
      [data-testid="stToolbar"] { display:none !important; }
      #MainMenu { display:none !important; }
      footer { display:none !important; }
      [data-testid="stDecoration"] { display:none !important; }

      /* ---- 側邊欄 ---- */
      /* 強制顯示並展開，覆蓋窄視窗時的自動收合（Streamlit 1.63 在 layout=wide
         視窗較窄時會自動收合側邊欄，導致看似「消失」）。 */
      [data-testid="stSidebar"] {
        background:#f2ece0; border-right:1px solid var(--line);
        display:flex !important; visibility:visible !important;
        transform:none !important;
        min-width:260px !important; width:260px !important;
        margin-left:0 !important;
      }
      [data-testid="stSidebar"][aria-expanded="false"] {
        transform:none !important; margin-left:0 !important;
      }
      [data-testid="stSidebar"] * { color:var(--ink) !important; }
      /* 側邊欄固定展開，隱藏那顆點了無效的收合箭頭（<<），避免誤導。 */
      [data-testid="stSidebarCollapseButton"],
      [data-testid="stSidebarCollapsedControl"],
      [data-testid="stSidebar"] [data-testid="stSidebarHeader"] button,
      button[kind="headerNoPadding"] {
        display:none !important;
      }

      /* ---- 說明文字（caption）---- */
      .stCaption, [data-testid="stCaptionContainer"],
      [data-testid="stCaptionContainer"] * { color:var(--muted) !important; }

      /* ---- 頁首橫幅 ---- */
      .gov-header {
        background:linear-gradient(135deg,var(--ink) 0%,var(--ink2) 100%);
        padding:22px 28px; border-radius:12px;
        border-bottom:4px solid var(--gold); margin-bottom:10px;
      }
      .gov-header h1, .block-container .gov-header h1 {
        color:#ffffff !important; font-size:1.5rem; margin:0; letter-spacing:2px;
      }
      .gov-header .sub { color:#e6ecf7 !important; font-size:0.88rem; margin-top:6px; letter-spacing:1px; }
      .gov-seal { font-size:2rem; margin-right:8px; }

      /* ---- 步驟列 ---- */
      .steps { display:flex; gap:10px; margin:16px 0 14px; }
      .step {
        flex:1; text-align:center; padding:12px 6px; border-radius:8px;
        background:#efe9db; color:#8a8172; font-size:0.92rem; border:1px solid var(--line);
      }
      .step.active { background:var(--ink); color:#fff; border-color:var(--ink); font-weight:700; }
      .step.done { background:#e8efe4; color:#3c6e47; border-color:#bcd4bf; }
      .step .num { display:inline-block; width:22px; height:22px; line-height:22px; border-radius:50%;
        background:rgba(255,255,255,.25); margin-right:6px; font-size:0.8rem; }
      .step.active .num { background:var(--gold); color:var(--ink); }

      /* ---- 卡片 ---- */
      .law-card {
        background:var(--panel); border:1px solid var(--line); border-left:4px solid var(--gold);
        border-radius:8px; padding:16px 20px; margin:10px 0;
      }
      .law-card, .law-card p, .law-card h3, .law-card span { color:var(--ink); }
      .badge {
        display:inline-block; background:var(--ink); color:#fff !important; padding:3px 12px;
        border-radius:14px; font-size:0.82rem; letter-spacing:1px;
      }
      .badge.gold { background:var(--gold); color:#fff !important; }

      /* ---- 檔案上傳元件（改為淺底，修正深底看不到字）---- */
      [data-testid="stFileUploader"] label,
      [data-testid="stFileUploaderDropzone"] * { color:var(--ink) !important; }
      [data-testid="stFileUploaderDropzone"] {
        background:#fbf9f4 !important; border:1.5px dashed var(--gold) !important;
        border-radius:10px;
      }
      /* 已上傳檔案列 */
      [data-testid="stFileUploaderFile"] { color:var(--ink) !important; }
      [data-testid="stFileUploaderFile"] * { color:var(--ink) !important; }

      /* 上傳的「Browse files」按鈕：金色底白字，明顯可見 */
      [data-testid="stFileUploaderDropzone"] button {
        background:var(--gold) !important; color:#fff !important; border:none !important;
        border-radius:6px !important; font-weight:600 !important; opacity:1 !important;
      }
      [data-testid="stFileUploaderDropzone"] button * { color:#fff !important; }
      /* dropzone 說明文字（200MB per file...）用可讀的中灰 */
      [data-testid="stFileUploaderDropzoneInstructions"],
      [data-testid="stFileUploaderDropzoneInstructions"] * { color:var(--muted) !important; }

      /* ---- 主要按鈕 ---- */
      .stButton>button, .stDownloadButton>button {
        border-radius:8px; font-weight:600;
      }
      .stButton>button[kind="primary"], .stDownloadButton>button[kind="primary"] {
        background:var(--gold); color:#fff; border:none;
      }
      .stButton>button[kind="primary"]:hover, .stDownloadButton>button[kind="primary"]:hover {
        background:#9c7209; color:#fff;
      }
      .stButton>button[kind="secondary"] {
        background:#fff; color:var(--ink); border:1px solid var(--line);
      }

      /* ---- expander ---- */
      [data-testid="stExpander"] {
        background:var(--panel); border:1px solid var(--line); border-radius:8px;
      }
      [data-testid="stExpander"] summary, [data-testid="stExpander"] summary * { color:var(--ink) !important; }

      /* ---- 提示框（info/warning/error）文字對比 ---- */
      [data-testid="stAlert"] * { color:var(--ink) !important; }

      /* ---- 決定書預覽（公文感）---- */
      .doc-preview {
        background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:28px 34px;
        font-family:"KaiTi","DFKai-SB","BiauKai","STKaiti",serif; line-height:2.0; color:#1a1a1a;
        box-shadow:0 2px 10px rgba(26,42,74,.07); margin-top:8px;
      }
      .doc-preview, .doc-preview p, .doc-preview b { color:#1a1a1a; }
      .doc-preview .main-text { font-size:1.15rem; font-weight:700; text-align:center;
        letter-spacing:3px; margin:10px 0 22px; color:var(--ink); }
      .doc-preview .reason-no { color:var(--gold); font-weight:700; }

      /* ---- 系統判斷結果橫幅（草稿上方）---- */
      .verdict-banner {
        display:flex; align-items:center; gap:16px;
        background:linear-gradient(135deg,var(--ink) 0%,var(--ink2) 100%);
        border-left:6px solid var(--gold); border-radius:10px;
        padding:16px 24px; margin:6px 0 14px;
      }
      .verdict-label {
        color:#e6ecf7 !important; font-size:0.85rem; letter-spacing:2px;
        border-right:1px solid rgba(255,255,255,.3); padding-right:16px;
      }
      .verdict-value { color:#ffffff !important; font-size:1.3rem; font-weight:700; letter-spacing:2px; }
    </style>
    """,
    unsafe_allow_html=True,
)


# ============ 狀態與導覽 ============
if "step" not in st.session_state:
    st.session_state["step"] = 1

# 「法律查詢輔助」是獨立於審理進度主線的功能：以獨立 flag 控制主區域顯示，
# 開啟時主區域切成查詢畫面，不佔用 step 狀態、不影響 分析→草稿 主線。
if "law_lookup_mode" not in st.session_state:
    st.session_state["law_lookup_mode"] = False


def goto(step: int):
    st.session_state["step"] = step
    st.session_state["law_lookup_mode"] = False  # 回主線時關閉查詢模式


def open_law_lookup():
    st.session_state["law_lookup_mode"] = True


def close_law_lookup():
    st.session_state["law_lookup_mode"] = False


def reset_review():
    """清除審理流程的所有狀態，回到第一階段，準備審理下一件。"""
    for k in ("analysis", "draft_result"):
        st.session_state.pop(k, None)
    st.session_state["step"] = 1
    st.session_state["law_lookup_mode"] = False


def reset_law_lookup():
    """清除法律小幫手的查詢結果，準備下一次查詢。"""
    st.session_state.pop("law_lookup_result", None)


STEP_NAMES = ["案件受理與分析", "決定書草稿"]


def _cn_num(n: int) -> str:
    cn = "一二三四五六七八九十"
    return cn[n - 1] if 1 <= n <= 10 else str(n)


def _draft_to_pdf(draft: dict) -> bytes:
    """把決定書草稿組成 PDF（含中文），回傳 bytes 供下載。"""
    import pymupdf

    lines = ["訴 願 決 定 書（草稿）", "", f"主文：{draft.get('main', '')}", "", "事實及理由"]
    for i, p in enumerate(draft.get("reasons", []), start=1):
        lines.append(f"{_cn_num(i)}、{p.get('text', '')}")
    remedy = draft.get("remedy_notice")
    if remedy:
        lines += ["", "教示", remedy]

    doc = pymupdf.open()
    page_w, page_h, margin, size, line_h, max_chars = 595, 842, 60, 13, 22, 34
    page = doc.new_page(width=page_w, height=page_h)
    y = margin
    for raw in lines:
        chunks = [raw[i:i + max_chars] for i in range(0, len(raw), max_chars)] or [""]
        for chunk in chunks:
            if y > page_h - margin:
                page = doc.new_page(width=page_w, height=page_h)
                y = margin
            page.insert_text((margin, y), chunk, fontname="china-t", fontsize=size)
            y += line_h
    doc.subset_fonts()
    out = doc.tobytes(garbage=4, deflate=True)
    doc.close()
    return out


def _render_doc(draft: dict):
    """以公文感版面預覽訴願決定書草稿。"""
    reasons_html = ""
    for i, p in enumerate(draft.get("reasons", []), start=1):
        reasons_html += f'<p><span class="reason-no">{_cn_num(i)}、</span>{p.get("text","")}</p>'
    if not reasons_html:
        reasons_html = "<p>（尚無理由段落）</p>"
    remedy = draft.get("remedy_notice")
    remedy_html = ""
    if remedy:
        remedy_html = (
            f'<div style="border-top:1px dashed #cfc6b4;margin-top:12px;padding-top:12px;">'
            f'<b>教示</b><p>{remedy}</p></div>'
        )
    st.markdown(
        f'<div class="doc-preview">'
        f'<div style="text-align:center;letter-spacing:6px;font-size:1.05rem;">訴 願 決 定 書（草稿）</div>'
        f'<div class="main-text">主文：{draft.get("main","")}</div>'
        f'<div style="border-top:1px dashed #cfc6b4;padding-top:12px;">'
        f'<b>事實及理由</b>{reasons_html}</div>'
        f'{remedy_html}'
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
    <div class="gov-header" style="color:#ffffff">
      <div style="color:#ffffff;font-size:1.5rem;font-weight:700;letter-spacing:2px">
        <span style="font-size:2rem;margin-right:8px">⚖️</span>新北市政府訴願審議 AI 輔助工作台
      </div>
      <div style="color:#e6ecf7;font-size:0.88rem;margin-top:6px;letter-spacing:1px">
        法制局 · 訴願案件審理輔助系統　|　先審查、後撰稿　每句法律依據可追溯
      </div>
    </div>
    """,
    unsafe_allow_html=True,
)

with st.sidebar:
    st.markdown("#### 📋 審理進度")
    _in_main_line = not st.session_state["law_lookup_mode"]
    for i, name in enumerate(STEP_NAMES, start=1):
        if _in_main_line:
            mark = "✅" if i < st.session_state["step"] else ("🔵" if i == st.session_state["step"] else "⚪")
        else:
            mark = "⚪"          # 查詢模式時主線不高亮
        st.markdown(f"{mark} 第 {i} 階段　{name}")

    st.button("🔄 審理新案件", use_container_width=True, on_click=reset_review,
              key="sb_reset_review")

    st.markdown("---")
    st.markdown("#### 💡 法律小幫手")
    if st.session_state["law_lookup_mode"]:
        st.button("◀ 返回審理流程", use_container_width=True, on_click=close_law_lookup)
    else:
        st.button("💡 開啟法律小幫手", use_container_width=True, on_click=open_law_lookup)

# 主線步驟列僅在審理流程頁顯示（查詢模式為獨立功能，不顯示審理步驟列）
if not st.session_state["law_lookup_mode"]:
    render_steps(st.session_state["step"])


# ============ 獨立功能：法律小幫手 ============
# 與審理進度主線無關的獨立工具：使用者自行上傳/貼文字 → 自動判定案由（內部 classify）
# → KB 檢索 1 次 + Haiku 綜合 1 次 → Markdown 呈現。不進草稿、不做引用校驗。
if st.session_state["law_lookup_mode"]:
    st.markdown("### 　💡 法律小幫手")
    st.caption("上傳或貼上文件，查詢相關法規與相似歷史案例。")

    ll_upload = st.file_uploader("文件（PDF 或 純文字檔）", type=["pdf", "txt"], key="ll_upload")
    with st.expander("或改用貼上文字"):
        st.text_area("案情或文件全文", height=160, key="ll_pasted")

    if st.button("🔍 查詢相關法規與案例", type="primary", use_container_width=True):
        # 只在按下按鈕時才讀檔（避免每次 rerun / 瀏覽器重新整理都讀上傳物件，
        # 讀到已消耗或殘留的 uploader 會拋例外導致整頁空白）。
        ll_text = ""
        try:
            if ll_upload is not None:
                ll_text = _read_upload(ll_upload)
        except Exception:                       # noqa: BLE001 — 讀檔失敗不應讓整頁崩潰
            ll_text = ""
        if not ll_text.strip():
            ll_text = st.session_state.get("ll_pasted") or ""

        if not ll_text.strip():
            st.error("請先上傳文件或貼上文字。")
        else:
            with st.spinner("去識別化 → 檢索知識庫 → 綜合整理…"):
                # 競賽規範：個資不得送進 AWS，檢索與 LLM 前先去識別化
                safe_text, _ = deidentify(ll_text)
                # route_key=None：由 lookup_from_text 內部以 classify 自動判定案由（0 呼叫）
                st.session_state["law_lookup_result"] = lookup_from_text(
                    {"petition": safe_text},
                    client=get_client(),
                )
            st.rerun()

    if "law_lookup_result" in st.session_state:
        res = st.session_state["law_lookup_result"]
        st.markdown(
            f'<div class="law-card"><div class="badge gold">'
            f'案由：{ROUTE_LABELS.get(res.get("route_key",""), res.get("route_key",""))}</div>'
            f'<span style="margin-left:10px;color:var(--muted)">'
            f'參考知識庫 {res.get("retrieved_count",0)} 筆檢索結果</span></div>',
            unsafe_allow_html=True)
        st.markdown(res.get("markdown", "") or "（無回覆）")

        st.markdown("---")
        st.button("🔄 重新查詢", use_container_width=True, on_click=reset_law_lookup,
                  key="ll_reset")

    st.stop()   # 查詢模式為獨立頁，不再往下渲染審理主線


# ============ 第 1 階段：案件受理與分析 ============
if st.session_state["step"] == 1:
    st.markdown("### 　一、案件受理與分析")
    st.caption("上傳訴願書與原處分書，系統自動擷取欄位、判定案由，並檢索相關法規與歷史案例。")

    up_petition = st.file_uploader("訴願書（PDF 或 純文字檔）　必填", type=["pdf", "txt"],
                                   key="up_petition")

    up_disposition = st.file_uploader("原處分書（PDF 或 純文字檔）　建議上傳", type=["pdf", "txt"],
                                      key="up_disposition")
    st.caption("　　原處分書提供處分機關、法令依據、送達日期等資訊；未提供時無法核算訴願期間與原處分瑕疵。")

    up_reply = st.file_uploader("機關答辯書（PDF 或 純文字檔）　可選", type=["pdf", "txt"],
                                key="up_reply")
    st.caption("　　機關答辯書常於審理中始送達，未提供不影響流程。")

    with st.expander("或改用貼上文字（沒有檔案時）"):
        pasted_petition = st.text_area("訴願書全文", height=160, key="paste_petition")

    if st.button("⚖️ 受理並執行分析", type="primary", use_container_width=True):
        # 1) 取文字：優先用上傳檔，否則用貼上的文字
        petition_txt = _read_upload(up_petition) if up_petition else (pasted_petition or "")
        disposition_txt = _read_upload(up_disposition) if up_disposition else ""
        reply_txt = _read_upload(up_reply) if up_reply else ""

        if not petition_txt.strip():
            st.error("請至少提供訴願書（上傳檔案或貼上文字）。")
        else:
            with st.spinner("去識別化 → 擷取欄位 → 案由分類 → 檢索法規與案例…"):
                # 2) 去識別化（競賽規範：個資不得送進 AWS；文字會經 Bedrock 擷取）
                petition_txt, _ = deidentify(petition_txt)
                if disposition_txt:
                    disposition_txt, _ = deidentify(disposition_txt)
                if reply_txt:
                    reply_txt, _ = deidentify(reply_txt)

                # 3) 原始文字 → 交接 JSON（intake：分類 0 呼叫 + 擷取 1 呼叫）
                case_text = {
                    "petition": petition_txt,
                    "original_disposition_doc": disposition_txt,
                    "agency_reply_doc": reply_txt,
                }
                payload = intake(case_text, client=get_client())

                # 4) 分析（路由 + KB 檢索 + 自動判定主文）
                st.session_state["analysis"] = analyze_case(payload, client=get_client())
            st.session_state.pop("draft_result", None)
            st.rerun()

    if "analysis" in st.session_state:
        a = st.session_state["analysis"]
        if a.get("error"):
            st.error(f"{a['error']}：{a.get('problems')}")
            st.stop()

        # 不受理案主文於程序階段即確定（快速通道，不進撰稿）；其餘案件的主文
        # 由第二階段撰稿時 LLM 依事實與法律判斷，此處僅顯示初步參考方向。
        if a.get("inadmissible"):
            mid_title, mid_value = "程序判定", a.get("decided_disposition", "")
        else:
            mid_title, mid_value = "初步參考方向", f"{a.get('decision_basis','')}"

        c1, c2 = st.columns(2)
        c1.markdown(f'<div class="law-card"><div class="badge gold">案由分類</div>'
                    f'<h3 style="margin:8px 0 0">{ROUTE_LABELS.get(a["route_key"], a["route_key"])}</h3></div>',
                    unsafe_allow_html=True)
        c2.markdown(f'<div class="law-card"><div class="badge">{mid_title}</div>'
                    f'<p style="margin:8px 0 0">{mid_value}</p></div>',
                    unsafe_allow_html=True)

        if not a.get("inadmissible"):
            st.caption("　主文將於下一階段由系統依事實與法律判斷後產生。")
        else:
            st.warning(f"本案程序不受理（{a.get('inadmissible_note','')}），將套用不受理款次模板。")

        with st.expander("📚 檢索到的推薦法規", expanded=True):
            laws = a.get("recommended_laws", [])
            if laws:
                for law in laws:
                    cite = law.get("citation", "") or law.get("law", "")
                    reason = law.get("reason", "")
                    st.markdown(f"- **{cite}**　{reason}" if reason else f"- **{cite}**")
            else:
                st.markdown("- （本案無相關法規檢索結果）")
        st.markdown("#### 📂 相似歷史案例")
        sims = a.get("similar_cases", [])
        if sims:
            # 每筆一個可展開項：標題顯示案號＋主文，點開看全文（避免頁面過長）
            for s in sims:
                doc = s.get("doc_id", "") or "（案號未標）"
                disp = s.get("disposition", "")
                full = s.get("full_text") or s.get("reasoning_summary", "")
                label = f"{doc}　（{disp}）" if disp else doc
                with st.expander(label):
                    st.markdown(full or "（無內文）")
        else:
            st.caption("（本案無相似歷史案例）")

        st.markdown("---")
        _, nav = st.columns([3, 1])
        nav.button("下一步：草稿研擬 ▶", type="primary", use_container_width=True,
                   on_click=goto, args=(2,))


# ============ 第 2 階段：決定書草稿 ============
elif st.session_state["step"] == 2:
    st.markdown("### 　二、決定書草稿")
    st.caption("系統依事實、可引用法條與檢索結果判斷主文並撰寫理由，可下載為 PDF。")

    if "analysis" not in st.session_state:
        st.warning("請先完成第一階段分析。")
        st.button("◀ 返回第一階段", on_click=goto, args=(1,))
    else:
        a = st.session_state["analysis"]

        if st.button("✍️ 生成決定書草稿", type="primary", use_container_width=True):
            with st.spinner("研擬決定書草稿中…"):
                st.session_state["draft_result"] = generate_case_draft(a, client=get_client())
            st.rerun()

        if "draft_result" in st.session_state:
            r = st.session_state["draft_result"]

            # 在草稿上方顯著顯示系統判斷結果（主文）
            disp = r.get("disposition") or r["draft"].get("main", "")
            st.markdown(
                f'<div class="verdict-banner">'
                f'<span class="verdict-label">系統判斷結果</span>'
                f'<span class="verdict-value">{disp}</span>'
                f'</div>', unsafe_allow_html=True)

            # 只有系統確定抓到問題（規則層紅燈，重寫後仍未解）才提示人工複核；
            # 並把「為什麼」有理有據列出來（檢查事項 + 說明 + 證據），而非籠統一句話。
            # 其餘一律視為通過，品質檢核在系統內部進行，不對使用者展示明細。
            if r.get("needs_human_review"):
                red_checks = [c for c in r.get("verification", {}).get("checks", [])
                              if c.get("status") == "fail" and c.get("level") == "red"]
                st.error("⚠️ 本件經系統檢核發現下列問題，建議人工複核後再行核發：")
                for c in red_checks:
                    ev = "；".join(str(e) for e in c.get("evidence", []))
                    body = (f'<div class="law-card" style="border-left-color:#c0392b">'
                            f'<div class="badge" style="background:#c0392b">{c.get("name","")}</div>'
                            f'<p style="margin:8px 0 4px">{c.get("detail","")}</p>'
                            + (f'<p style="margin:0;color:#666;font-size:0.9rem">證據：{ev}</p>' if ev else "")
                            + '</div>')
                    st.markdown(body, unsafe_allow_html=True)

            _render_doc(r["draft"])

            case_id = a.get("case_id") or "訴願決定書草稿"
            st.download_button(
                "⬇️ 下載草稿（PDF）",
                data=_draft_to_pdf(r["draft"]),
                file_name=f"{case_id}_訴願決定書草稿.pdf",
                mime="application/pdf",
                type="primary",
                use_container_width=True,
            )

        st.markdown("---")
        b1, _, b2 = st.columns([1, 2, 1])
        b1.button("◀ 上一步", use_container_width=True, on_click=goto, args=(1,))
        b2.button("🔄 審理新案件", use_container_width=True, on_click=reset_review,
                  key="stage2_reset")
