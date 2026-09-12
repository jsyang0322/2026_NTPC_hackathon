"""Router：讀前段輸出的 route_key，分派到對應案由設定（§2 流程第一步）。

route_key 為英文枚舉（schemas.ROUTE_KEYS）。三個專責案由以外一律走 general。
未知 route_key 或低信心（need_human_review）時，安全退回 general，但仍可路由、不擋流程。
"""

from __future__ import annotations

from .schemas import ROUTE_KEYS


def resolve_route(payload: dict) -> str:
    """由交接輸入決定 route_key。未知值一律 general。"""
    ct = payload.get("case_type", {})
    rk = ct.get("route_key", "general")
    if rk not in ROUTE_KEYS:
        return "general"
    return rk


def needs_human_review(payload: dict) -> bool:
    """信心不足時提示人工確認（仍照常路由與撰稿）。"""
    return bool(payload.get("case_type", {}).get("need_human_review", False))
