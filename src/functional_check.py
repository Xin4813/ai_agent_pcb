"""生成后功能性自检（低误报）——ERC / 视觉 / safety 之外的最后一道关。

设计目标：让网页每次生成都能自己回答「这次电路功能上对吗」，不再靠用户
逐张截图、人工发现新问题。只报「物理上一定错」的硬不变量，刻意过滤
芯片 NC/Modem/USB ID 等正常不接的脚，避免像早期 audit_functional 那样
报一堆误报反而失去意义。

返回结构化结果：{"ok": bool, "issues": [str], "checked": int}
"""
from __future__ import annotations
import re
from .safety import _pins, _GND_RE

_SUPPLY_RE = re.compile(r"^(vcc|vdd|vbus|\+?\d+(?:\.\d+)?v|3v3|5v|vin)", re.I)
_VOLTAGE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*v", re.I)
_ICL7660_RE = re.compile(r"icl7660", re.I)


def _net_voltage(netname: str, net_pins: dict, by_ref: dict, symbol_lib=None) -> float | None:
    """从网名或网内稳压器型号推断该网的标称电压（V）。推断不出返回 None。"""
    m = _VOLTAGE_RE.search(netname or "")
    if m:
        return float(m.group(1))
    if symbol_lib:
        for ref, _pin in net_pins.get(netname, ()):
            it = by_ref.get(ref, {})
            if "regulator" not in str(it.get("symbol", "")).lower():
                continue
            m = _VOLTAGE_RE.search(str(it.get("value", "")))
            if m:
                return float(m.group(1))
    return None


def _check_icl7660_lv(items: list, by_ref: dict, pin_net: dict, net_pins: dict,
                      symbol_lib=None) -> tuple[list[str], int]:
    """ICL7660 数据手册硬规则：LV 脚接法取决于 V+ 电压。

    V+ ≥ 5V 时 LV 必须悬空；接到 VOUT 会旁路内部稳压器、造成工作点漂移甚至闩锁。
    V+ ≤ 3.5V 时 LV 必须接 GND；悬空则低压模式失效。
    ERC 只看电气规则、视觉只看几何，都查不出这类「连上了但芯片不能正常工作」。
    """
    issues: list[str] = []
    checked = 0
    for it in items:
        ident = " ".join(str(it.get(k, "")) for k in ("value", "name", "symbol"))
        if not _ICL7660_RE.search(ident):
            continue
        checked += 1
        ref = it.get("ref", "")
        names: dict = {}
        if symbol_lib:
            try:
                names = symbol_lib.pin_names(it.get("symbol", ""))
            except Exception:
                names = {}
        lv = [str(n) for n, nm in names.items() if str(nm).strip().upper() == "LV"] or ["6"]
        vp = [str(n) for n, nm in names.items()
              if str(nm).strip().upper() in ("V+", "VCC", "VDD")] or ["8"]
        vplus_net = pin_net.get((ref, vp[0]))
        lv_net = pin_net.get((ref, lv[0]))
        if not vplus_net:
            continue                      # V+ 本身没接，由电源脚悬空规则负责
        volt = _net_voltage(vplus_net, net_pins, by_ref, symbol_lib)
        if volt is None or lv_net is None:
            continue                      # 电压不可判定，或 LV 已正确悬空
        if volt >= 4.5:
            issues.append(
                f"{ref}（ICL7660）供电 {vplus_net}={volt:g}V，数据手册要求 LV({lv[0]}) 悬空，"
                f"当前却接到 {lv_net}（会旁路内部稳压器）")
        elif volt <= 3.5 and not _GND_RE.search(lv_net):
            issues.append(
                f"{ref}（ICL7660）供电 {vplus_net}={volt:g}V 为低压模式，"
                f"LV({lv[0]}) 必须接 GND，当前接到 {lv_net}")
    return issues, checked


# 这些脚悬空是正常的，不算功能性错误。
_NORMAL_FLOAT = re.compile(
    r"^(nc|id|suspend|wake|cts|rts|dtr|dsr|dcd|ri|test|reserved|"
    r"n\.c\.?|not\s*connected)", re.I)


