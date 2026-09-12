#!/usr/bin/env python3
"""Demo 啟動前置檢查（preflight）。

在正式 Demo 前跑一次，擋掉三個「程式能跑真的、但預設會靜默用假資料」的陷阱：

  1. BEDROCK_KB_ID 未設 → kb.retrieve() 會靜默回 mock，等於「假檢索餵真撰稿」。
  2. dry-run 開著（BEDROCK_DRY_RUN=1）→ 全程回假字串、法官永遠 pass。
  3. data/cache/ 內有 dry-run 假回應 → 因快取以 payload hash 為 key 且先於 dry_run
     判斷，關掉 dry-run 仍可能命中舊的假回應。

用法：
    python -m scripts.preflight_check              # 只檢查，不改任何東西
    python -m scripts.preflight_check --purge-cache  # 順手清掉 dry-run 假快取
    python -m scripts.preflight_check --probe        # 額外做一次真實 KB/Bedrock 連通測試

離開碼：全部通過回 0；有任一紅燈回 1（可接進 CI 或啟動腳本）。
本腳本唯讀為主；只有帶 --purge-cache 才會刪檔，且只刪標記 dry_run 的快取。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# 讓腳本可直接執行（python scripts/preflight_check.py）也能被 -m 匯入
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# 觸發 core 的 .env 自動載入，讓 preflight 看到的環境與實際執行時一致
# （否則會誤判「KB 未設」——實際跑 pipeline 時 core 會把它載進來）。
try:
    from core import env as _env
    _env.load()
except Exception:  # noqa: BLE001 — core 不可用時 preflight 仍能純檢查環境變數
    pass

CACHE_DIR = _ROOT / "data" / "cache"

# ANSI 顏色（非 TTY 時自動關閉，避免污染日誌）
_TTY = sys.stdout.isatty()


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _TTY else text


def _ok(msg: str) -> None:
    print(f"  {_c('32', '✅ PASS')}  {msg}")


def _warn(msg: str) -> None:
    print(f"  {_c('33', '🟡 WARN')}  {msg}")


def _fail(msg: str) -> None:
    print(f"  {_c('31', '🔴 FAIL')}  {msg}")


# ---------------------------------------------------------------------------
# 檢查 1：KB id 已設
# ---------------------------------------------------------------------------
def check_kb_id() -> bool:
    print(_c("1", "\n[1/3] Bedrock Knowledge Base 設定"))
    generic = os.environ.get("BEDROCK_KB_ID", "").strip()

    # 各案由專用 KB（有設任何一個也算有效檢索來源）
    route_envs = {
        "money_laundering": "BEDROCK_KB_ID_MONEY_LAUNDERING",
        "waste": "BEDROCK_KB_ID_WASTE",
        "air_pollution": "BEDROCK_KB_ID_AIR_POLLUTION",
        "building": "BEDROCK_KB_ID_BUILDING",
        "noise": "BEDROCK_KB_ID_NOISE",
        "general": "BEDROCK_KB_ID_GENERAL",
    }
    dedicated = {rk: v for rk, env in route_envs.items()
                 if (v := os.environ.get(env, "").strip())}

    if generic:
        _ok(f"BEDROCK_KB_ID = {generic}")
    elif dedicated:
        _ok(f"未設通用 KB，但有案由專用 KB：{list(dedicated)}")
    else:
        _fail("BEDROCK_KB_ID 未設，且無任何案由專用 KB。")
        print("         → kb.retrieve() 會靜默回 mock，檢索階段將是假資料。")
        print("         → 修正：set -a; source .env; set +a  （或啟動前帶 BEDROCK_KB_ID=...）")
        return False

    if not generic and dedicated:
        _warn("只有部分案由設了專用 KB，其餘案由（無專用且無通用）檢索會回 mock。")
    return True


# ---------------------------------------------------------------------------
# 檢查 2：dry-run 已關
# ---------------------------------------------------------------------------
def check_dry_run() -> bool:
    print(_c("1", "\n[2/3] 執行模式（dry-run 需為關）"))
    val = os.environ.get("BEDROCK_DRY_RUN", "0").strip()
    if val == "1":
        _fail("BEDROCK_DRY_RUN=1 → 全程回假字串、KB 回 mock、法官永遠自動 pass。")
        print("         → 修正：export BEDROCK_DRY_RUN=0")
        print("         → 另注意 Streamlit 側邊欄「Dry-run 模式」開關預設是開的，Demo 時要手動關閉。")
        return False
    _ok(f"BEDROCK_DRY_RUN={val}（真實呼叫模式）")
    _warn("提醒：Streamlit UI 的 Dry-run 開關預設為開，此環境變數管不到 UI，Demo 時仍需手動關掉。")
    return True


# ---------------------------------------------------------------------------
# 檢查 3：快取無 dry-run 污染
# ---------------------------------------------------------------------------
def _scan_dry_run_cache() -> list[Path]:
    """回傳所有標記 dry_run=true 的快取檔。"""
    if not CACHE_DIR.exists():
        return []
    hits: list[Path] = []
    for p in sorted(CACHE_DIR.glob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        # dry_run 回應：明確標 dry_run=true，或內文帶 DRY_RUN 標記
        if data.get("dry_run") is True or "[DRY_RUN::" in str(data.get("text", "")):
            hits.append(p)
    return hits


def check_cache(purge: bool) -> bool:
    print(_c("1", "\n[3/3] 回應快取（不得含 dry-run 假回應）"))
    if not CACHE_DIR.exists():
        _ok(f"快取目錄不存在（{CACHE_DIR.relative_to(_ROOT)}），無污染風險。")
        return True

    dirty = _scan_dry_run_cache()
    total = len(list(CACHE_DIR.glob("*.json")))
    if not dirty:
        _ok(f"快取共 {total} 檔，無 dry-run 假回應。")
        return True

    if purge:
        for p in dirty:
            p.unlink(missing_ok=True)
        _ok(f"已清除 {len(dirty)} 個 dry-run 假快取（原 {total} 檔，剩 {total - len(dirty)}）。")
        return True

    _fail(f"快取共 {total} 檔，其中 {len(dirty)} 個是 dry-run 假回應：")
    for p in dirty[:10]:
        print(f"         - {p.name}")
    if len(dirty) > 10:
        print(f"         … 還有 {len(dirty) - 10} 個")
    print("         → 因快取以 prompt hash 為 key 且先於 dry_run 判斷，關掉 dry-run 仍可能命中假回應。")
    print("         → 修正：python -m scripts.preflight_check --purge-cache")
    return False


# ---------------------------------------------------------------------------
# 選配：真實連通測試（會實際呼叫 1 次 KB + 1 次 Bedrock，受 ≤1 RPS 閘門保護）
# ---------------------------------------------------------------------------
def probe_live() -> bool:
    print(_c("1", "\n[選配] 真實連通測試（KB Retrieve + Bedrock 各 1 次）"))
    ok = True
    try:
        from core import kb
        from core.bedrock_client import BedrockClient
    except Exception as exc:  # noqa: BLE001
        _fail(f"匯入 core 失敗：{exc}")
        return False

    # 用臨時快取目錄，避免污染正式快取，也確保打的是真實路徑
    import tempfile
    tmp = tempfile.mkdtemp(prefix="ntpc_preflight_")
    client = BedrockClient(dry_run=False, cache_dir=tmp)

    # KB
    try:
        hits = kb.retrieve("洗錢防制法 交付帳戶 告誡", "money_laundering",
                           num_results=2, client=client)
        real = [h for h in hits if h.get("source") != "mock"]
        if real:
            _ok(f"KB Retrieve 回 {len(hits)} 筆，來源真實（例：{str(real[0].get('source',''))[:50]}）")
        else:
            _fail("KB Retrieve 回的是 mock（檢查 BEDROCK_KB_ID 與權限）。")
            ok = False
    except Exception as exc:  # noqa: BLE001
        _fail(f"KB Retrieve 失敗：{exc}")
        ok = False

    # Bedrock 文字生成
    try:
        text = client.converse(
            [{"role": "user", "content": "回覆兩個字：正常"}],
            max_tokens=16, use_cache=False,
        )
        if "[DRY_RUN::" in text:
            _fail("Bedrock 回的是 dry-run 假字串。")
            ok = False
        else:
            _ok(f"Bedrock InvokeModel 正常回應（{text.strip()[:20]}…）")
    except Exception as exc:  # noqa: BLE001
        _fail(f"Bedrock 呼叫失敗：{exc}")
        ok = False

    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description="Demo 啟動前置檢查")
    parser.add_argument("--purge-cache", action="store_true",
                        help="清除 data/cache 內的 dry-run 假回應")
    parser.add_argument("--probe", action="store_true",
                        help="額外做一次真實 KB/Bedrock 連通測試（會各呼叫 1 次）")
    args = parser.parse_args()

    print(_c("1", "=== Demo 前置檢查（preflight）==="))
    results = [
        check_kb_id(),
        check_dry_run(),
        check_cache(args.purge_cache),
    ]
    if args.probe:
        results.append(probe_live())

    print()
    if all(results):
        print(_c("32", "全部通過，可以開始 Demo。"))
        return 0
    print(_c("31", "有項目未通過，請依上面指示修正後再跑。"))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
