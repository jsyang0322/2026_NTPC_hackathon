"""模擬案卷生成（§5.1）。[LLM]

由實體決定書的事實欄反推：訴願書 + 原處分書 + 答辯書。
關鍵：生成訴願書時只給「訴願意旨」段（不給「緣…」段，避免法律定性洩漏，§12.3）；
原處分書須忠實保留瑕疵（如 114#18 事實欄空白），供 §7 健檢測試。
個資一律 ○ 遮蔽。
"""

from __future__ import annotations

from core.bedrock_client import get_client, MODEL_WRITER


def simulate_case(decision_facts: dict, client=None) -> dict:
    """回傳 {"petition": str, "disposition": str, "reply": str}。"""
    client = client or get_client()
    _ = client
    # TODO(§5.1): 以 prompts/simulate_case.txt 呼叫 MODEL_WRITER
    return {"petition": "", "disposition": "", "reply": "", "_stub": True}


if __name__ == "__main__":
    print(simulate_case({}))