def _is_normal_float_pin(pin_name: str) -> bool:
    s = str(pin_name).strip()
    # KiCad 反转信号写成 ~{DSR} / ~RI，先剥掉 ~ 与花括号再判定
    s = s.strip("~").strip("{}").strip()
    if s.upper().startswith("PIN_"):
        return True          # 连接器通用脚名（如 USB ID=Pin_4），允许不接
    return bool(_NORMAL_FLOAT.search(s))


def _check_connector_function(items: list, by_ref: dict, pin_net: dict,
                              net_pins: dict, symbol_lib=None) -> tuple[list[str], int]:
    """连接器层面的两类硬错误（ERC / 视觉 / 安全都查不出）：

    1) USB 连接器接了 D± 却漏接 GND 脚——USB 无回流地，转串口完全不可用；
    2) 网名含 SWDIO/SWCLK 却只挂 MCU 一个脚、没有引到任何排针——调试器无处接。
    判据只看网名语义 / 引脚语义与连接关系，不绑定位号。
    """
    issues: list[str] = []
    checked = 0
    # 1) USB 口 GND
    for it in items:
        sym = str(it.get("symbol", "")).lower()
        if not ("conn_01x05" in sym or "usb_micro" in sym or "usb_type" in sym):
            continue
        ref = it.get("ref", "")
        names = [pin_net.get((ref, str(n)), "") for n in range(1, 6)]
        if not any(re.search(r"usb_d[+-]|(^|_)d[+-]$", n, re.I) for n in names):
            continue
        checked += 1
        # GND 引脚号：优先符号引脚名，回退 Micro-B 的 5 脚
        gnd_pin = None
        if symbol_lib:
            try:
                for num, nm in symbol_lib.pin_names(it.get("symbol", "")).items():
                    if re.search(r"gnd|ground|vss", str(nm), re.I):
                        gnd_pin = str(num)
                        break
            except Exception:
                pass
        if gnd_pin is None:
            gnd_pin = "5"
        gnd_net = pin_net.get((ref, gnd_pin))
        if gnd_net is None:
            issues.append(f"USB 连接器 {ref} 的 GND 脚 {gnd_pin} 悬空——"
                          f"USB 无回流地，转串口/供电回路不成立")
        elif not _GND_RE.search(gnd_net):
            issues.append(f"USB 连接器 {ref} 的 GND 脚 {gnd_pin} 接在 {gnd_net} 而非 GND")
    # 2) SWD 信号未引出
    swd_nets = {
        "SWDIO": [n for n in net_pins if re.search(r"swdio|swdat|jtms", n, re.I)],
        "SWCLK": [n for n in net_pins if re.search(r"swclk|swck|jtc", n, re.I)],
    }
    for label, nets in swd_nets.items():
        for n in nets:
            members = net_pins.get(n, set())
            checked += 1
            if len(members) <= 1:
                issues.append(f"{n}（{label}）只挂在 {sorted(members)} 一个脚上、"
                              f"未引出到调试排针——调试器无法连接")
    return issues, checked


