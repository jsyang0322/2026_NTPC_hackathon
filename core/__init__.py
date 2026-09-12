"""核心業務邏輯套件：純函式，dict 進 dict 出，不綁執行環境。

單體應用、Lambda、Step Functions 皆共用此套件。
所有 LLM 呼叫一律透過 core.bedrock_client。
"""
