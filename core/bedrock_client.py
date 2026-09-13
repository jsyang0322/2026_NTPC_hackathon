"""所有 LLM / Bedrock 呼叫的唯一入口。

競賽硬性規定：Amazon Bedrock 請求須 ≤ 1 RPS（原文：每秒 1 個請求以下）。

限流有兩層，缺一不可：
  1. 行程內：threading.Lock，處理同一行程的多執行緒。
  2. 跨行程：檔案鎖 + 時間戳（見 throttle()），處理「同一台機器上多個 Python
     行程同時呼叫 Bedrock」的情況——例如 Streamlit 介面與 henry/ 的批次腳本
     同時執行。若只有行程內限流，兩邊各自守 1 RPS，合計卻是 2 RPS，違反規範。

extract / issues / defects / draft / critic / similar_cases 等模組
一律透過此 client 呼叫，不得直接 import boto3 打 Bedrock。
若某支程式因 API 形狀不同必須自行呼叫 boto3（如 henry/rag_triage.py 用
invoke_model），至少須在呼叫前呼叫本模組的 throttle()，共用同一個閘門。

設計為「不綁環境」：單體應用與 Lambda 都能直接使用同一個實例。
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# 預設模型（us-west-2 實測 ACTIVE）。
# ★ 重要：Claude Sonnet 4.5 / Haiku 4.5 不支援裸模型 ID 的 on-demand 呼叫，
#   必須用「inference profile」ID（加 us. 前綴），否則會 ValidationException。
# 分層理由：配額與延遲，非地端與雲端之分（見說明書 §1.4）
MODEL_WRITER = os.environ.get("BEDROCK_MODEL_WRITER", "us.anthropic.claude-sonnet-4-5-20250929-v1:0")
MODEL_LIGHT = os.environ.get("BEDROCK_MODEL_LIGHT", "us.anthropic.claude-haiku-4-5-20251001-v1:0")
EMBED_MODEL = os.environ.get("BEDROCK_EMBED_MODEL", "cohere.embed-multilingual-v3")

AWS_REGION = os.environ.get("AWS_REGION", "us-west-2")

# 全專案共用的最小呼叫間隔（秒）。1.05 而非 1.0 是留安全邊際，實測約 0.95 RPS。
MIN_INTERVAL = float(os.environ.get("BEDROCK_MIN_INTERVAL", "1.05"))

# 全域關閉快取：BEDROCK_NO_CACHE=1 時，converse 整趟不讀也不寫 data/cache，
# 強制每個 LLM 呼叫都真實發出——用於驗證「確實端到端真呼叫」或現場 demo。
# 注意會犧牲 ≤1 RPS 下的速度（每次都真打），故預設關閉。
def _no_cache() -> bool:
    return os.environ.get("BEDROCK_NO_CACHE", "0") == "1"


# ===========================================================================
# 跨行程限流閘門
# ===========================================================================
# 為什麼需要：行程內的 Lock 只能協調同一個 Python 行程。競賽規範限制的是「整個
# 帳號對 Bedrock 的請求速率」，因此只要有兩個行程同時跑（Streamlit + 批次腳本），
# 行程內限流就失效。以下用「原子建目錄」當跨行程互斥鎖，配一個時間戳檔記錄上次
# 呼叫時刻。os.mkdir 在 POSIX 與 Windows 皆為原子操作，不需 fcntl（Windows 沒有）。

#: 閘門狀態存放位置。預設放系統暫存目錄，讓同機所有行程共用，且不會被 commit。
_RATE_STATE = Path(os.environ.get(
    "BEDROCK_RATE_STATE", Path(tempfile.gettempdir()) / "ntpc_appeal_bedrock_rate"))

#: 鎖被視為陳舊（持有者可能已崩潰）的秒數，超過即強制回收。
_STALE_LOCK_SECONDS = 30.0

#: 等不到鎖的上限秒數；逾時採保守作法（照睡一個間隔）而非直接放行。
_GATE_TIMEOUT = 60.0

_local_lock = threading.Lock()


def _lock_dir() -> Path:
    return Path(str(_RATE_STATE) + ".lock")


def _stamp_file() -> Path:
    return Path(str(_RATE_STATE) + ".stamp")


def throttle(min_interval: float | None = None) -> None:
    """在呼叫 Bedrock 前等待，確保同機所有行程合計 ≤ 1 RPS。

    任何無法透過 BedrockClient.converse 的呼叫（例如 KB Retrieve、
    henry/rag_triage.py 的 invoke_model）都應在呼叫前先呼叫本函式。

    以 wall-clock 時間記錄跨行程狀態（monotonic 不可跨行程比較）。
    """
    interval = MIN_INTERVAL if min_interval is None else min_interval
    with _local_lock:                       # 先擋同行程的其他執行緒
        if not _acquire(interval):
            time.sleep(interval)            # 取不到鎖：保守等一個間隔
            return
        try:
            last = _read_stamp()
            wait = interval - (time.time() - last)
            if wait > 0:
                time.sleep(wait)
            _write_stamp(time.time())
        finally:
            _release()


def _acquire(interval: float) -> bool:
    """取得跨行程鎖。回傳 False 表示逾時未取得。"""
    lock = _lock_dir()
    lock.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + _GATE_TIMEOUT
    while True:
        try:
            os.mkdir(lock)                  # 原子操作：成功即代表取得鎖
            return True
        except FileExistsError:
            if _reclaim_if_stale(lock):
                continue
            if time.time() > deadline:
                return False
            time.sleep(0.02)
        except OSError:
            return False                    # 檔案系統不可用時不擋流程


def _reclaim_if_stale(lock: Path) -> bool:
    """持有者崩潰時回收陳舊鎖，避免整批流程卡死。"""
    try:
        if time.time() - lock.stat().st_mtime > _STALE_LOCK_SECONDS:
            os.rmdir(lock)
            return True
    except (FileNotFoundError, OSError):
        return True                         # 鎖剛被釋放，重試即可
    return False


def _release() -> None:
    try:
        os.rmdir(_lock_dir())
    except OSError:
        pass


def _read_stamp() -> float:
    try:
        return float(_stamp_file().read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0.0


def _write_stamp(ts: float) -> None:
    try:
        _stamp_file().write_text(str(ts), encoding="utf-8")
    except OSError:
        pass


# ===========================================================================
# InvokeModel body 組裝與回應解析
# ===========================================================================
# 競賽帳號的允許清單只授權 bedrock:InvokeModel，未授權 bedrock:Converse。
# 故所有文字生成走 InvokeModel。不同模型家族的 body / 回應形狀不同：
#   - Anthropic Claude：Messages API（anthropic_version + messages + system 頂層）
#   - Amazon Nova：messages + inferenceConfig，回應在 output.message.content
# 以 model_id 內的關鍵字分派，與 henry/rag_triage.py 的 nova() 對齊。

def _to_text_content(content: Any) -> str:
    """把 Converse 風格的 content（[{"text": ...}] 或字串）攤平成純文字。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and "text" in block:
                parts.append(block["text"])
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return str(content)


