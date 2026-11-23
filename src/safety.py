"""电气安全启发式检查（不阻断生成，只给出人工核对提示）。

KiCad 的 ERC 只检查「连接规则」（电源脚是否被驱动、引脚冲突、悬空等），
完全不看电气参数：电压、耐压、极性、功率。所以把 5V 稳压器换成 3.3V、
把极性电容装反这类「会让实物烧板」的改动，ERC 照样 0 违规。
本模块做一层轻量启发式检查，在网页上给出醒目的黄色警告。

设计原则：
- 只报「高置信度」的问题，宁可漏报也不要误报刷屏；
- 不修改任何 BOM/网表，只返回警告文本列表；
- 可被 Agent 在 design()/revise() 后调用，写入 state["warnings"]。
- 网表由 circuit_fix 初次生成时已修正过；这里主要防止用户后续「修改元件」
  把正确电路改坏（ERC 依旧看不出来）。
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

# 无符号库时的引脚号回退表（仅覆盖本作业高置信度型号）。
_FALLBACK_PINS = {
    "ch340": {"VCC": ["16"], "GND": ["1"], "UD+": ["5"], "UD-": ["6"],
              "XI": ["7"], "XO": ["8"], "V3": ["4"]},
    "stm32f1": {"VDD": ["24", "36", "48"], "VBAT": ["1"], "VDDA": ["9"],
                "VSSA": ["8"], "BOOT0": ["44"], "NRST": ["7"],
                "VSS": ["23", "35", "47"]},
    "led": {"A": ["2"], "K": ["1"]},
    "1117": {"VO": ["2"], "VI": ["3"], "GND": ["1"]},
    "usb5": {"VBUS": ["1"], "D-": ["2"], "D+": ["3"], "ID": ["4"], "GND": ["5"]},
}

_SUPPLY_NET_RE = re.compile(
    r"^(vcc|vdd|\+?\d+(?:\.\d+)?v|3v3|5v|vbus)", re.I)
_GND_RE = re.compile(r"gnd|vss|地", re.I)


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


def _is_gpio_driver(item: dict) -> bool:
    """是否可能驱动 GPIO 的主控/SoC——用于 LED 阳极直挂 GPIO 的告警。

    早期只认 "mcu_"/"stm32f1" 符号，漏掉 RF_Module:ESP32-WROOM-32，
    导致 ESP32 的 LED 直挂 IO 口这类会烧IO的功能错误三检全绿。
    与 circuit_fix._is_mcu_item 保持同一判据。
    """
    if not item:
        return False
    sym = item.get("symbol", "").lower()
    return (_chip_vcc(item) is not None
            or "mcu_" in sym
            or "rf_module" in sym
            or "stm32f1" in sym)


def _is_regulator(item: dict) -> bool:
    sym = item.get("symbol", "").lower()
    return "regulator" in sym or "ldo" in sym or "7805" in sym or "1117" in sym


def _polarized_cap(item: dict) -> bool:
    """判断是否极性电容（钽电容/电解电容）。极性接反会炸。"""
    sym = item.get("symbol", "").lower()
    name = f"{item.get('value','')} {item.get('name','')}".lower()
    return ("cp1" in sym or "polar" in sym or "electrolytic" in sym
            or "钽" in name or "电解" in name or "polar" in name)


def _pins(item: dict, symbol_lib, wanted) -> dict[str, list[str]]:
    """返回 {引脚名: [引脚号]}。优先用真实 KiCad 符号，回退到已知型号表。"""
    want = tuple(wanted)
    if symbol_lib is not None:
        try:
            names = symbol_lib.pin_names(item.get("symbol", ""))
            out: dict[str, list[str]] = {}
            for w in want:
                ps = [str(n) for n, nm in names.items()
                      if (nm or "").upper() == w.upper()]
                if ps:
                    out[w] = ps
            if out:
                return out
        except Exception:
            pass
    text = f"{item.get('value','')} {item.get('symbol','')}".lower()
    if "ch340" in text:
        table = _FALLBACK_PINS["ch340"]
    elif "stm32f1" in text or "stm32f103" in text:
        table = _FALLBACK_PINS["stm32f1"]
    elif "led" in item.get("symbol", "").lower():
        table = _FALLBACK_PINS["led"]
    elif "1117" in text:
        table = _FALLBACK_PINS["1117"]
    elif "conn_01x05" in text or "usb_micro" in text:
        table = _FALLBACK_PINS["usb5"]
    else:
        return {}
    return {w: table[w] for w in want if w in table}


def _net_of(pin_net: dict, ref: str, num: str) -> str | None:
    return pin_net.get((ref, num))


def check_electrical(bom: dict, netlist: dict, symbol_lib=None) -> list[str]:
    """返回电气风险警告列表（可能为空）。不修改入参。

    symbol_lib 可选：提供时按真实符号引脚名定位，避免硬编码型号差异。
    """
    warnings: list[str] = []
    if not bom or not netlist:
        return warnings
    items = bom.get("items", [])
    nets = netlist.get("nets", [])

    # 位号 → 元件
    by_ref = {it.get("ref", ""): it for it in items}

    # 每条网络里出现的引脚（ref, pin），以及「引脚 → 网络名」反查
    net_pins: dict[str, set[tuple[str, str]]] = {}
    pin_net: dict[tuple[str, str], str] = {}
    for net in nets:
        name = net.get("name", "")
        pins: set[tuple[str, str]] = set()
        for p in net.get("pins", []):
            if ":" in p:
                r, _, n = p.partition(":")
                pins.add((r, n))
                pin_net[(r, n)] = name
        net_pins[name] = pins

    def _add(msg: str):
        warnings.append(msg)

    # 1) 稳压器输出电压 vs 同网芯片供电需求
    for reg in items:
        if not _is_regulator(reg):
            continue
        vout = _regulator_output(reg)
        if vout is None:
            continue
        reg_ref = reg.get("ref", "")
        out_pins = set(_pins(reg, symbol_lib, ("VO", "VOUT")).get("VO", []))
        for net_name, pins in net_pins.items():
            reg_pins = [n for r, n in pins if r == reg_ref]
            if not reg_pins:
                continue
            # 只把稳压器的输出脚视作驱动源；若模型没标引脚名则退回任意脚
            if out_pins and not (out_pins & set(reg_pins)):
                continue
            for r, _n in pins:
                if r == reg_ref or r not in by_ref:
                    continue
                chip = by_ref[r]
                need = _chip_vcc(chip)
                if need is None:
                    continue
                if abs(vout - need) >= 1.0:   # 差值 >= 1V 才算高风险
                    _add(
                        f"⚠️ 供电电压可能不匹配：{reg_ref} 输出约 {vout:g}V，"
                        f"接到 {r}（{chip.get('value','')}，典型 {need:g}V）——"
                        f"可能导致芯片不工作或损坏，请人工核对。"
                    )
                    break   # 每对稳压器-芯片只报一次

    # 2) 极性电容疑似接反：电容 1 脚通常为正、2 脚为负/GND。
    for it in items:
        if not _polarized_cap(it):
            continue
        ref = it.get("ref", "")
        pin1_net = _net_of(pin_net, ref, "1")
        pin2_net = _net_of(pin_net, ref, "2")
        if (pin1_net and _GND_RE.search(pin1_net)
                and pin2_net and not _GND_RE.search(pin2_net)):
            _add(
                f"⚠️ 极性电容 {ref} 疑似反接：1 脚（通常为正）接在 {pin1_net}，"
                f"2 脚接在 {pin2_net}——装反可能炸裂，请核对极性。"
            )

    # 3) 3.3V 芯片的 VDD/VBAT/VDDA 被接到 5V 网（USB VBUS 或 5V 稳压输出）
    hot_nets: set[str] = set()
    for it in items:
        text = f"{it.get('value','')} {it.get('symbol','')}".lower()
        vout = _regulator_output(it) if _is_regulator(it) else None
        if vout is not None and vout >= 4.5:
            for p in _pins(it, symbol_lib, ("VO", "VOUT")).get("VO", []):
                n = _net_of(pin_net, it["ref"], p)
                if n:
                    hot_nets.add(n)
        if "usb" in text and ("conn" in text or "micro" in text):
            for p in _pins(it, symbol_lib, ("VBUS",)).get("VBUS", []):
                n = _net_of(pin_net, it["ref"], p)
                if n:
                    hot_nets.add(n)
    if hot_nets:
        for it in items:
            need = _chip_vcc(it)
            if need is None or need > 3.6:
                continue
            hits: list[str] = []          # 同一芯片的多个 VDD 脚合并为一条
            hot_name = ""
            for pname in ("VDD", "VBAT", "VDDA"):
                for p in _pins(it, symbol_lib, (pname,)).get(pname, []):
                    n = _net_of(pin_net, it["ref"], p)
                    if n and n in hot_nets:
                        hits.append(f"{pname}({p})")
                        hot_name = hot_name or n
            if hits:
                _add(
                    f"⚠️ {it['ref']} 的供电脚 {', '.join(hits)} 接到 5V 网络 {hot_name}，"
                    f"但该芯片典型供电仅 {need:g}V——长期通电会烧坏，"
                    f"必须先经 3.3V 稳压器降压。"
                )

    # 4) LED 阳极直接接电源、没有串联限流电阻（或 LED 被短路）
    for it in items:
        sym = it.get("symbol", "").lower()
        if "led" not in sym:
            continue
        ps = _pins(it, symbol_lib, ("A", "K"))
        a_list, k_list = ps.get("A", []), ps.get("K", [])
        if not a_list or not k_list:
            continue
        ref = it["ref"]
        a_net = _net_of(pin_net, ref, a_list[0])
        k_net = _net_of(pin_net, ref, k_list[0])
        if a_net and k_net and a_net == k_net:
            _add(f"⚠️ LED {ref} 的阳极/阴极落在同一网络 {a_net}，LED 被短路。")
            continue
        if not a_net:
            continue
        # 关键：限流电阻必须与 LED 阳极「独占一条只有两者相连的网络」。
        # 只要阳极挂在一个电源网（如 VCC 上还挂着上拉电阻、电源脚），
        # 就说明 LED 是直接跨接电源，网里其它电阻并不与它串联。
        anode_members = net_pins.get(a_net, set())
        series_res = (
            len(anode_members) == 2
            and any(r != ref and by_ref.get(r, {}).get("symbol", "").lower().startswith("device:r")
                    for r, _n in anode_members)
        )
        if _SUPPLY_NET_RE.match(a_net) and not series_res:
            _add(
                f"⚠️ LED {ref} 阳极直接接在电源网 {a_net}，没有串联限流电阻——"
                f"LED 会过流烧毁，请串入 330Ω~1kΩ 电阻（GPIO→电阻→LED→GND）。"
            )
        # 4b) LED 阳极直接挂在含芯片 GPIO 的网络上，同样没有串联限流。
        #     电源网那条规则抓不到 GPIO 网，但 GPIO 直驱 LED 一样会烧 IO/LED。
        elif a_net and not series_res and any(
                r != ref and (r, _n) in anode_members
                and _is_gpio_driver(by_ref.get(r, {}))
                for r, _n in anode_members):
            _add(
                f"⚠️ LED {ref} 阳极直接挂在 {a_net} 的芯片 GPIO 上，没有串联限流电阻——"
                f"通电会烧 LED 或 IO 口，请串入 330Ω~1kΩ（GPIO→电阻→LED→GND）。"
            )

    # 5) USB D+/D− 与 USB 转串口芯片交叉接反
    uart = next((it for it in items if "ch340" in
                 f"{it.get('value','')} {it.get('symbol','')}".lower()), None)
    usb = next((it for it in items if
                "conn_01x05" in it.get("symbol", "").lower()
                or "usb_micro" in it.get("symbol", "").lower()), None)
    if uart and usb:
        for dname, usbname in (("UD+", "D+"), ("UD-", "D-")):
            pins = _pins(uart, symbol_lib, (dname,)).get(dname, [])
            want = _pins(usb, symbol_lib, (usbname,))
            want_pins = set(next(iter(want.values()), []))
            if not pins or not want_pins:
                continue
            n = _net_of(pin_net, uart["ref"], pins[0])
            if not n:
                _add(
                    f"⚠️ USB {usbname} 未接到 {uart['ref']} 的 {dname} 脚（{pins[0]}）——"
                    f"电脑将无法通过 USB 识别串口。"
                )
                continue
            usb_side = {num for r, num in net_pins.get(n, set()) if r == usb["ref"]}
            if not usb_side:
                _add(
                    f"⚠️ {uart['ref']} 的 {dname} 网 {n} 上没有 USB 连接器 {usb['ref']} 的引脚——"
                    f"USB 数据线悬空，电脑将无法识别串口。"
                )
            elif not (usb_side & want_pins):
                wrong = ", ".join(sorted(usb_side))
                right = ", ".join(sorted(want_pins))
                _add(
                    f"⚠️ USB {usbname} 接反：{uart['ref']}:{pins[0]} 所在网络 {n} "
                    f"接到 {usb['ref']} 的 {wrong} 脚，实际应为 {right} 脚——"
                    f"USB 通信会失败，请交叉对调。"
                )

    # 6) CH340G 的 XI/XO 之间必须有晶振本体（两个负载电容不算起振）
    if uart:
        xi = _pins(uart, symbol_lib, ("XI",)).get("XI", [])
        xo = _pins(uart, symbol_lib, ("XO",)).get("XO", [])
        xi_net = _net_of(pin_net, uart["ref"], xi[0]) if xi else None
        xo_net = _net_of(pin_net, uart["ref"], xo[0]) if xo else None
        has_xtal = False
        if xi_net and xo_net and xi_net != xo_net:
            for it in items:
                if "crystal" not in it.get("symbol", "").lower():
                    continue
                a = _net_of(pin_net, it["ref"], "1")
                b = _net_of(pin_net, it["ref"], "2")
                if a and b and {a, b} == {xi_net, xo_net}:
                    has_xtal = True
                    break
        if not has_xtal:
            _add(
                f"⚠️ {uart['ref']} 的 XI/XO 之间没有晶振本体（只接负载电容不算）——"
                f"USB 转串口芯片无法起振，电脑将识别不到串口。"
            )

    # 7) STM32 的 VBAT/VDDA/BOOT0 未接（BOOT0 还需下拉，否则启动模式不确定）
    for it in items:
        if not (_chip_vcc(it) is not None and "stm32" in
                f"{it.get('value','')} {it.get('symbol','')}".lower()):
            continue
        for pname in ("VBAT", "VDDA", "BOOT0"):
            pins = _pins(it, symbol_lib, (pname,)).get(pname, [])
            if not pins:
                continue
            n = _net_of(pin_net, it["ref"], pins[0])
            if n is None:
                extra = "（BOOT0 通常需 10kΩ 下拉到 GND）" if pname == "BOOT0" else ""
                _add(
                    f"⚠️ {it['ref']} 的 {pname} 脚（{pins[0]}）未连接{extra}——"
                    f"可能导致芯片不工作或启动模式不确定。"
                )
        # NRST 是低有效复位：上拉电阻必须接 3.3V，按键另一端接 GND。
        # 若电阻改接 GND，复位脚会被永久拉低，芯片一直处于复位态。
        for rp in _pins(it, symbol_lib, ("NRST",)).get("NRST", []):
            rn = _net_of(pin_net, it["ref"], rp)
            if not rn:
                continue
            for r, num in net_pins.get(rn, set()):
                rit = by_ref.get(r, {})
                if not rit.get("symbol", "").lower().startswith("device:r"):
                    continue
                other = "2" if num == "1" else "1"
                on = pin_net.get((r, other))
                if on and re.search(r"gnd|vss|ground|地", on, re.I):
                    _add(
                        f"⚠️ {it['ref']} 的 NRST 脚（{rp}）上拉电阻 {r} 接到了 GND——"
                        f"复位脚会被永久拉低，芯片无法运行；该电阻应接 3.3V，"
                        f"复位按键接 GND。"
                    )

    # 去重
    seen, uniq = set(), []
    for w in warnings:
        if w not in seen:
            seen.add(w)
            uniq.append(w)
    return uniq
