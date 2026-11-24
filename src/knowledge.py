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
            {"name": "GND",   "pins": ["U1:8", "U1:22", "C1:2", "C2:2", "C3:2", "D1:2"]},
            {"name": "RESET", "pins": ["U1:1", "R1:2"]},
            {"name": "XTAL1", "pins": ["U1:9", "Y1:1", "C1:1"]},
            {"name": "XTAL2", "pins": ["U1:10", "Y1:2", "C2:1"]},
            {"name": "LED",   "pins": ["R2:2", "D1:1"]},
        ]
    },
    # ICSP 编程口与主控 SPI/D 脚是硬件复用关系。LLM 常把 ICSP 网络只写排针一端，
    # 造成「一排针没连」。生成网表后据此把单脚 ICSP 网络并入对应主网络。
    # 值 = 目标网络名（同名即同一电位），键 = 需补全的 ICSP 网络名。
    "interface_aliases": {
        "ICSP_MISO": "D12",
        "ICSP_MOSI": "D11",
        "ICSP_SCK": "D13",
        "ICSP_RESET": "RESET",
        "ICSP_VCC": "VCC",
        "ICSP_GND": "GND",
    },
}

# 8051 最小系统板：真实 40 脚 AT89C51，引脚号必须属于 8051 而不是 AVR。
# 之前 LLM 曾被 AVR 符号目录误导，把 8051 画成 ATmega328P（AREF/7/20 脚），
# 所以这块板固定走内置数据，避免再次串芯片。
def _p8_net(prefix: str, chip_pins: list[int], header: str) -> list[dict]:
    return [{"name": f"{prefix}_{i}", "pins": [f"U1:{chip_pins[i]}", f"{header}:{i + 1}"]}
            for i in range(8)]


MINIMAL_8051 = {
    "pinned": True,
    "bom": {
        "project": "8051最小系统板",
        "items": [
            {"ref": "U1", "name": "AT89C51", "value": "AT89C51", "qty": 1,
             "symbol": "MCU_Microchip_8051:AT89x51xxP", "footprint": "Package_DIP:DIP-40_W15.24mm",
             "desc": "真 8051 内核 40 脚 MCU"},
            {"ref": "Y1", "name": "晶振", "value": "11.0592MHz", "qty": 1,
             "symbol": "Device:Crystal", "footprint": "", "desc": "系统时钟"},
            {"ref": "C1", "name": "陶瓷电容", "value": "30pF", "qty": 1,
             "symbol": "Device:C", "footprint": "", "desc": "晶振负载电容"},
            {"ref": "C2", "name": "陶瓷电容", "value": "30pF", "qty": 1,
             "symbol": "Device:C", "footprint": "", "desc": "晶振负载电容"},
            {"ref": "C3", "name": "电解电容", "value": "10uF", "qty": 1,
             "symbol": "Device:C", "footprint": "", "desc": "上电复位电容"},
            {"ref": "C4", "name": "陶瓷电容", "value": "100nF", "qty": 1,
             "symbol": "Device:C", "footprint": "", "desc": "电源去耦电容"},
            {"ref": "R1", "name": "电阻", "value": "10k", "qty": 1,
             "symbol": "Device:R", "footprint": "", "desc": "复位下拉电阻"},
            {"ref": "R2", "name": "电阻", "value": "10k", "qty": 1,
             "symbol": "Device:R", "footprint": "", "desc": "EA 上拉电阻"},
            {"ref": "SW1", "name": "按键", "value": "RESET", "qty": 1,
             "symbol": "Switch:SW_Push", "footprint": "", "desc": "手动复位按键"},
            {"ref": "J1", "name": "电源排针", "value": "2P", "qty": 1,
             "symbol": "Connector:Conn_01x02_Pin", "footprint": "", "desc": "VCC/GND 电源输入"},
            {"ref": "J2", "name": "P0 排针", "value": "8P", "qty": 1,
             "symbol": "Connector:Conn_01x08_Pin", "footprint": "", "desc": "P0.0~P0.7 扩展"},
            {"ref": "J3", "name": "P1 排针", "value": "8P", "qty": 1,
             "symbol": "Connector:Conn_01x08_Pin", "footprint": "", "desc": "P1.0~P1.7 扩展"},
            {"ref": "J4", "name": "P2 排针", "value": "8P", "qty": 1,
             "symbol": "Connector:Conn_01x08_Pin", "footprint": "", "desc": "P2.0~P2.7 扩展"},
        ],
    },
    "netlist": {
        "nets": [
            {"name": "VCC", "pins": ["U1:40", "C3:1", "C4:1", "R2:1", "J1:1"]},
            {"name": "GND", "pins": ["U1:20", "C1:2", "C2:2", "C4:2", "R1:1", "SW1:2", "J1:2"]},
            {"name": "XTAL1", "pins": ["U1:19", "Y1:1", "C1:1"]},
            {"name": "XTAL2", "pins": ["U1:18", "Y1:2", "C2:1"]},
            {"name": "RST", "pins": ["U1:9", "C3:2", "R1:2", "SW1:1"]},
            {"name": "EA", "pins": ["U1:31", "R2:2"]},
            *_p8_net("P0", [39, 38, 37, 36, 35, 34, 33, 32], "J2"),
            *_p8_net("P1", [1, 2, 3, 4, 5, 6, 7, 8], "J3"),
            *_p8_net("P2", [21, 22, 23, 24, 25, 26, 27, 28], "J4"),
        ]
    },
}