def _build_invoke_body(model_id: str, messages: list[dict], system: str | None,
                       temperature: float, max_tokens: int) -> dict:
    """把 Converse 風格的 messages 轉成 InvokeModel 的 body。"""
    if "anthropic" in model_id:
        body: dict[str, Any] = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": [
                {"role": m.get("role", "user"),
                 "content": _to_text_content(m.get("content", ""))}
                for m in messages
            ],
        }
        if system:
            body["system"] = system
        return body

    # Amazon Nova / 其他採 Converse-on-invoke 形狀的模型
    body = {
        "messages": [
            {"role": m.get("role", "user"),
             "content": [{"text": _to_text_content(m.get("content", ""))}]}
            for m in messages
        ],
        "inferenceConfig": {"temperature": temperature, "maxTokens": max_tokens},
    }
    if system:
        body["system"] = [{"text": system}]
    return body


def _extract_invoke_text(model_id: str, payload: dict) -> str:
    """從 InvokeModel 回應取出純文字。"""
    if "anthropic" in model_id:
        blocks = payload.get("content", [])
        return "".join(b.get("text", "") for b in blocks if isinstance(b, dict))
    # Amazon Nova
    return payload["output"]["message"]["content"][0]["text"]


@dataclass
class BedrockClient:
    """全域 ≤ 1 RPS 的 Bedrock 呼叫入口。

    參數：
        min_interval: 兩次呼叫之間的最小間隔秒數（1.05 留安全邊際）。
        cache_dir:    依 prompt hash 落檔的快取目錄。
        max_retries:  ThrottlingException 的指數退避重試次數。
        dry_run:      True 時不呼叫真實 Bedrock，回傳 mock，用於離線開發與測試。
    """

    min_interval: float = MIN_INTERVAL
    cache_dir: str = "data/cache"
    max_retries: int = 5
    dry_run: bool = False
    region: str = AWS_REGION

    _client: Any = field(default=None, init=False)

    def __post_init__(self) -> None:
        Path(self.cache_dir).mkdir(parents=True, exist_ok=True)

    # --- 內部：延遲建立 boto3 client（dry_run 時完全不建立）---
    def _bedrock(self):
        if self._client is None:
            import boto3  # 延遲 import，dry_run 或未裝 boto3 時不受影響

            self._client = boto3.client("bedrock-runtime", region_name=self.region)
        return self._client

    # --- 內部：限流，確保 ≤ 1 RPS（行程內 + 跨行程） ---
    def _throttle(self) -> None:
        """委派給模組級 throttle()，與其他行程（含 henry/）共用同一個閘門。"""
        throttle(self.min_interval)

    # --- 內部：快取鍵 ---
    @staticmethod
    def _hash(payload: dict) -> str:
        blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]

    def _cache_path(self, key: str) -> Path:
        return Path(self.cache_dir) / f"{key}.json"

    def _cache_get(self, key: str) -> dict | None:
        p = self._cache_path(key)
        if not p.exists():
            return None
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        # 防呆：離線（dry-run）時寫下的假回應絕不當真結果回傳。
        # 正常情況下這類檔案不會產生（見 converse），此處為相容舊快取的第二道防線。
        if data.get("dry_run"):
            return None
        return data

    def _cache_put(self, key: str, value: dict) -> None:
        self._cache_path(key).write_text(
            json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # --- 對外：文字生成（InvokeModel）---
    def converse(
        self,
        messages: list[dict],
        model_id: str | None = None,
        system: str | None = None,
        temperature: float = 0.2,
        max_tokens: int = 4096,
        use_cache: bool = True,
    ) -> str:
        """呼叫 Bedrock 產生文字，回傳純文字。受全域限流與快取保護。

        對外沿用 Converse 風格的 messages 介面
        （[{"role","content":[{"text":...}]}]），呼叫端不需改動。
        內部改走 InvokeModel：競賽帳號的允許清單只授權
        bedrock:InvokeModel，未授權 bedrock:Converse，Converse 會被 IAM 擋下。
        """
        model_id = model_id or MODEL_WRITER
        # BEDROCK_NO_CACHE=1 全域強制關閉快取，優先於呼叫端的 use_cache。
        use_cache = use_cache and not _no_cache()
        payload = {
            "model_id": model_id,
            "messages": messages,
            "system": system,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        key = self._hash(payload)

        if use_cache:
            cached = self._cache_get(key)
            if cached is not None:
                return cached["text"]

        if self.dry_run:
            # 離線 mock 一律不寫快取：避免假回應汙染快取、於日後真實執行被當真結果回傳。
            return f"[DRY_RUN::{model_id}] 離線模式回應，未呼叫真實 Bedrock。"

        body = _build_invoke_body(model_id, messages, system, temperature, max_tokens)
        text = self._invoke_with_retry(model_id, body)
        if use_cache:
            self._cache_put(key, {"text": text})
        return text

    def _invoke_with_retry(self, model_id: str, body: dict) -> str:
        from botocore.exceptions import ClientError

        blob = json.dumps(body, ensure_ascii=False)
        delay = 1.0
        for attempt in range(self.max_retries):
            self._throttle()
            try:
                resp = self._bedrock().invoke_model(modelId=model_id, body=blob)
                payload = json.loads(resp["body"].read())
                return _extract_invoke_text(model_id, payload)
            except ClientError as e:
                code = e.response.get("Error", {}).get("Code", "")
                if code in ("ThrottlingException", "TooManyRequestsException") and attempt < self.max_retries - 1:
                    time.sleep(delay)
                    delay *= 2  # 指數退避
                    continue
                raise
        raise RuntimeError("Bedrock 重試次數耗盡")

    # --- 對外：embedding ---
    def embed(self, texts: list[str], model_id: str | None = None, use_cache: bool = True) -> list[list[float]]:
        """回傳每段文字的向量。dry_run 回傳零向量以驗證串接。"""
        model_id = model_id or EMBED_MODEL
        if self.dry_run:
            return [[0.0] * 8 for _ in texts]
        # 實作留待 build_index 階段；此處保留介面
        raise NotImplementedError("embed 將於 §9.1 索引建置階段實作")


# 模組級單例：全流程共用同一個限流器，確保全域 ≤ 1 RPS
_default_client: BedrockClient | None = None


def get_client(dry_run: bool | None = None) -> BedrockClient:
    """取得全域共用的 BedrockClient。

    dry_run 可由參數或環境變數 BEDROCK_DRY_RUN=1 控制。
    """
    global _default_client
    if dry_run is None:
        dry_run = os.environ.get("BEDROCK_DRY_RUN", "0") == "1"
    if _default_client is None or _default_client.dry_run != dry_run:
        _default_client = BedrockClient(dry_run=dry_run)
    return _default_client
