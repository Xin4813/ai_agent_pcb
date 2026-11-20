"""电气安全启发式检查（不阻断生成，只给出人工核对提示）。

KiCad 的 ERC 只检查「连接规则」（电源脚是否被驱动、引脚冲突、悬空等），
完全不看电气参数：电压、耐压、极性、功率。所以把 5V 稳压器换成 3.3V、
把极性电容装反这类「会让实物烧板」的改动，ERC 照样 0 违规。
本模块做一层轻量启发式检查，在网页上给出醒目的黄色警告。

设计原则：
- 只报「高置信度」的问题，宁可漏报也不要误报刷屏；
- 不修改任何 BOM/网表，只返回警告文本列表；
- 可被 Agent 在 design()/revise() 后调用，写入 state["warnings"]。
"""
from __future__ import annotations
import re


# 已知器件家族的典型供电电压（数字部分）。用于「稳压器输出 vs 芯片需求」比对。
# 只列高置信度的常见型号，避免误报。
_CHIP_VCC = [
    (r"atmega328|atmega8|atmega16|atmega32", 5.0),
    (r"stm32f1|stm32f4|stm32f0", 3.3),
    (r"esp32|esp8266", 3.3),
    (r"74hc", 5.0),
    (r"74lvc", 3.3),
    (r"ne555", 5.0),   # 典型 5V，范围宽，仅弱提示
]

# 稳压器型号 → 输出电压。覆盖常见 LM1117/AMS1117/78xx/AMS1117 系列。
_REG_VOUT = re.compile(
    r"(?:lm|ams)?1117(?:t)?[-_]?(\d+(?:\.\d+)?)\b"
    r"|78(\d{2})\b"
    r"|ldo[-_]?(\d+(?:\.\d+)?)v?\b", re.I)


def _regulator_output(item: dict) -> float | None:
    """从稳压器元件的 value/symbol 解析输出电压（伏）。"""
    text = f"{item.get('value','')} {item.get('symbol','')}"
    m = _REG_VOUT.search(text)
    if not m:
        return None
    if m.group(1):            # 1117-3.3 / LM1117T-5.0
        return float(m.group(1))
    if m.group(2):            # 7805 / 7812
        return float(m.group(2)) / 100.0
    if m.group(3):            # ldo-3.3v
        return float(m.group(3))
    return None


def _chip_vcc(item: dict) -> float | None:
    """从芯片型号识别典型供电电压。"""
    text = f"{item.get('value','')} {item.get('symbol','')}".lower()
    for pat, v in _CHIP_VCC:
        if re.search(pat, text):
            return v
    return None


def _is_regulator(item: dict) -> bool:
    sym = item.get("symbol", "").lower()
    return "regulator" in sym or "ldo" in sym or "7805" in sym or "1117" in sym


def _polarized_cap(item: dict) -> bool:
    """判断是否极性电容（钽电容/电解电容）。极性接反会炸。"""
    sym = item.get("symbol", "").lower()
    name = f"{item.get('value','')} {item.get('name','')}".lower()
    return ("cp1" in sym or "polar" in sym or "electrolytic" in sym
            or "钽" in name or "电解" in name or "polar" in name)


def check_electrical(bom: dict, netlist: dict) -> list[str]:
    """返回电气风险警告列表（可能为空）。不修改入参。"""
    warnings: list[str] = []
    if not bom or not netlist:
        return warnings
    items = bom.get("items", [])
    nets = netlist.get("nets", [])

    # 位号 → 元件
    by_ref = {it.get("ref", ""): it for it in items}

    # 每条网络里出现的引脚（ref, pin）
    net_pins: dict[str, set[tuple[str, str]]] = {}
    for net in nets:
        name = net.get("name", "")
        pins = set()
        for p in net.get("pins", []):
            if ":" in p:
                r, _, n = p.partition(":")
                pins.add((r, n))
        net_pins[name] = pins

    # 1) 稳压器输出电压 vs 同网芯片供电需求
    for reg in items:
        if not _is_regulator(reg):
            continue
        vout = _regulator_output(reg)
        if vout is None:
            continue
        # 找稳压器输出脚所在的网络（假设电源输出脚接在某网上）——遍历所有网络，
        # 找出同时含该稳压器引脚与某芯片的网络
        reg_ref = reg.get("ref", "")
        for net_name, pins in net_pins.items():
            reg_pins = [n for r, n in pins if r == reg_ref]
            if not reg_pins:
                continue
            for r, _n in pins:
                if r == reg_ref or r not in by_ref:
                    continue
                chip = by_ref[r]
                need = _chip_vcc(chip)
                if need is None:
                    continue
                if abs(vout - need) >= 1.0:   # 差值 >= 1V 才算高风险
                    warnings.append(
                        f"⚠️ 供电电压可能不匹配：{reg_ref} 输出约 {vout:g}V，"
                        f"接到 {r}（{chip.get('value','')}，典型 {need:g}V）——"
                        f"可能导致芯片不工作或损坏，请人工核对。"
                    )
                    break   # 每对稳压器-芯片只报一次

    # 2) 极性电容疑似接反：电容 1 脚通常为正、2 脚为负/GND。
    #    若极性电容的 1 脚落在含 GND 的网络、2 脚落在非 GND 电源网 → 疑似反接。
    for it in items:
        if not _polarized_cap(it):
            continue
        ref = it.get("ref", "")
        pin1_net = pin2_net = None
        for net_name, pins in net_pins.items():
            if (ref, "1") in pins:
                pin1_net = net_name
            if (ref, "2") in pins:
                pin2_net = net_name
        gnd_re = re.compile(r"gnd|vss|地", re.I)
        if pin1_net and gnd_re.search(pin1_net) and pin2_net and not gnd_re.search(pin2_net):
            warnings.append(
                f"⚠️ 极性电容 {ref} 疑似反接：1 脚（通常为正）接在 {pin1_net}，"
                f"2 脚接在 {pin2_net}——装反可能炸裂，请核对极性。"
            )

    # 去重
    seen, uniq = set(), []
    for w in warnings:
        if w not in seen:
            seen.add(w)
            uniq.append(w)
    return uniq
