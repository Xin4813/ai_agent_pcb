"""步骤③：BOM + 需求 → 引脚级网表 JSON。LLM 生成 + 校验-重试闭环。"""
from __future__ import annotations
import json
import re
from .knowledge import lookup_board
from .validator import validate_netlist

SYS_NET = (
    "你是电路设计助手。已知元器件清单(含 KiCad 符号)和用户需求，请给出引脚级连接关系。"
    "只输出 JSON。格式："
    '{"nets":[{"name":"网络名(电源统一用 VCC/GND)","pins":["位号:引脚号"]}]}。'
    "要求：每个引脚号必须真实存在于对应元器件的 KiCad 符号中；同一引脚不能出现在两个网络；"
    "不要输出 JSON 以外的任何内容。"
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


def generate_netlist(prompt: str, bom: dict, llm=None, mock: bool = False, max_retries: int = 3):
    board = lookup_board(prompt)
    if mock or llm is None:
        if board is None:
            raise RuntimeError("离线模式且未匹配到内置板卡。")
        return board["netlist"], False

    bom_text = json.dumps(bom, ensure_ascii=False)
    user = f"需求: {prompt}\n元器件清单:\n{bom_text}\n请给出引脚级连接网表。"
    last = None
    for _ in range(max_retries):
        try:
            raw = llm.complete(SYS_NET, user)
            nl = _extract_json(raw)
            validate_netlist(nl, bom)
            return nl, True
        except Exception as e:
            last = e
    raise RuntimeError(f"网表生成失败: {last}")