# 可调直流稳压电源（0~5V / 1A，输入 12V）。
# LLM 曾把「可调」需求画成 LM1117-5.0/3.3 两个固定稳压器：输出死钉在 5V/3.3V，
# 无电位器/ADJ 反馈，输入/输出接口与二极管全部悬空，ERC 却是 0（功能错误 ERC 查不出）。
# 这里钉死一套工程上真正可调的拓扑，任何「可调稳压电源」需求都走内置数据：
#   12V → 防反接 → LM317(1.5A) 主稳压；ADJ 由 240Ω + 2k 电位器分压；
#   电位器下端接 ICL7660 电荷泵产生的负电压，使 ADJ 可被拉到 0V 以下 → 输出真正从 0V 起调；
#   LM1117-5.0 给 ICL7660 提供 +5V；输入/输出/保护器件引脚全部接入。
def _adj_psu() -> dict:
    return {
        "pinned": True,
        "bom": {
            "project": "可调直流稳压电源",
            "items": [
                {"ref": "J1", "name": "电源输入端子", "value": "12V", "qty": 1,
                 "symbol": "Connector:Conn_01x02_Pin", "footprint": "", "desc": "12V 直流输入"},
                {"ref": "D1", "name": "防反接二极管", "value": "1N4007", "qty": 1,
                 "symbol": "Diode:1N4007", "footprint": "", "desc": "输入极性反接保护"},
                {"ref": "C1", "name": "输入滤波电容", "value": "100uF", "qty": 1,
                 "symbol": "Device:C", "footprint": "", "desc": "输入平滑滤波"},
                {"ref": "U1", "name": "5V稳压器", "value": "LM1117T-5.0", "qty": 1,
                 "symbol": "Regulator_Linear:LM1117T-5.0", "footprint": "", "desc": "给负压电荷泵供电"},
                {"ref": "C2", "name": "5V滤波电容", "value": "10uF", "qty": 1,
                 "symbol": "Device:C", "footprint": "", "desc": "+5V 去耦"},
                {"ref": "U2", "name": "负压电荷泵", "value": "ICL7660", "qty": 1,
                 "symbol": "Regulator_SwitchedCapacitor:ICL7660", "footprint": "", "desc": "产生负电压供 ADJ 下拉"},
                {"ref": "C3", "name": "泵电容", "value": "10uF", "qty": 1,
                 "symbol": "Device:C", "footprint": "", "desc": "电荷泵飞跨电容"},
                {"ref": "C4", "name": "负压滤波电容", "value": "10uF", "qty": 1,
                 "symbol": "Device:C", "footprint": "", "desc": "负电压滤波"},
                {"ref": "U3", "name": "可调线性稳压器", "value": "LM317", "qty": 1,
                 "symbol": "Regulator_Linear:LM317_TO-220", "footprint": "", "desc": "主稳压 1.5A，输出 0~5V 可调"},
                {"ref": "R1", "name": "反馈电阻", "value": "240", "qty": 1,
                 "symbol": "Device:R", "footprint": "", "desc": "LM317 Vout→ADJ 基准电阻"},
                {"ref": "RP1", "name": "调压电位器", "value": "2k", "qty": 1,
                 "symbol": "Device:R_Potentiometer", "footprint": "", "desc": "调节输出电压 0~5V"},
                {"ref": "C5", "name": "输出滤波电容", "value": "10uF", "qty": 1,
                 "symbol": "Device:C", "footprint": "", "desc": "输出滤波/稳定性"},
                {"ref": "J2", "name": "输出端子", "value": "0-5V", "qty": 1,
                 "symbol": "Connector:Conn_01x02_Pin", "footprint": "", "desc": "可调电压输出 1A"},
            ],
        },
        "netlist": {
            "nets": [
                {"name": "VIN_12V", "pins": ["J1:1", "D1:2"]},
                {"name": "VIN", "pins": ["D1:1", "C1:1", "U1:3", "U3:3"]},
                {"name": "GND", "pins": ["J1:2", "C1:2", "U1:1", "U2:3", "C2:2", "C4:2", "C5:2", "J2:2"]},
                {"name": "VCC_5V", "pins": ["U1:2", "C2:1", "U2:8"]},
                {"name": "CAP_POS", "pins": ["U2:2", "C3:1"]},
                {"name": "CAP_NEG", "pins": ["U2:4", "C3:2"]},
                # ICL7660 的 6 脚是 LV（低电压操作脚）：datasheet 规定供电 <3.5V 时接 GND、
                # ≥5V 时必须悬空(leave open)。这里供电是 LM1117 给的 +5V，故 LV 必须悬空，
                # 绝不能接到 VOUT(VNEG)——否则内部稳压器被旁路、工作点不确定甚至闩锁。
                # 不在网表里的引脚由 kicad_writer 自动打 no_connect（×），正是 leave open。
                {"name": "VNEG", "pins": ["U2:5", "C4:1", "RP1:2", "RP1:3"]},
                {"name": "VOUT", "pins": ["U3:2", "R1:1", "C5:1", "J2:1"]},
                {"name": "ADJ", "pins": ["U3:1", "R1:2", "RP1:1"]},
            ]
        },
    }


