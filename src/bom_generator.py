"""步骤②：自然语言 → BOM 元器件清单。支持 LLM 模式与离线知识库回退。"""
from __future__ import annotations
import json
import re
from .knowledge import lookup_board
from .validator import validate_bom

SYS_BOM = (
    "你是电子工程师助手。根据用户自然语言需求，列出电路板所需的元器件清单。"
    "只输出 JSON，不要任何解释文字。格式："
    '{"project":"项目名","items":[{"ref":"位号如U1","name":"名称","value":"标称值",'
    '"qty":数量,"symbol":"KiCad 符号库ID 如 Device:C","footprint":"封装(可空)","desc":"说明(可空)"}]}'
)


def _extract_json(text: str):
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        text = m.group(1)
    s, e = text.find("{"), text.rfind("}")
    if s >= 0 and e >= 0:
        text = text[s:e + 1]
    return json.loads(text)


def generate_bom(prompt: str, llm=None, mock: bool = False, max_retries: int = 3):
    board = lookup_board(prompt)
    if mock or llm is None:
        if board is None:
            raise RuntimeError("离线模式且未匹配到内置板卡；请提供 LLM 密钥以生成。")
        return board["bom"], False

    last = None
    for _ in range(max_retries):
        try:
            raw = llm.complete(SYS_BOM, prompt)
            bom = _extract_json(raw)
            validate_bom(bom)
            return bom, True
        except Exception as e:
            last = e
    raise RuntimeError(f"BOM 生成失败: {last}")
