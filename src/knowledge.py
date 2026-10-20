"""离线设计知识库（无 LLM 密钥时的可运行回退）。

目前内置：Arduino 最小系统板（ATmega328P-PU）。
真实作业里这部分应由 LLM 在步骤②③生成；这里用于在没密钥时把整条流水线演示通。
"""
from __future__ import annotations

ARDUINO_MINIMAL = {
    "project": "Arduino 最小系统板 (ATmega328P-PU)",
    "bom": {
        "project": "Arduino 最小系统板 (ATmega328P-PU)",
        "items": [
            {"ref": "U1", "name": "ATmega328P-PU", "value": "ATmega328P-P", "qty": 1,
             "symbol": "MCU_Microchip_ATmega:ATmega328P-P", "footprint": "Package_DIP:DIP-28_W11.18mm"},
            {"ref": "Y1", "name": "晶振 16MHz", "value": "16MHz", "qty": 1,
             "symbol": "Device:Crystal", "footprint": ""},
            {"ref": "C1", "name": "瓷片电容 22pF", "value": "22pF", "qty": 1,
             "symbol": "Device:C", "footprint": ""},
            {"ref": "C2", "name": "瓷片电容 22pF", "value": "22pF", "qty": 1,
             "symbol": "Device:C", "footprint": ""},
            {"ref": "C3", "name": "去耦电容 100nF", "value": "100nF", "qty": 1,
             "symbol": "Device:C", "footprint": ""},
            {"ref": "R1", "name": "电阻 10kΩ", "value": "10k", "qty": 1,
             "symbol": "Device:R", "footprint": ""},
            {"ref": "R2", "name": "电阻 330Ω", "value": "330", "qty": 1,
             "symbol": "Device:R", "footprint": ""},
            {"ref": "D1", "name": "LED 指示灯", "value": "LED", "qty": 1,
             "symbol": "Device:LED", "footprint": ""},
        ],
    },
    "netlist": {
        "nets": [
            {"name": "VCC",   "pins": ["U1:7", "U1:20", "R1:1", "R2:1", "C3:1"]},
            {"name": "GND",   "pins": ["U1:8", "U1:22", "C1:2", "C2:2", "C3:2", "D1:2", "R2:2"]},
            {"name": "RESET", "pins": ["U1:1", "R1:2"]},
            {"name": "XTAL1", "pins": ["U1:9", "Y1:1", "C1:1"]},
            {"name": "XTAL2", "pins": ["U1:10", "Y1:2", "C2:1"]},
            {"name": "LED",   "pins": ["R2:2", "D1:1"]},
        ]
    },
}

_BOARDS = {
    "arduino": ARDUINO_MINIMAL,
    "arduino 最小系统": ARDUINO_MINIMAL,
    "arduino 最小系统板": ARDUINO_MINIMAL,
    "atmega328": ARDUINO_MINIMAL,
    "最小系统板": ARDUINO_MINIMAL,
}


def lookup_board(prompt: str):
    """按关键词匹配内置板卡；匹配不到返回 None（此时应使用 LLM）。"""
    p = prompt.lower()
    for k, v in _BOARDS.items():
        if k in p:
            return v
    return None