ADJUSTABLE_PSU = _adj_psu()

_BOARDS = {
    "8051": MINIMAL_8051,
    "8051最小系统": MINIMAL_8051,
    "8051 最小系统": MINIMAL_8051,
    "8051最小系统板": MINIMAL_8051,
    "at89c51": MINIMAL_8051,
    "at89s51": MINIMAL_8051,
    "arduino": ARDUINO_MINIMAL,
    "arduino 最小系统": ARDUINO_MINIMAL,
    "arduino 最小系统板": ARDUINO_MINIMAL,
    "atmega328": ARDUINO_MINIMAL,
    "最小系统板": ARDUINO_MINIMAL,
}

# 可调稳压电源需要「可调」+ 电源/稳压语义同时出现，单个词太宽泛（如「可调电阻」）。
_ADJ_PSU_REQUIRE = ("可调", "可调稳压", "连续可调", "0-5v", "0~5v", "0至5v")
_ADJ_PSU_SUPPLY = ("稳压电源", "稳压", "直流电源", "电源模块", "电源电路", "直流稳压")


def lookup_board(prompt: str):
    """按关键词匹配内置板卡；匹配不到返回 None（此时应使用 LLM）。"""
    p = prompt.lower()
    for k, v in _BOARDS.items():
        if k in p:
            return v
    # 可调稳压电源：可调语义 AND 电源/稳压语义
    if any(k in p for k in _ADJ_PSU_REQUIRE) and any(k in p for k in _ADJ_PSU_SUPPLY):
        return ADJUSTABLE_PSU
    return None