def check_functional(bom: dict, netlist: dict, symbol_lib=None) -> dict:
    """检查真正的功能性硬错误，返回结构化结果。"""
    issues: list[str] = []
    checked = 0
    items = bom.get("items", [])
    nets = netlist.get("nets", [])
    if not items or not nets:
        return {"ok": True, "issues": [], "checked": 0}

    by_ref = {it.get("ref", ""): it for it in items}
    pin_net: dict[tuple[str, str], str] = {}
    net_pins: dict[str, set[tuple[str, str]]] = {}
    for net in nets:
        name = net.get("name", "")
        members: set[tuple[str, str]] = set()
        for p in net.get("pins", []):
            if ":" in p:
                r, _, n = p.partition(":")
                members.add((r, n))
                pin_net[(r, n)] = name
        net_pins[name] = members

    # 1) 同一引脚出现在两个网络（ERC 会报，但闭环修订后可能引入）
    seen: dict[tuple[str, str], str] = {}
    for net in nets:
        for p in net.get("pins", []):
            if ":" not in p:
                continue
            r, _, n = p.partition(":")
            key = (r, n)
            if key in seen and seen[key] != net["name"]:
                issues.append(f"{p} 同时出现在 {seen[key]} 与 {net['name']}")
            seen[key] = net["name"]

    # 2) 无源器件（R/C/LED/晶振/电感）每个脚都必须接网——但排除 NC 脚。
    #    连接器不算：排针/USB 的针脚按设计本就可悬空（如 USB ID、闲置 GPIO）。
    for it in items:
        sym = it.get("symbol", "").lower()
        # 精确前缀，避免 Interface_USB:CH340G / Regulator_Linear 被 ":c"/":l" 误匹配
        if not sym.startswith(("device:r", "device:c", "device:led",
                               "device:crystal", "device:l")):
            continue
        if symbol_lib:
            try:
                pmap = symbol_lib.pin_names(it.get("symbol", ""))
            except Exception:
                pmap = {}
        else:
            pmap = {}
        nums = [str(n) for n in pmap.keys()] if pmap else ["1", "2"]
        for n in nums:
            nm = str(pmap.get(int(n), pmap.get(n, ""))) if pmap else ""
            if _is_normal_float_pin(nm):
                continue
            if (it["ref"], n) not in pin_net:
                issues.append(f"{it['ref']}（{it.get('value','')}）的 {n}({nm or '脚'}) 悬空")
                continue
            # 引脚虽在某个网里，但该网只有它自己一个成员——这是一根没有
            # 连接到任何其它器件的线头（如断头限流电阻接在孤立 "LED" 网）。
            # ERC/视觉都看不出：引脚"已连接"，但电路里它不起作用。
            lone = net_pins.get(pin_net[(it["ref"], n)], set())
            if len(lone) == 1:
                issues.append(
                    f"{it['ref']}（{it.get('value','')}）的 {n} 脚接在孤立网 "
                    f"{pin_net[(it['ref'], n)]}（仅此一脚，未连任何器件）")
        checked += 1

    # 3) 有源芯片的电源/地脚必须接网（排除 NC 脚与明确标注不接的脚）
    for it in items:
        sym = it.get("symbol", "").lower()
        if not any(k in sym for k in ("mcu_", "regulator", "interface", "rf_module")):
            continue
        if symbol_lib:
            try:
                names = symbol_lib.pin_names(it.get("symbol", ""))
            except Exception:
                names = {}
        else:
            names = {}
        for num, nm in names.items():
            nm_u = str(nm).upper()
            if nm_u in ("VCC", "VDD", "GND", "VSS", "VI", "VIN", "VBUS"):
                if (it["ref"], str(num)) not in pin_net:
                    issues.append(f"{it['ref']} 的 {nm_u}({num}) 供电脚悬空")
        checked += 1

    # 4) 稳压器输入/输出不能同网
    for it in items:
        if "regulator" not in it.get("symbol", "").lower() or not symbol_lib:
            continue
        try:
            names = symbol_lib.pin_names(it.get("symbol", ""))
        except Exception:
            names = {}
        vi = [str(n) for n, nm in names.items() if str(nm) in ("VI", "VIN")]
        vo = [str(n) for n, nm in names.items() if str(nm) in ("VO", "VOUT")]
        if vi and vo:
            ni = {pin_net.get((it["ref"], p)) for p in vi}
            no = {pin_net.get((it["ref"], p)) for p in vo}
            if ni & no and None not in ni | no:
                issues.append(f"稳压器 {it['ref']} 输入/输出脚同网 {ni & no}，被短接")
        checked += 1

    # 4b) 数据手册级硬规则：ICL7660 的 LV 脚接法随 V+ 电压变化。
    #     5V 供电时必须悬空，接到 VOUT 不为 ERC/视觉/safety 所见，却会让芯片失效。
    lv_issues, lv_checked = _check_icl7660_lv(items, by_ref, pin_net, net_pins, symbol_lib)
    issues.extend(lv_issues)
    checked += lv_checked

    # 5) LED 阴阳极不能同网（短路）
    leds: list[dict] = []
    for it in items:
        if "led" not in it.get("symbol", "").lower():
            continue
        a = _pins(it, symbol_lib, ("A",)).get("A", ["2"])
        k = _pins(it, symbol_lib, ("K",)).get("K", ["1"])
        an = pin_net.get((it["ref"], a[0])) if a else None
        kn = pin_net.get((it["ref"], k[0])) if k else None
        if an and kn and an == kn:
            issues.append(f"LED {it['ref']} 阳极/阴极同网 {an}，被短路")
        leds.append((it, a, k, an, kn))
        checked += 1

    # 6) LED 回路必须有驱动源：从阳极沿串联电阻 BFS，必须能到达电源网
    #    （VCC/3V3/5V…）或有源芯片 GPIO 网；若只能走到 GND 或死胡同，
    #    则整条 LED 支路两端都落在 GND，灯永远不会亮。
    #    ERC 只看连接规则、视觉只看几何、safety 只看电压极性，四检全绿也
    #    查不出这种「接了线但不工作」的死回路——这是 functional 独有的盲区。
    def is_res(ref: str) -> bool:
        return by_ref.get(ref, {}).get("symbol", "").lower().startswith("device:r")

    def pin_semantic(ref: str, num: str) -> str:
        """引脚语义名（GND/VSS/PC13/VCC…），无符号库时返回空串。"""
        if not symbol_lib:
            return ""
        it = by_ref.get(ref, {})
        if not it.get("symbol"):
            return ""
        try:
            names = symbol_lib.pin_names(it["symbol"])
            return str(names.get(int(num), names.get(num, ""))).upper()
        except Exception:
            return ""

    def is_true_gnd(netname: str) -> bool:
        """真地网：网内成员含器件的 GND/VSS 脚。

        不能只看网名——LLM 常把 GPIO 驱动网命名成 LED2_GND，
        但其成员 U1:2 其实是 PC13，是真 GPIO，不是地。
        """
        for r, num in net_pins.get(netname, ()):
            sem = pin_semantic(r, num)
            if sem in ("GND", "VSS", "VSSA", "AGND"):
                return True
        # 无符号库时退化为网名判断（兜底）
        return bool(symbol_lib is None and _GND_RE.search(netname))

    def driven(netname: str) -> bool:
        """该网是否本身就是驱动源：电源网，或含非无源器件脚(GPIO/连接器/芯片)。

        真地网是电流汇、不是驱动源——GND 网里必然挂着 MCU 的地脚，若把它
        算作驱动，任何「LED 阳极经电阻回到 GND」的死回路都会被误判为正常。
        """
        if is_true_gnd(netname):
            return False
        if _SUPPLY_RE.match(netname):
            return True
        for r, _n in net_pins.get(netname, ()):
            sym = by_ref.get(r, {}).get("symbol", "").lower()
            if sym and not sym.startswith(("device:r", "device:c", "device:l",
                                           "device:led", "device:crystal")):
                # 有源器件的脚：若恰好是 GND/VSS 脚则不算，否则是驱动(GPIO/电源脚)
                if pin_semantic(r, _n) in ("GND", "VSS", "VSSA", "AGND"):
                    continue
                return True
        return False

    # 6b) 网表功能覆盖：ERC 看不出、但功能上一定错的漏接。
    #     - 单脚网络（ICSP 只写排针端、AREF 悬空）
    #     - 芯片整组 IO 全未接（如 Arduino 的 A0-A5/PC0-PC5 整组丢失），
    #       而同芯片其它端口已引出。这是「五维全绿却少一路信号」的盲区。
    #     复用 netlist_generator 的同一套规则，保证生成回灌与交付自检口径一致。
    try:
        from .netlist_generator import io_coverage_issues
        for msg in io_coverage_issues(bom, netlist, symbol_lib):
            issues.append(msg)
            checked += 1
    except Exception:
        pass

    # 6c) 连接器功能硬错误：USB 口漏 GND、SWD 信号未引出排针
    try:
        ci, cc = _check_connector_function(items, by_ref, pin_net, net_pins, symbol_lib)
        for msg in ci:
            issues.append(msg)
            checked += cc
    except Exception:
        pass

    for it, a, k, an, kn in leds:
        if not an or not kn:
            continue
        # 沿电阻从阳极出发做 BFS；真地网是汇点，走到它即停止，不再往下传播
        seen = {an}
        stack = [an]
        reached = False
        while stack:
            cur = stack.pop()
            if driven(cur):
                reached = True
                break
            if is_true_gnd(cur):
                continue
            for r, num in net_pins.get(cur, ()):
                if not is_res(r):
                    continue
                other = "2" if num == "1" else "1"
                on = pin_net.get((r, other))
                if on and on not in seen:
                    seen.add(on)
                    stack.append(on)
        if not reached:
            issues.append(
                f"LED {it['ref']} 的阳极({an})经限流电阻只能回到 GND/悬空，"
                f"走不到电源或 GPIO 驱动源——回路无驱动，灯不会亮")
        checked += 1

    return {"ok": not issues, "issues": issues, "checked": checked}
