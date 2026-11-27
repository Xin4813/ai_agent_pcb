"""常见电路拓扑纠错（ERC 检查不到的功能性错误）。

LLM 生成的网表经过 _self_heal_netlist 后是「合法」的（位号/引脚真实、同脚不冲突），
ERC 也能 0 违规，但仍可能包含功能性错误：5V 直灌 3.3V 芯片、LED 无串联限流
电阻、USB D+/D− 交叉、CH340G 晶振悬空、STM32 电源脚未接。

本模块在**初次设计**时逐项检测并修正这些错误。每项独立触发——LLM 每次生成的
元件组合不同（例如有时用 CH340G、有时用 STM32 内部 USB），不能要求某个特定
元件组合齐全才执行。已正确的连接不会被改动；用户后续 revise() 改坏了则由
safety.py 给警告，这里不介入。

设计原则：
- 按 KiCad 符号的真实引脚名定位，不硬编码引脚号；
- 修完仍要过 validate_netlist（位号存在、引脚唯一、引脚号为整数）；
- 返回修正说明，供网页展示「已自动优化 N 处」。
"""
from __future__ import annotations
import re
from .validator import validate_netlist
from .safety import _pins, _regulator_output, _chip_vcc, _GND_RE

_SUPPLY_RE = re.compile(r"^(vcc|vdd|vbus|\+?\d+(?:\.\d+)?v|3v3|5v)", re.I)
_V33_NAME_RE = re.compile(r"3v3|\+3\.3|3\.3v", re.I)
_PORT_GROUP_RE = re.compile(r"^([A-Za-z]{1,2})(\d+)$")


def _find(items, *keys):
    for it in items:
        t = f"{it.get('value','')} {it.get('symbol','')}".lower()
        if all(k.lower() in t for k in keys):
            return it
    return None


def _p(item, lib, name):
    return _pins(item, lib, (name,)).get(name, [])


def _add_ref(items, prefix: str) -> str:
    nums = []
    for it in items:
        m = re.match(r"^" + re.escape(prefix) + r"(\d+)$", it["ref"])
        if m:
            nums.append(int(m.group(1)))
    return f"{prefix}{max(nums or [0]) + 1}"


class _Nets:
    def __init__(self, netlist: dict):
        self.nets = netlist.setdefault("nets", [])

    def find(self, pin: str):
        for net in self.nets:
            if pin in net["pins"]:
                return net
        return None

    def name(self, pin: str):
        net = self.find(pin)
        return net["name"] if net else None

    def members(self, name: str) -> set[str]:
        for net in self.nets:
            if net["name"] == name:
                return set(net["pins"])
        return set()

    def ensure(self, name: str):
        # LLM 或某些修复分支可能传入 None/空网名；统一在这里兜底，
        # 避免后续 re.search / .lower() 收到 None 抛 TypeError。
        if not isinstance(name, str) or not name.strip():
            taken = {n.get("name") for n in self.nets}
            i = 1
            name = f"NET_{i}"
            while name in taken:
                i += 1
                name = f"NET_{i}"
        for net in self.nets:
            if net["name"] == name:
                return net
        net = {"name": name, "pins": []}
        self.nets.append(net)
        return net

    def attach(self, pin: str, name: str):
        for net in self.nets:
            if pin in net["pins"]:
                net["pins"].remove(pin)
        self.ensure(name)["pins"].append(pin)

    def drop_empty(self):
        self.nets[:] = [n for n in self.nets if n.get("pins")]

    def clear_pin(self, pin: str):
        """把某个引脚从所有网络移除（删除元件时用）。"""
        for net in self.nets:
            if pin in net["pins"]:
                net["pins"].remove(pin)

    def remove_component(self, ref: str, pins):
        """从所有网络移除元件的全部引脚。"""
        for p in pins:
            self.clear_pin(p)


def _v33_net(ops, items, lib) -> str | None:
    """找现有的 3.3V 网络名（按网络名或稳压器输出引脚）。"""
    for net in ops.nets:
        if _V33_NAME_RE.search(net["name"]):
            return net["name"]
    for it in items:
        vout = _regulator_output(it)
        if vout is not None and abs(vout - 3.3) < 0.2:
            for p in _p(it, lib, "VO"):
                n = ops.name(f"{it['ref']}:{p}")
                if n:
                    return n
    return None


def _split_v33_from_hot(ops, items, lib, hot, fixes) -> str | None:
    """3.3V 稳压器输出若和 USB 5V 同网，拆出干净的 3.3V 网。

    LLM 有时把 LM1117 的 VO 接到 USB_VBUS，同时 VI 反接在另一条网。
    此时 `_v33_net` 会误把 USB_VBUS 当作 3.3V 网，后续把 MCU 的 VDD
    迁过去等于直接灌 5V。这里先分离输出网，保留 VBUS 连接器/5V 来源
    在旧网，把逻辑负载迁到新 +3V3。
    """
    v33 = _v33_net(ops, items, lib)
    regs = [it for it in items
            if (v := _regulator_output(it)) is not None and abs(v - 3.3) < 0.2]
    if not regs or not v33 or v33 not in hot:
        return v33
    base = "+3V3"
    name = base
    n = 2
    while any(net["name"] == name for net in ops.nets):
        name = f"{base}_{n}"
        n += 1
    old = v33
    by_ref = {it["ref"]: it for it in items}

    def pin_name(it, num):
        try:
            return str(lib.pin_names(it["symbol"]).get(int(num), "")).lower()
        except Exception:
            return ""

    moved = []
    for pin in list(ops.members(old)):
        ref, _, num = pin.partition(":")
        it = by_ref.get(ref)
        if not it or it in regs:
            continue
        pn = pin_name(it, num)
        sym = it.get("symbol", "").lower()
        # USB VBUS / 5V 来源脚留在旧网
        if ("vbus" in pn or re.search(r"\b5v\b", pn)
                or ("connector:" in sym or "conn_01x" in sym)
                and re.search(r"(5v|vbus)", old, re.I)):
            continue
        # GND 脚错进电源网时归位
        if _GND_RE.search(pn) or num in {str(p) for p in _p(it, lib, "GND")}:
            ops.attach(pin, _gnd_net(ops))
            moved.append(f"{pin}→GND")
            continue
        ops.attach(pin, name)
        moved.append(f"{pin}→{name}")
    # 电容两端同在一网会被短接，顺手把第二端归地
    for it in items:
        if not it.get("symbol", "").lower().startswith("device:c"):
            continue
        a, b = f"{it['ref']}:1", f"{it['ref']}:2"
        if ops.name(a) == name and ops.name(b) == name:
            ops.attach(b, _gnd_net(ops))
            moved.append(f"{b}→GND")
    for reg in regs:
        for p in _p(reg, lib, "VO"):
            pin = f"{reg['ref']}:{p}"
            if ops.name(pin) == old or not ops.name(pin):
                ops.attach(pin, name)
                moved.append(f"{pin}→{name}")
    fixes.append(f"3.3V 稳压器输出与 {old} 短接，已分离出 {name}：{', '.join(moved)}")
    return name


def _connector_vbus_pin(ops, it, lib):
    """按**实际网名**找 USB 连接器的 VBUS 脚。

    不能只信符号引脚名：LLM 常把 Micro-B 的 VBUS 画在 4 脚、GND 画在 1 脚，
    而 KiCad 符号把 1 脚命名为 VBUS。若只读符号名，会把 GND 当 5V 输入、
    同时漏掉真正的 USB_VBUS，导致 3.3V 拆分逻辑完全不触发。
    """
    try:
        nums = sorted(int(n) for n in lib.pin_names(it["symbol"]))
    except Exception:
        nums = list(range(1, 6))
    for num in nums:
        n = ops.name(f"{it['ref']}:{num}")
        if n and not _GND_RE.search(n) and re.search(r"(5v|vbus)", n, re.I):
            return num
    for num in _p(it, lib, "VBUS"):
        n = ops.name(f"{it['ref']}:{num}")
        if n and not _GND_RE.search(n):
            return num
    return None


def _hot_nets(ops, items, lib) -> set[str]:
    """收集高于 3.3V 的网络：USB VBUS、5V 稳压器输出、3.3V 稳压器输入端。

    旧实现只看 USB 连接器和 5V 稳压器输出。若 LLM 画了一个 3.3V 稳压器、
    却把 MCU 的 VDD 直接挂在它的**输入端**（网名还叫 VCC），hot 为空，
    `_fix_power` 直接跳过，5V 就会灌进 3.3V 芯片。这里把 3.3V 稳压器的
    VI 网也纳入 hot，让后续迁移动作能覆盖这种常见错接。
    """
    hot: set[str] = set()
    for it in items:
        t = f"{it.get('value','')} {it.get('symbol', '')}".lower()
        if "conn_01x05" in it.get("symbol", "").lower() or "usb_micro" in t:
            vbus = _connector_vbus_pin(ops, it, lib)
            n = ops.name(f"{it['ref']}:{vbus}") if vbus is not None else None
            if n and not _GND_RE.search(n):
                hot.add(n)
        vout = _regulator_output(it)
        if vout is None:
            continue
        if vout >= 4.5:
            for p in _p(it, lib, "VO"):
                n = ops.name(f"{it['ref']}:{p}")
                if n:
                    hot.add(n)
        elif abs(vout - 3.3) < 0.2:
            # 输入端才是待降级网络；输出端是 3.3V，不能当 hot。
            for p in _p(it, lib, "VI"):
                n = ops.name(f"{it['ref']}:{p}")
                if n and not _GND_RE.search(n):
                    hot.add(n)
    return hot


def _fix_vbus_on_3v3_output(items, ops, lib, fixes):
    """USB VBUS(5V) 若和 3.3V 稳压器输出同网，拆回稳压器输入网。

    `_split_v33_from_hot` 靠网名含 5v/vbus 判断「5V 来源脚」，但 LLM 常把
    VBUS 画进一条叫 +3V3 的网：此时 VBUS 会被当成普通 3.3V 负载一起迁移，
    结果 5V 仍与稳压器输出/芯片 VDD 短接，只是换了个网名。这里改按
    **引脚语义**（USB 连接器的 VBUS 脚）与稳压器 VO 是否同网判断，与网名无关。
    """
    regs = [it for it in items
            if (v := _regulator_output(it)) is not None and abs(v - 3.3) < 0.2]
    if not regs:
        return
    by_ref = {it["ref"]: it for it in items}
    for it in list(items):
        t = f"{it.get('value','')} {it.get('symbol','')}".lower()
        sym = it.get("symbol", "").lower()
        if not (("conn_01x05" in sym or "usb_micro" in t)
                and ("usb" in t or "conn" in sym)):
            continue
        # VBUS 脚：网名含 vbus/5v 优先，否则回退到符号 VBUS 引脚号（Micro-B=1）
        vbus = _connector_vbus_pin(ops, it, lib)
        if vbus is None:
            continue
        vpin = f"{it['ref']}:{vbus}"
        vnet = ops.name(vpin)
        if not vnet:
            continue
        # 该 USB 网里是否含某个 3.3V 稳压器的输出脚 → 5V 倒灌进 3.3V 域
        for reg in regs:
            vo = next(iter(_p(reg, lib, "VO")), "2")
            vi = next(iter(_p(reg, lib, "VI")), "3")
            vo_pin, vi_pin = f"{reg['ref']}:{vo}", f"{reg['ref']}:{vi}"
            if ops.name(vo_pin) != vnet:
                continue
            vi_net = ops.name(vi_pin)
            if not vi_net or vi_net == vnet or _GND_RE.search(vi_net):
                continue
            ops.attach(vpin, vi_net)
            fixes.append(f"{it['ref']} 的 VBUS 脚 {vbus} 原接在 3.3V 输出网 {vnet}，"
                         f"已改接稳压器输入网 {vi_net}，避免 5V 倒灌烧坏 3.3V 芯片")
            break


def _fix_power(items, ops, lib, fixes):
    """把 3.3V 芯片的供电脚从 5V 网迁到 3.3V 网，必要时新增稳压器。"""
    hot = _hot_nets(ops, items, lib)
    if not hot:
        return
    needy = []                       # (ref:pin, 芯片)
    for it in items:
        need = _chip_vcc(it)
        if need is None or need > 3.6:
            continue
        for name in ("VDD", "VBAT", "VDDA"):
            for p in _p(it, lib, name):
                pin = f"{it['ref']}:{p}"
                if ops.name(pin) in hot:
                    needy.append((pin, it))
    if not needy:
        return

    v33 = _split_v33_from_hot(ops, items, lib, hot, fixes)
    if v33 is None:
        reg = None
        for it in items:
            vout = _regulator_output(it)
            if vout is not None and abs(vout - 3.3) < 0.2:
                reg = it
                break
        if reg is None:
            ref = _add_ref(items, "U")
            reg = {"ref": ref, "name": "3.3V 稳压器 AMS1117-3.3",
                   "value": "AMS1117-3.3", "qty": 1,
                   "symbol": "Regulator_Linear:AMS1117-3.3", "footprint": ""}
            items.append(reg)
            fixes.append(f"新增 {ref}（AMS1117-3.3）把 USB 5V 降到 3.3V")
        vi = next(iter(_p(reg, lib, "VI")), "3")
        vo = next(iter(_p(reg, lib, "VO")), "2")
        rg = next(iter(_p(reg, lib, "GND")), "1")
        ops.attach(f"{reg['ref']}:{vi}", next(iter(hot)))
        v33 = "+3V3"
        ops.attach(f"{reg['ref']}:{vo}", v33)
        ops.attach(f"{reg['ref']}:{rg}", "GND")

    old = ops.name(needy[0][0])
    chip_refs = {it["ref"] for _, it in needy}
    moved_names = sorted({name for pin, _it in needy
                          for name in ("VDD", "VBAT", "VDDA")
                          if any(pin.endswith(":" + p)
                                 for p in _p(_it, lib, name))})
    for pin, _it in needy:
        ops.attach(pin, v33)
    # 把「一端接 5V、另一端接该 3.3V 芯片」的上拉电阻/去耦电容迁到 3.3V：
    # 例如 NRST 上拉若留在 5V 会向芯片 3.3V 脚灌 5V。
    for it in items:
        sym_l = it.get("symbol", "").lower()
        if not (sym_l.startswith("device:r") or sym_l.startswith("device:c")):
            continue
        desc = f"{it.get('name', '')} {it.get('desc', '')}".lower()
        # 稳压器输入滤波电容必须留在输入端：它只靠 GND 和稳压器 VI 识别，
        # 不能因为 GND 网上有芯片脚就被一起迁到 3.3V。
        input_filter = (sym_l.startswith("device:c") and
                        any(k in desc for k in ("输入", "input", "vbus", "5v")))
        for p, other in (("1", "2"), ("2", "1")):
            if ops.name(f"{it['ref']}:{p}") in hot:
                other_net = ops.name(f"{it['ref']}:{other}")
                if input_filter and other_net and _GND_RE.search(other_net):
                    continue
                if other_net and any(x.startswith(r + ":") for x in ops.members(other_net)
                                     for r in chip_refs):
                    ops.attach(f"{it['ref']}:{p}", v33)

    # 其它芯片/接口的逻辑电源脚也必须跟着降到 3.3V：典型是 CH340G 的 VCC
    # 和 SWD 排针的 VCC。稳压器自身的 VI 不迁，否则输入端失去高压来源。
    reg33 = {it["ref"] for it in items
             if (v := _regulator_output(it)) is not None and abs(v - 3.3) < 0.2}
    extra = []
    for it in items:
        if it["ref"] in reg33:
            continue
        for name in ("VCC", "VDD", "VBAT", "VDDA"):
            for p in _p(it, lib, name):
                pin = f"{it['ref']}:{p}"
                if ops.name(pin) in hot:
                    extra.append((pin, name))
    # 排针脚名常是 Pin_N，无法按名字识别；但网名只是笼统的 VCC/VDD 时，
    # 挂在 hot 上的非 USB 连接器脚就是逻辑电源，也应迁到 3.3V。
    # 明确叫 5V/VBUS 的网络不动，避免把真正的 USB 5V 输入搬走。
    for it in items:
        sym = it.get("symbol", "").lower()
        if it["ref"] in reg33 or not ("connector:" in sym or "conn_01x" in sym):
            continue
        for net in ops.nets:
            if net["name"] not in hot or re.fullmatch(
                    r"(\+?5v|vbus|usb_vbus)", net["name"], re.I):
                continue
            for pin in [p for p in net["pins"] if p.startswith(it["ref"] + ":")]:
                extra.append((pin, net["name"]))
    for pin, name in dict.fromkeys(extra):
        ops.attach(pin, v33)
        fixes.append(f"{pin}（{name}）从 {old} 迁到 {v33}")

    fixes.append(f"把 {', '.join(chip_refs)} 的 {'/'.join(moved_names)} 从 {old} 迁到 {v33}")


def _fix_regulator_input(items, ops, lib, fixes: list[str]) -> None:
    """稳压器 VI/VIN 悬空时补 5V 输入网（必要时新增 2 针电源接口）。

    LLM 常把 3.3V 稳压器的输出/地接好，却把输入端整脚漏掉；ERC 与四层视觉
    检查都不报，板子实际没有电源入口。若板上已有 5V/VBUS 网就直接并入，
    否则新增 Conn_01x02_Pin 作为 5V_IN↔GND 输入，保证有实际来源。
    """
    gnd = _gnd_net(ops)
    if not gnd:
        return
    vbus = _usb_vbus_net(ops, items, lib)
    if not vbus:
        for net in ops.nets:
            if re.search(r"(?:^|[_+])(?:5v|vbus|vin)(?:$|[_+])", net["name"], re.I):
                vbus = net["name"]
                break
    for reg in items:
        if "regulator" not in reg.get("symbol", "").lower():
            continue
        pins = _p(reg, lib, "VI") or _p(reg, lib, "VIN")
        if not pins:
            continue
        for p in pins:
            pin = f"{reg['ref']}:{p}"
            net = ops.name(pin)
            if net and len(ops.members(net)) > 1:
                continue
            if not vbus:
                ref = _add_ref(items, "J")
                j = {"ref": ref, "name": "电源输入", "value": "5V 输入",
                     "qty": 1, "symbol": "Connector:Conn_01x02_Pin", "footprint": ""}
                items.append(j)
                ops.attach(f"{ref}:1", f"{reg['ref']}_VIN")
                ops.attach(f"{ref}:2", gnd)
                vbus = f"{reg['ref']}_VIN"
                fixes.append(f"稳压器 {reg['ref']} 的输入悬空，新增 {ref}（5V 输入↔GND）")
            ops.attach(pin, vbus)
            fixes.append(
                f"稳压器 {reg['ref']} 的 VI 脚 {pin} 原"
                f"{'悬空' if not net else '仅单脚网 ' + net}，已接到输入电源网 {vbus}")


def _fix_dangling_chip_power(items, ops, lib, fixes: list[str]) -> None:
    """3.3V 芯片的 VDD/VBAT/VDDA 若悬空或只挂在自己的单脚网，并到 3.3V 电源。

    `_fix_power` 只处理「原本接在 5V 热网」的供电脚；LLM 也会把某个 VDD
    脚整脚漏掉（如 STM32 的 VDD36），ERC 视悬空电源输入为警告/不报，
    safety 只看位号不看每个电源脚，功能自检能抓到但此前没有修复动作。
    """
    rail = None
    for it in items:
        v = _regulator_output(it)
        if v is None or abs(v - 3.3) >= 0.2:
            continue
        vo = next(iter(_p(it, lib, "VO") or _p(it, lib, "VOUT") or ["2"]))
        nn = ops.name(f"{it['ref']}:{vo}")
        if nn:
            rail = nn
            break
    if not rail:
        for it in items:
            need = _chip_vcc(it)
            if need is None or need > 3.6:
                continue
            for p in _p(it, lib, "VDD"):
                nn = ops.name(f"{it['ref']}:{p}")
                if nn and len(ops.members(nn)) > 1:
                    rail = nn
                    break
            if rail:
                break
    if not rail:
        return
    gnd = _gnd_net(ops)
    # 3.3V 芯片的全部供电脚，用于识别「供电脚 + 退耦电容」组成的孤立支路。
    power_pins: set[str] = set()
    for it in items:
        need = _chip_vcc(it)
        if need is None or need > 3.6:
            continue
        for name in ("VDD", "VBAT", "VDDA", "VCC"):
            for p in _p(it, lib, name):
                power_pins.add(f"{it['ref']}:{p}")

    for it in items:
        need = _chip_vcc(it)
        if need is None or need > 3.6:
            continue
        for name in ("VDD", "VBAT", "VDDA"):
            for p in _p(it, lib, name):
                pin = f"{it['ref']}:{p}"
                net = ops.name(pin)
                if net and len(ops.members(net)) > 1:
                    # 「电源脚 + 退耦电容」若没有接到电源网就是假性滤波：
                    #  - 电容另一端留在 C8_2 这类孤立网：退耦根本不成立；
                    #  - 电容另一端已接 GND：网络仍只有芯片脚和电容，电源域悬空。
                    # 判据：该网成员只能是 3.3V 芯片供电脚 + 电容脚（不能混进
                    # 其它芯片/电源/信号脚），才并入 3.3V 网。
                    caps: list[tuple[str, str, bool]] = []
                    stub_ok = bool(gnd) and net != rail
                    for other in ops.members(net):
                        if other in power_pins:
                            continue
                        ref, _sep, num = other.partition(":")
                        cit = next((x for x in items if x["ref"] == ref), None)
                        sym = (cit or {}).get("symbol", "").lower()
                        if not (sym.startswith("device:c") or "电容" in str((cit or {}).get("name", ""))):
                            stub_ok = False
                            break
                        opposite = f"{ref}:{'2' if num == '1' else '1'}"
                        oname = ops.name(opposite)
                        grounded = bool(oname and _GND_RE.search(oname))
                        if oname and not grounded and len(ops.members(oname)) > 1:
                            stub_ok = False
                            break
                        caps.append((other, opposite, grounded))
                    if stub_ok and caps:
                        for pp in sorted(p for p in ops.members(net) if p in power_pins):
                            ops.attach(pp, rail)
                        for cap_pin, opposite, grounded in caps:
                            ops.attach(cap_pin, rail)
                            if not grounded:
                                ops.attach(opposite, gnd)
                        fixes.append(
                            f"{pin}（{name}）原在 {net} 只与退耦电容组成孤立电源域，"
                            f"已把 {name} 和电容并入 {rail}")
                        continue
                    continue                   # 已有真实供电/滤波网络
                ops.attach(pin, rail)
                fixes.append(
                    f"{it['ref']} 的 {name} 脚 {pin} 原"
                    f"{'悬空' if not net else '仅单脚网 ' + net}，已接到 3.3V 网 {rail}")


def _is_gpio_net(ops, name, items) -> bool:
    """网络含非电源/GND 的活跃引脚（MCU/连接器/其它芯片）→ 视作 GPIO 网。"""
    if not name or _SUPPLY_RE.match(name) or _GND_RE.search(name):
        return False
    for pin in ops.members(name):
        ref = pin.partition(":")[0]
        it = next((x for x in items if x.get("ref") == ref), None)
        if not it:
            continue
        sym = it.get("symbol", "").lower()
        if sym.startswith("device:r") or sym.startswith("device:c"):
            continue   # 电阻/电容端不算驱动源
        return True
    return False


def _has_active_chip(ops, name, items) -> bool:
    """网里是否含真正的活跃芯片脚（主控/接口 IC/连接器），排除 R/C/LED。

    `_is_gpio_net` 只排除 R/C，会把「LED 阳极 + 限流电阻」网误判成 GPIO 网。
    在需要确认「确有驱动源」的场合（如生成 GPIO(x)→R→LED 提示）必须用本函数，
    否则会产出与网表矛盾的假提示。
    """
    if not name or _SUPPLY_RE.match(name) or _GND_RE.search(name):
        return False
    for pin in ops.members(name):
        ref = pin.partition(":")[0]
        it = next((x for x in items if x.get("ref") == ref), None)
        if not it:
            continue
        sym = it.get("symbol", "").lower()
        if (sym.startswith("device:r") or sym.startswith("device:c")
                or sym.endswith(":led") or "led" in sym):
            continue
        return True
    return False


def _fix_orphan_led_drivers(items, ops, lib, fixes):
    """修复「孤儿 GPIO 限流电阻 + LED 常亮在电源」断头网（通用，不靠网络名判电源）。

    LLM 常把两个 GPIO 驱动的 LED 画坏：限流电阻一端接 GPIO、另一端的网络
    成员却全是电阻端（断头网，没接到任何 LED），而 LED 反过来经另一个电阻
    常亮在电源上——这样 LED 无法被程序控制。判据（须同时满足，避免误伤
    正常的电源指示灯）：
      1) 存在断头网：成员数>=2 且全部是电阻端、无 LED/芯片/电源/GND；
      2) 断头网里每个电阻的另一端是 GPIO 网（有活跃引脚）；
      3) 存在 LED：阴极接 GND、阳极经一个限流电阻接到电源（常亮）。
    逐个把孤儿电阻的悬空端接到 LED 阳极，并移除该 LED 原来的电源侧电阻，
    形成 GPIO→R→LED→GND。没有断头网时不触发，已正确的 LED 不受影响。
    """
    res_items = [it for it in items
                 if it.get("symbol", "").lower().startswith("device:r")]
    leds = [it for it in items if "led" in
            f"{it.get('value','')} {it.get('symbol','')}".lower()]
    if not res_items or not leds:
        return

    # 1)+2) 断头网 → 孤儿电阻列表（电阻，接到 LED 阳极的悬空端 pin）
    orphans = []   # (res, dangling_pin, gpio_net_name)
    for net in list(ops.nets):
        pins = list(net["pins"])
        # 断头网可以只有 1 个成员：LLM 常把限流电阻一端接 GPIO、另一端
        # 留在只有自己的悬空网（如 LED_CTRL=[R2:2]）。旧版要求 >=2 成员，
        # 这类「孤儿电阻」就漏掉了，LED 反而常亮在电源、无法被程序控制。
        if len(pins) < 1:
            continue
        if not all(p.partition(":")[0].startswith("R") for p in pins):
            continue
        # 该网所有成员都是电阻端 → 断头网
        refset = {p.partition(":")[0] for p in pins}
        resistors = [r for r in res_items if r["ref"] in refset]
        if len(resistors) != len(refset):
            continue
        for r in resistors:
            for p in pins:
                if not p.startswith(r["ref"] + ":"):
                    continue
                other = "1" if p.endswith(":2") else "2"
                other_net = ops.name(f"{r['ref']}:{other}")
                if _is_gpio_net(ops, other_net, items):
                    orphans.append((r, p, other_net))
    if not orphans:
        return

    # 3) 找「经一个限流电阻常亮在电源」的 LED（阴极 GND，阳极网=LED:2+R:2）
    def supply_leds():
        found = []
        by_ref = {it["ref"]: it for it in items}
        for led in leds:
            a = next(iter(_p(led, lib, "A")), "2")
            k = next(iter(_p(led, lib, "K")), "1")
            if not _GND_RE.search(ops.name(f"{led['ref']}:{k}") or ""):
                continue
            anet = ops.name(f"{led['ref']}:{a}")
            if not anet:
                continue
            members = [p for p in ops.members(anet) if p != f"{led['ref']}:{a}"]
            if len(members) != 1:
                continue
            rp = members[0]
            rref = rp.partition(":")[0]
            r = by_ref.get(rref)
            if not r or not r.get("symbol", "").lower().startswith("device:r"):
                continue
            rother = "1" if rp.endswith(":2") else "2"
            if _SUPPLY_RE.match(ops.name(f"{rref}:{rother}") or ""):
                found.append((led, a, r, rref, rother))
        return found

    candidates = supply_leds()
    if not candidates:
        return

    # 配对：每个孤儿电阻救活一个常亮 LED
    for (res, dpin, gpio_net), (led, a, res_supply, rref, rother) in zip(
            orphans, candidates):
        led_net = f"LED_A_{led['ref']}"
        # 移除原来的电源侧限流电阻（避免一边接 GPIO 一边接 VCC 并联）
        ops.remove_component(rref, [f"{rref}:1", f"{rref}:2"])
        items[:] = [x for x in items if x.get("ref") != rref]
        # GPIO 电阻悬空端 + LED 阳极并入 LED 专属网
        ops.attach(dpin, led_net)
        ops.attach(f"{led['ref']}:{a}", led_net)
        fixes.append(
            f"{led['ref']} 由常亮改为 GPIO 驱动：{gpio_net}→{res['ref']}→{led['ref']}→GND"
            f"（移除冗余电源电阻 {rref}）")


def _fix_led_cross_resistor(items, ops, lib, fixes):
    """修复「限流电阻跨接两个 LED 阳极」——两灯反串、都不亮。

    判据（严格，避免误伤）：某电阻的两端分别落在两个不同 LED 的阳极网，
    且这两个网各自只含该 LED 阳极 + 该电阻端（即电阻被夹在两 LED 阳极之间）。
    修复（不删元件，避免布局退化）：
      - 悬空电阻（一端电源、另一端只含自己）的悬空端 → 电源灯阳极（常亮）；
      - 跨接电阻在电源灯网的那端 → 改接到 GPIO 网，保留在用户灯网的另一端，
        于是跨接电阻变成用户灯的 GPIO 限流电阻；
      - 证据不足（无悬空电阻或无 GPIO 网）时保守跳过，不制造新错误。
    """
    res_items = [it for it in items
                 if it.get("symbol", "").lower().startswith("device:r")]
    leds = [it for it in items
            if "led" in f"{it.get('value','')} {it.get('symbol','')}".lower()
            and not it.get("symbol", "").lower().startswith("device:r")]
    if not res_items or len(leds) < 2:
        return

    # LED 阳极 -> (阳极脚, 网名)
    anode: dict[str, tuple[str, str]] = {}
    for led in leds:
        a = next(iter(_p(led, lib, "A")), "2")
        n = ops.name(f"{led['ref']}:{a}")
        if n:
            anode[led["ref"]] = (a, n)
    net_leds: dict[str, list[tuple[str, str]]] = {}
    for lref, (a, n) in anode.items():
        net_leds.setdefault(n, []).append((lref, a))

    for r in res_items:
        n1, n2 = ops.name(f"{r['ref']}:1"), ops.name(f"{r['ref']}:2")
        if not (n1 and n2 and n1 != n2 and n1 in net_leds and n2 in net_leds):
            continue

        def only_res(netname, rref):
            led_pins = {f"{l}:{a}" for l, a in net_leds[netname]}
            extra = [p for p in ops.members(netname) if p not in led_pins]
            return bool(extra) and all(p.startswith(rref + ":") for p in extra)

        if not (only_res(n1, r["ref"]) and only_res(n2, r["ref"])):
            continue
        l1, a1 = net_leds[n1][0]
        l2, a2 = net_leds[n2][0]

        # 按 BOM 语义判断哪个是电源灯（常亮）、哪个是用户/GPIO 灯：
        # 电源灯接悬空电源电阻，用户灯接 GPIO 上拉电阻改接。判据按描述文本，
        # 不依赖具体位号；无法判定则保持 (l1→电源, l2→GPIO) 默认。
        by_ref = {it["ref"]: it for it in items}
        def is_power_led(lref):
            t = " ".join(str(by_ref.get(lref, {}).get(k, "")) for k in
                         ("name", "value", "desc")).lower()
            return any(k in t for k in ("电源", "power", "pwr", "供电"))
        def is_user_led(lref):
            t = " ".join(str(by_ref.get(lref, {}).get(k, "")) for k in
                         ("name", "value", "desc")).lower()
            return any(k in t for k in ("用户", "gpio", "user", "可编程", "信号", "指示"))
        power_led, gpio_led = l1, l2
        if is_user_led(l1) and is_power_led(l2):
            power_led, gpio_led = l2, l1
        # 让 n_power / n_gpio 对应到正确 LED 的阳极网
        net_of_led = {lref: n for n, lst in net_leds.items() for lref, _a in lst}
        n_power, n_gpio = net_of_led[power_led], net_of_led[gpio_led]

        # 跨接电阻 r 的哪一端落在电源灯网 / 用户灯网
        r_pin_power = r_pin_gpio = None
        for num in ("1", "2"):
            nn = ops.name(f"{r['ref']}:{num}")
            if nn == n_power:
                r_pin_power = num
            elif nn == n_gpio:
                r_pin_gpio = num
        if not (r_pin_power and r_pin_gpio):
            continue

        # 悬空电阻：一端电源、另一端在「只含自己」的网 → 给电源灯
        dangling = None
        for rr in res_items:
            if rr["ref"] == r["ref"]:
                continue
            for num, other in (("1", "2"), ("2", "1")):
                nn = ops.name(f"{rr['ref']}:{num}")
                on = ops.name(f"{rr['ref']}:{other}")
                if (nn and _SUPPLY_RE.match(nn) and on
                        and ops.members(on) == {f"{rr['ref']}:{other}"}):
                    dangling = (rr, other)
                    break
            if dangling:
                break
        # GPIO 网：找含芯片活跃引脚的用户 GPIO（如 GPIO2），作为用户灯驱动源。
        # 排除 EN/RESET/BOOT 等控制网和只接按键的网——它们不是可编程 GPIO。
        def is_user_gpio(netname):
            n = (netname or "").lower()
            if any(k in n for k in ("en", "reset", "rst", "boot", "sw", "key", "button", "chip_pu")):
                return False
            for p in ops.members(netname):
                ref = p.partition(":")[0]
                it = next((x for x in items if x.get("ref") == ref), None)
                if it and "switch" in it.get("symbol", "").lower():
                    return False
            return _is_gpio_net(ops, netname, items)
        gpio_net = None
        for net in ops.nets:
            if (is_user_gpio(net["name"])
                    and net["name"] not in (n_power, n_gpio)):
                gpio_net = net["name"]
                break
        # 证据不足则保守跳过，不制造新错误
        if not dangling or not gpio_net:
            continue

        # 电源灯：悬空电阻的悬空端接到电源灯阳极 → 电源→R→LED→GND（常亮）
        rr, dp = dangling
        ops.attach(f"{rr['ref']}:{dp}", n_power)
        fixes.append(f"{power_led} 用悬空电阻 {rr['ref']} 限流：电源→{rr['ref']}→{power_led}→GND")
        # 用户灯：跨接电阻在电源灯网的那端改接到 GPIO，另一端保留在用户灯阳极
        ops.attach(f"{r['ref']}:{r_pin_power}", gpio_net)
        fixes.append(
            f"{gpio_led} 用原跨接电阻 {r['ref']} 作 GPIO 限流：{gpio_net}→{r['ref']}→{gpio_led}→GND")
        break   # 一次只修一个跨接电阻


def _fix_mislabeled_led_resistor(items, ops, lib, fixes):
    """BOM 标注为「LED限流」的电阻若被接成 GPIO 上拉，纠正为 LED 串联电阻。

    LLM 常把同一个电阻既写进 BOM 当 LED 限流，又接成 GPIO 上拉，
    然后另加一个电阻把 LED 直接挂电源变成常亮灯。这里按「描述语义 +
    实际网络」交叉判断：有 GPIO 侧、有 LED 阳极侧、且另有电源侧电阻，
    才把标注限流电阻挪到 LED 阳极并删掉冗余电源电阻。
    """
    by_ref = {it["ref"]: it for it in items}
    leds = [it for it in items
            if it.get("symbol", "").lower().endswith(":led")]
    for led in leds:
        a = next(iter(_p(led, lib, "A")), "2")
        k = next(iter(_p(led, lib, "K")), "1")
        a_net = ops.name(f"{led['ref']}:{a}")
        k_net = ops.name(f"{led['ref']}:{k}")
        if not a_net or not k_net or not _GND_RE.search(k_net):
            continue
        # 阳极网里那个「电源 → 电阻 → LED」的常亮限流电阻。
        # 不删元件：删掉会改变布局，之前已多次造成晶振/电容被挤到页边断线。
        supply_res = None
        supply_hot_pin = None
        for p in sorted(ops.members(a_net)):
            ref = p.partition(":")[0]
            it = by_ref.get(ref)
            if not it or not it.get("symbol", "").lower().startswith("device:r"):
                continue
            other = f"{ref}:{'2' if p.endswith(':1') else '1'}"
            on = ops.name(other)
            if on and (_SUPPLY_RE.match(on) or _V33_NAME_RE.search(on)):
                supply_res, supply_hot_pin = it, other
                break
        if supply_res is None:
            continue
        # 找 BOM 里自称 LED 限流、实际却只做 GPIO 上拉的电阻
        for cand in items:
            if cand is supply_res or not cand.get("symbol", "").lower().startswith("device:r"):
                continue
            desc = f"{cand.get('name', '')} {cand.get('desc', '')}".lower()
            if "led" not in desc or "限流" not in desc:
                continue
            for pin, other_pin in (("1", "2"), ("2", "1")):
                n = ops.name(f"{cand['ref']}:{pin}")
                on = ops.name(f"{cand['ref']}:{other_pin}")
                if not n or not on or a_net in (n, on):
                    continue
                if _has_active_chip(ops, n, items) and not _is_gpio_net(ops, on, items):
                    # 复用电源侧那个电阻做 GPIO 限流：保留元件和布局，
                    # 只把它原本接电源的一端改接 GPIO。
                    # 注意 n 必须是真芯片脚网：LED 阳极+电阻不是驱动源，
                    # 否则会生成「GPIO(LED_A_LED2)→R→LED」这种假提示。
                    ops.attach(supply_hot_pin, n)
                    # 原「LED限流」电阻其实只是 GPIO 上拉，修正 BOM 描述
                    cand["name"] = "电阻（GPIO 上拉）"
                    cand["desc"] = "GPIO 上拉电阻"
                    ref = supply_res["ref"]
                    # 旧的「新增 R3 串在电源与 LED 之间」说明已失效
                    fixes[:] = [f for f in fixes if ref not in f]
                    fixes.append(
                        f"LED {led['ref']} 改由 {ref} 限流：GPIO({n})→{ref}→"
                        f"{led['ref']}→GND；{cand['ref']} 改正为 GPIO 上拉")
                    return


def _fix_led_gpio_no_resistor(items, ops, lib, fixes):
    """修复「LED 阳极直接挂在 MCU GPIO 上、没有串联限流电阻」。

    典型：GPIO 网同时挂着 MCU 脚、LED 阳极、上拉电阻、别的灯的限流电阻，
    LED 阳极和 GPIO 同网即直连——通电会烧 LED / IO 口。ERC 不管这个，
    safety 旧规则也只看电源网，所以三检全绿但电路是错的。
    判据（严格）：阴极接 GND；阳极网含 MCU 引脚；但该 LED 阳极**没有**
    专属串联电阻（阳极网不是「LED+单个电阻」的独占网）。
    修法：复用同网里一端已在 GPIO、另一端接 GND/电源的上拉/下拉电阻做限流，
    把 LED 阳极单独拎到 LED_A_<ref> 网；不删元件，不动别的灯。
    """
    by_ref = {it["ref"]: it for it in items}
    mcu_refs = {it["ref"] for it in items if _is_mcu_item(it)}
    if not mcu_refs:
        return
    for led in [it for it in items
                if "led" in f"{it.get('value','')} {it.get('symbol','')}".lower()
                and it.get("symbol", "").lower().endswith(":led")]:
        a = next(iter(_p(led, lib, "A")), "2")
        k = next(iter(_p(led, lib, "K")), "1")
        a_net = ops.name(f"{led['ref']}:{a}")
        k_net = ops.name(f"{led['ref']}:{k}")
        if not a_net or not k_net:
            continue
        members = ops.members(a_net)
        if not any(p.partition(":")[0] in mcu_refs for p in members):
            continue                      # 阳极网没有 MCU 驱动脚 → 不是 GPIO 直驱
        # 阴极经电阻回 GND：电流确实被限制，但安全自检按「阳极串阻」口径
        # 会一直报警。把同一颗电阻挪到阳极、阴极直接接地，不增加 BOM。
        if not _GND_RE.search(k_net):
            k_members = ops.members(k_net)
            if len(k_members) == 2:
                rp = next((p for p in k_members if p != f"{led['ref']}:{k}"), None)
                if rp:
                    ref, _, num = rp.partition(":")
                    rit = by_ref.get(ref)
                    other = f"{ref}:{'2' if num == '1' else '1'}"
                    o_net = ops.name(other)
                    if (rit and rit.get("symbol", "").lower().startswith("device:r")
                            and o_net and _GND_RE.search(o_net)):
                        led_net = f"LED_A_{led['ref']}"
                        n = 2
                        while any(net_name.get("name") == led_net for net_name in ops.nets):
                            led_net = f"LED_A_{led['ref']}_{n}"
                            n += 1
                        ops.attach(f"{led['ref']}:{a}", led_net)
                        ops.attach(rp, led_net)
                        # 电阻的另一端必须回到原 GPIO 网，否则灯会脱离控制；
                        # 阴极直接接到电阻原来占用的 GND 网。
                        ops.attach(other, a_net)
                        ops.attach(f"{led['ref']}:{k}", o_net)
                        rit["name"] = "电阻（LED 限流）"
                        rit["desc"] = "LED 限流电阻"
                        fixes.append(
                            f"LED {led['ref']} 的限流电阻原在阴极侧（{a_net}→LED→{ref}→GND），"
                            f"已挪到阳极：{a_net}→{ref}→{led['ref']}→GND")
                        continue
        if not _GND_RE.search(k_net):
            continue
        # 已有专属串联电阻：阳极网只有 LED 阳极 + 一个电阻 → 已正确，跳过
        if len(members) == 2 and any(
                p.partition(":")[0] != led["ref"]
                and by_ref.get(p.partition(":")[0], {}).get("symbol", "").lower().startswith("device:r")
                for p in members):
            continue
        # 找可复用的上拉/下拉电阻：一端已在 GPIO 网，另一端接 GND 或电源
        reuse = None
        for p in sorted(members):
            ref, num = p.split(":", 1)
            it = by_ref.get(ref)
            if not it or not it.get("symbol", "").lower().startswith("device:r"):
                continue
            other = f"{ref}:{'2' if num == '1' else '1'}"
            on = ops.name(other)
            if on and (_GND_RE.search(on) or _SUPPLY_RE.match(on)):
                reuse = (it, other)
                break
        led_net = f"LED_A_{led['ref']}"
        if reuse:
            res, res_pin = reuse
            ops.attach(f"{led['ref']}:{a}", led_net)
            ops.attach(res_pin, led_net)
            res["name"] = "电阻（LED 限流）"
            res["desc"] = "LED 限流电阻"
            fixes.append(f"LED {led['ref']} 原先阳极直挂 GPIO（{a_net}），"
                         f"复用 {res['ref']} 作限流：{a_net}→{res['ref']}→{led['ref']}→GND")
        else:
            ref = _add_ref(items, "R")
            items.append({"ref": ref, "name": "电阻（LED 限流）", "value": "1k",
                          "qty": 1, "symbol": "Device:R", "footprint": ""})
            ops.attach(f"{led['ref']}:{a}", led_net)
            ops.attach(f"{ref}:2", led_net)
            ops.attach(f"{ref}:1", a_net)
            fixes.append(f"新增 {ref}（1kΩ）串在 GPIO({a_net}) 与 {led['ref']} 之间限流")


def _fix_floating_led_gpio(items, ops, lib, fixes):
    """修复「MCU 的 LED 控制 GPIO 悬空在单成员网，LED 却常亮在电源」。

    LLM 有时把 LED 的 GPIO 驱动脚单独留在一个含 LED 编号的网名里
    （如 LED2_GND 只含 PC13），却把该 LED 经限流电阻常亮接到电源上——
    结果 LED 永远亮、程序控制不了。ERC/视觉/电气告警都查不出（悬空输入合法、
    电源灯限流正确）。这里按结构识别并把限流电阻的电源端改接到该 GPIO，
    形成 GPIO→R→LED→GND。

    判据（须同时满足，避免误伤 VBAT/NRST/BOOT 等悬空电源/控制脚）：
      1) 单成员网，成员是 MCU 的 GPIO 脚，网名含 led 编号（如 led2）；
      2) 该脚不是电源/复位/BOOT 等特殊功能脚；
      3) 存在 LED：阴极接 GND，阳极网含一个电阻端，该电阻另一端接电源。
    """
    by_ref = {it["ref"]: it for it in items}
    mcu_items = [it for it in items if _is_mcu_item(it)]
    if not mcu_items:
        return
    mcu_refs = {it["ref"]: it for it in mcu_items}
    leds = [it for it in items
            if it.get("symbol", "").lower().endswith(":led")
            or "led" in it.get("symbol", "").lower()]

    for net in list(ops.nets):
        pins = list(net["pins"])
        if len(pins) != 1:
            continue
        ref, _, pin_num = pins[0].partition(":")
        mcu = mcu_refs.get(ref)
        if not mcu:
            continue
        net_l = net["name"].lower()
        # 网名含 LED 语义（LED2_GND）或本身是标准 GPIO 名（PC13/PB0）都算候选。
        # LLM 常把用户 LED 的控制脚直接命名为引脚名，旧规则只认 "led" 会漏掉。
        bare_net = net_l.lstrip("/")
        is_gpio_name = bool(_PORT_GROUP_RE.fullmatch(bare_net))
        if "led" not in net_l and not is_gpio_name:
            continue
        # 排除电源/复位/BOOT 等功能脚：按符号真实引脚名判定
        pname = ""
        if lib:
            try:
                raw = lib.pin_names(mcu["symbol"]).get(int(pin_num), "")
            except (ValueError, TypeError):
                raw = ""
            pname = str(raw).lower()
        if any(k in pname for k in ("vbat", "vdda", "vdd", "vss", "gnd",
                                    "nrst", "reset", "boot", "osc", "xtal")):
            continue

        # 找经电阻常亮在电源的 LED：阴极 GND、阳极网含某电阻端，该电阻另一端接电源
        fixed = False
        for led in leds:
            a = next(iter(_p(led, lib, "A")), "2")
            k = next(iter(_p(led, lib, "K")), "1")
            a_net = ops.name(f"{led['ref']}:{a}")
            k_net = ops.name(f"{led['ref']}:{k}")
            if not a_net or not k_net or not _GND_RE.search(k_net):
                continue
            # 网名里的 led 编号要对得上（LED2_GND ↔ LED2），避免张冠李戴
            m = re.search(r"led\s*(\d+)", net_l)
            lm = re.search(r"(\d+)$", led["ref"])
            if m and lm and m.group(1) != lm.group(1):
                continue
            # 网名是纯 GPIO 名（PC13）时没有编号线索：只改 BOM 明确标为
            # 「用户指示灯」的 LED，绝不误伤「电源指示灯」这类常亮灯。
            if is_gpio_name and not m:
                desc = f"{led.get('name','')} {led.get('desc','')}".lower()
                if not re.search(r"用户|user", desc):
                    continue
            for p in sorted(ops.members(a_net)):
                rr = p.partition(":")[0]
                it = by_ref.get(rr)
                if not it or not it.get("symbol", "").lower().startswith("device:r"):
                    continue
                other = f"{rr}:{'2' if p.endswith(':1') else '1'}"
                on = ops.name(other)
                if on and (_SUPPLY_RE.match(on) or _V33_NAME_RE.search(on)):
                    ops.attach(other, net["name"])
                    fixes.append(
                        f"{led['ref']} 的 GPIO 控制脚 {pins[0]} 原悬空（{net['name']}），"
                        f"LED 经 {rr} 常亮在电源；已将 {rr} 电源端改接 {pins[0]}："
                        f"{pins[0]}→{rr}→{led['ref']}→GND")
                    fixed = True
                    break
            if fixed:
                break
        if fixed:
            break

    # 兜底：有些生成结果把整组 GPIO 都引到了排针，网表里没有「单脚 GPIO 网」
    # 可复用；此时从 MCU 符号里挑一个完全未使用、且不带特殊功能的 GPIO，
    # 新建网络接走常亮电阻。否则用户指示灯永远亮、无法被程序控制。
    for led in leds:
        desc = f"{led.get('name', '')} {led.get('desc', '')}".lower()
        if not re.search(r"用户|user", desc):
            continue
        a = next(iter(_p(led, lib, "A")), "2")
        an = ops.name(f"{led['ref']}:{a}")
        if not an:
            continue
        for p in sorted(ops.members(an)):
            rr, _, num = p.partition(":")
            rit = by_ref.get(rr)
            if not rit or not rit.get("symbol", "").lower().startswith("device:r"):
                continue
            other = f"{rr}:{'2' if num == '1' else '1'}"
            on = ops.name(other)
            if not (on and (_SUPPLY_RE.match(on) or _V33_NAME_RE.search(on))):
                continue
            for mcu in mcu_items:
                if lib is None:
                    break
                try:
                    primary = lib.pin_names(mcu["symbol"])
                    alts = lib.pin_names_with_alts(mcu["symbol"])
                except Exception:
                    break
                # 偏好 PC13（常见用户灯脚），其次按端口/编号自然序。
                def _gpio_sort(item):
                    nm = str(item[1]).split()[0].upper()
                    pref = 0 if nm == "PC13" else 1
                    m = re.fullmatch(r"([A-Z])(\d+)", nm)
                    return (pref, m.group(1) if m else "Z",
                            int(m.group(2)) if m else 999)
                candidates = [
                    (n, nm) for n, nm in primary.items()
                    if _PORT_GROUP_RE.fullmatch(str(nm).split()[0])
                ]
                for n, nm in sorted(candidates, key=_gpio_sort):
                    pin = f"{mcu['ref']}:{n}"
                    if ops.name(pin):
                        continue                     # 已用脚不动
                    full = str(alts.get(n, nm)).upper()
                    if any(k in full for k in ("SWD", "JTMS", "JTCK", "JTDI",
                                               "JTDO", "USART", "UART", "OSC",
                                               "XTAL", "BOOT", "NRST", "RESET",
                                               "VDD", "VSS", "GND", "VBAT")):
                        continue
                    gpio_net = str(nm).split()[0]
                    if gpio_net in {x["name"] for x in ops.nets}:
                        gpio_net = f"{mcu['ref']}_{gpio_net}"
                    ops.attach(pin, gpio_net)
                    ops.attach(other, gpio_net)
                    fixes.append(
                        f"{led['ref']}（用户指示灯）原经 {rr} 常亮在 {on}，"
                        f"已把 {rr} 改接空闲 GPIO {pin}（{gpio_net}），"
                        f"形成 {gpio_net}→{rr}→{led['ref']}→GND")
                    break
            break


def _fix_reversed_led(items, ops, lib, fixes):
    """纠正 LED 极性接反：阳极接 GND、阴极经限流电阻接电源。

    LLM 有时把 LED 两脚接反（如 D1 阳极 A=2 落 GND、阴极 K=1 经 R 到 VCC），
    LED 反向截止永远不亮。ERC 合法、视觉正常、限流电阻也在，三检全绿；
    safety 会报「阳极挂在 GPIO」但 fix_topology 旧规则修不了。这里按结构对调
    阳极/阴极所接的网络，使 LED 正向导通（电源→R→阳极→阴极→GND）。

    判据（须同时满足，避免误伤正常 LED）：
      1) 阳极网是 GND、阴极网不是 GND，且阴极网含 LED 阴极 + 一个电阻端；
      2) 该电阻另一端接电源（说明阴极侧是高侧，LED 确实接反）。
    """
    by_ref = {it["ref"]: it for it in items}
    for led in [it for it in items
                if it.get("symbol", "").lower().endswith(":led")]:
        a = next(iter(_p(led, lib, "A")), "2")
        k = next(iter(_p(led, lib, "K")), "1")
        a_net = ops.name(f"{led['ref']}:{a}")
        k_net = ops.name(f"{led['ref']}:{k}")
        if not a_net or not k_net or a_net == k_net:
            continue
        if not _GND_RE.search(a_net) or _GND_RE.search(k_net):
            continue                       # 阳极必须接地、阴极不能接地
        # 阴极网里找限流电阻：另一端接电源
        found = False
        for p in sorted(ops.members(k_net)):
            rr = p.partition(":")[0]
            it = by_ref.get(rr)
            if not it or not it.get("symbol", "").lower().startswith("device:r"):
                continue
            other = f"{rr}:{'2' if p.endswith(':1') else '1'}"
            on = ops.name(other)
            if on and (_SUPPLY_RE.match(on) or _V33_NAME_RE.search(on)):
                found = True
                break
        if not found:
            continue
        # 对调 LED 两脚的网络：阳极→原阴极网（经 R 到电源），阴极→GND
        ops.attach(f"{led['ref']}:{a}", k_net)
        ops.attach(f"{led['ref']}:{k}", a_net)
        fixes.append(
            f"{led['ref']} 极性接反（阳极原接 GND、阴极经电阻接电源），"
            f"已对调：电源→R→{led['ref']} 阳极→阴极→GND")


def _fix_led_cathode_resistor_to_gpio(items, ops, lib, fixes: list[str]) -> None:
    """LED 阳极直挂 GPIO、阴极经电阻回到**另一个 GPIO**时，把电阻挪到阳极侧。

    LLM 有时画出 PB0→LED→R→PC13：阴极那颗电阻虽然限流，但两端都是 IO，
    不是电源/地回路，安全自检会报「阳极直接挂在 GPIO 上、没有限流电阻」。
    通用修法：复用这颗电阻串到阳极，阴极直接接地，原另一个 GPIO 释放。
    """
    by_ref = {it["ref"]: it for it in items}
    mcu_refs = {it["ref"] for it in items if _is_mcu_item(it)}
    if not mcu_refs:
        return
    gnd = _gnd_net(ops)
    for led in [it for it in items
                if "led" in f"{it.get('value', '')} {it.get('symbol', '')}".lower()
                and it.get("symbol", "").lower().endswith(":led")]:
        a = next(iter(_p(led, lib, "A")), "2")
        k = next(iter(_p(led, lib, "K")), "1")
        a_pin, k_pin = f"{led['ref']}:{a}", f"{led['ref']}:{k}"
        a_net, k_net = ops.name(a_pin), ops.name(k_pin)
        if not a_net or not k_net or _GND_RE.search(k_net) or _SUPPLY_RE.match(k_net):
            continue
        # 另一种形态：阳极整脚悬空（只有 LED 自己），阴极经电阻回到 GPIO。
        # 例如 LED2_A=[LED2:2]、LED2_K=[LED2:1,R3:2]、R3:1 在 PC13_LED。
        # 把电阻的阴极侧挪到阳极网、阴极直接接地，形成 GPIO→R→LED→GND。
        a_members = ops.members(a_net)
        if len(a_members) == 1 and a_pin in a_members:
            k_members = ops.members(k_net)
            if len(k_members) == 2:
                rp = next((p for p in k_members if p != k_pin), None)
                if rp:
                    ref, _, num = rp.partition(":")
                    rit = by_ref.get(ref)
                    other = f"{ref}:{'2' if num == '1' else '1'}"
                    o_net = ops.name(other)
                    if (rit and rit.get("symbol", "").lower().startswith("device:r")
                            and o_net and any(p.partition(":")[0] in mcu_refs
                                              for p in ops.members(o_net))):
                        ops.attach(rp, a_net)
                        ops.attach(k_pin, gnd)
                        fixes.append(
                            f"LED {led['ref']} 阳极原悬空、阴极经 {ref} 回 GPIO({o_net})；"
                            f"已把 {ref} 挪到阳极侧、阴极接地："
                            f"GPIO({o_net})→{ref}→{led['ref']}→GND")
                        continue
        if not any(p.partition(":")[0] in mcu_refs for p in ops.members(a_net)):
            continue                     # 阳极不是 GPIO 驱动
        k_members = ops.members(k_net)
        if len(k_members) != 2:
            continue
        rp = next((p for p in k_members if p != k_pin), None)
        if not rp:
            continue
        ref, _, num = rp.partition(":")
        rit = by_ref.get(ref)
        if not rit or not rit.get("symbol", "").lower().startswith("device:r"):
            continue
        other = f"{ref}:{'2' if num == '1' else '1'}"
        o_net = ops.name(other)
        if (not o_net or _GND_RE.search(o_net) or _SUPPLY_RE.match(o_net)
                or not any(p.partition(":")[0] in mcu_refs
                           for p in ops.members(o_net))):
            continue
        new_net = f"LED_A_{led['ref']}"
        k = 2
        while any(n.get("name") == new_net for n in ops.nets):
            new_net = f"LED_A_{led['ref']}_{k}"
            k += 1
        ops.attach(a_pin, new_net)
        ops.attach(rp, new_net)
        ops.attach(other, a_net)
        ops.attach(k_pin, gnd)
        fixes.append(
            f"LED {led['ref']} 原为 GPIO({a_net})→LED→{ref}→GPIO({o_net})；"
            f"已将 {ref} 串到阳极、阴极接地：GPIO({a_net})→{ref}→{led['ref']}→GND")


def _fix_led_gpio_pullup_always_on(items, ops, lib, fixes: list[str]) -> None:
    """修复「LED 阳极经限流电阻到 GPIO，但该 GPIO 又被上拉电阻接到电源」。

    变体（ERC/视觉/safety/功能全绿，实际灯常亮且 GPIO 拉不下来）：
      3V3 ─R2─ ┬─ U1:2(PC13 GPIO)
                └─ R3 ─ LED_A ─ LED 阳极 ─ LED 阴极 ─ GND
    R2 把 GPIO 永久拉高，LED 始终有电流，程序无法关灯。
    通用修法（按拓扑不按位号/网名）：删除那颗把 GPIO 网拉到电源的电阻，
    保留 GPIO→R3→LED→GND。只在该网同时含 MCU GPIO 与 LED 限流电阻时动手。
    """
    by_ref = {it["ref"]: it for it in items}
    mcu_refs = {it["ref"] for it in items if _is_mcu_item(it)}
    if not mcu_refs:
        return
    gnd = _gnd_net(ops)

    def is_res(pin: str) -> bool:
        ref = pin.partition(":")[0]
        return by_ref.get(ref, {}).get("symbol", "").lower().startswith("device:r")

    def res_other(pin: str) -> str | None:
        ref, _, num = pin.partition(":")
        other = f"{ref}:{'2' if num == '1' else '1'}"
        return other if is_res(other) else None

    for led in [it for it in items
                if it.get("symbol", "").lower().endswith(":led")]:
        a = next(iter(_p(led, lib, "A")), "2")
        k = next(iter(_p(led, lib, "K")), "1")
        a_net, k_net = ops.name(f"{led['ref']}:{a}"), ops.name(f"{led['ref']}:{k}")
        if not k_net or not _GND_RE.search(k_net):
            continue
        if not a_net:
            continue
        # LED 阳极网须恰好有一个电阻端（限流电阻 R_led 的 LED 侧）
        a_members = ops.members(a_net)
        led_res_pins = [p for p in a_members if is_res(p)]
        if len(led_res_pins) != 1 or len(a_members) != 2:
            continue
        r_led_pin = led_res_pins[0]
        gpio_side = res_other(r_led_pin)      # R_led 的另一端
        if not gpio_side:
            continue
        gpio_net = ops.name(gpio_side)
        if not gpio_net or _SUPPLY_RE.match(gpio_net) or _GND_RE.search(gpio_net):
            continue
        gm = ops.members(gpio_net)
        # GPIO 网必须含 MCU 脚，且有且仅有另一颗电阻把它上拉到电源
        if not any(p.partition(":")[0] in mcu_refs for p in gm):
            continue
        pull = None
        for p in gm:
            if not is_res(p) or p == gpio_side:
                continue
            other = res_other(p)
            if not other:
                continue
            on = ops.name(other)
            if on and _SUPPLY_RE.match(on):
                pull = (p.partition(":")[0], p, other)
                break
        if not pull:
            continue
        # 删除上拉电阻（两端都从网表摘除，避免残留单脚网/悬空件）
        ref, p1, p2 = pull
        ops.remove_component(ref, [p1, p2])
        items[:] = [x for x in items if x.get("ref") != ref]
        # LED 描述同步为受控状态灯，避免 BOM 仍写「电源指示灯」
        if "电源" in str(led.get("desc", "")) and "指示" in str(led.get("desc", "")):
            led["desc"] = "状态指示灯（GPIO 控制）"
        fixes.append(
            f"LED {led['ref']} 原经 {r_led_pin.partition(':')[0]} 回 GPIO、"
            f"但 GPIO 被 {ref} 上拉到电源而常亮；已删除上拉电阻 {ref}，"
            f"恢复 GPIO→{r_led_pin.partition(':')[0]}→{led['ref']}→GND（可由程序关断）")


def _fix_led(items, ops, lib, fixes):
    """逐个 LED 检查：阳极若直接跨电源则补串联限流电阻。"""
    leds = [it for it in items if "led" in
            f"{it.get('value','')} {it.get('symbol','')}".lower()]
    _fix_reversed_led(items, ops, lib, fixes)
    for led in leds:
        _fix_one_led(led, items, ops, lib, fixes)
    _fix_orphan_led_drivers(items, ops, lib, fixes)
    _fix_led_cross_resistor(items, ops, lib, fixes)
    _fix_mislabeled_led_resistor(items, ops, lib, fixes)
    _fix_led_gpio_no_resistor(items, ops, lib, fixes)
    _fix_led_cathode_resistor_to_gpio(items, ops, lib, fixes)
    _fix_led_gpio_pullup_always_on(items, ops, lib, fixes)
    _fix_led_dead_loop(items, ops, lib, fixes)
    _fix_floating_led_gpio(items, ops, lib, fixes)


def _fix_led_dead_loop(items, ops, lib, fixes):
    """修复「LED 阳极经限流电阻只回到 GND、走不到电源」的死回路。

    典型形态（ERC/视觉/safety 全绿、functional 才发现）：
      LED 阴极 → GND；LED 阳极 → 电阻 → GND。整条支路两端都在地，灯不亮。
    修复：找主电源网（VCC/VDD/3V3/…，或从稳压器输出推导），把串联电阻
    接地的那一端迁到电源网，形成 电源→电阻→LED阳极→LED阴极→GND。
    只复用已有电阻，不新增元件（避免用户抱怨"元件太多"）。判据保守：
      - 阳极网除 LED 自身外恰好只有一个电阻端；
      - 该电阻另一端在 GND 网；
      - 阴极网是 GND 网；
      - 阳极侧沿电阻 BFS 走不到任何电源/GPIO 驱动源。
    """
    by_ref = {it["ref"]: it for it in items}
    leds = [it for it in items
            if it.get("symbol", "").lower().endswith(":led")]

    def pin_sem(ref: str, num: str) -> str:
        if not lib:
            return ""
        it = by_ref.get(ref, {})
        if not it.get("symbol"):
            return ""
        try:
            names = lib.pin_names(it["symbol"])
            return str(names.get(int(num), names.get(num, ""))).upper()
        except Exception:
            return ""

    def has_gnd_pin(netname: str) -> bool:
        for pin in ops.members(netname):
            r, _, num = pin.partition(":")
            if pin_sem(r, num) in ("GND", "VSS", "VSSA", "AGND"):
                return True
        return lib is None and bool(_GND_RE.search(netname))

    def is_res(ref):
        return by_ref.get(ref, {}).get("symbol", "").lower().startswith("device:r")

    # 找主电源网：优先用 3.3V 稳压器输出（板载芯片/指示灯的常规供电域），
    # 其次按引脚语义（VCC/VDD/VI/VBUS），网名仅兜底。不能只靠 _SUPPLY_RE，
    # LLM 会把电源网命名成 PWR_5V 之类导致判据失效；也不能先撞到未稳压
    # 5V 输入网就把 3.3V 板卡的灯挂上去。
    v33_rails = sorted(_v33_rail_nets(ops, items, lib))
    supply = v33_rails[0] if v33_rails else None
    if supply is None:
        for net in ops.nets:
            if has_gnd_pin(net["name"]):
                continue
            for pin in ops.members(net["name"]):
                r, _, num = pin.partition(":")
                if pin_sem(r, num) in ("VCC", "VDD", "VDDA", "VIN", "VI", "VBUS"):
                    supply = net["name"]
                    break
            if supply:
                break
    if supply is None:
        for it in items:
            if "regulator" not in it.get("symbol", "").lower():
                continue
            for p in _p(it, lib, "VO") or ["3", "2"]:
                n = ops.name(f"{it['ref']}:{p}")
                if n and not has_gnd_pin(n):
                    supply = n
                    break
            if supply:
                break
    if supply is None:
        for net in ops.nets:  # 最后按网名兜底
            if _SUPPLY_RE.match(net["name"]) and not _GND_RE.search(net["name"]):
                supply = net["name"]
                break
    if supply is None:
        return

    def is_driven(netname):
        if has_gnd_pin(netname):
            return False
        if _SUPPLY_RE.match(netname):
            return True
        for pin in ops.members(netname):
            r, _, num = pin.partition(":")
            sym = by_ref.get(r, {}).get("symbol", "").lower()
            if sym and not sym.startswith(("device:r", "device:c", "device:l",
                                           "device:led", "device:crystal")):
                if pin_sem(r, num) in ("GND", "VSS", "VSSA", "AGND"):
                    continue
                return True
        return False

    # 守卫：若某 LED 有对应的「悬空 MCU GPIO 网」，说明它是可由程序控制的
    # 用户指示灯，应交给 _fix_floating_led_gpio 接 GPIO，而不是接成常亮电源灯。
    # 否则两条规则会互相把限流电阻在 GPIO/电源间来回搬，永不幂等。
    gpio_led_ids = set()
    mcu_refs = {it["ref"] for it in items if _is_mcu_item(it)}
    for net in ops.nets:
        pins = list(net["pins"])
        nl = net["name"].lower()
        if not pins or "led" not in nl:
            continue
        # 网里必须含 MCU 的脚（不限单成员——GPIO 规则改过后网里会有电阻端，
        # 若只认单成员，改过之后守卫失效，两条规则就会来回搬同一个电阻）。
        if not any(p.partition(":")[0] in mcu_refs for p in pins):
            continue
        m = re.search(r"led\s*(\d+)", nl)
        if m:
            gpio_led_ids.add(m.group(1))

    for led in leds:
        lm = re.search(r"(\d+)$", led["ref"])
        if lm and lm.group(1) in gpio_led_ids:
            continue                      # 让位给 GPIO 规则
        a = next(iter(_p(led, lib, "A")), "2")
        k = next(iter(_p(led, lib, "K")), "1")
        a_pin, k_pin = f"{led['ref']}:{a}", f"{led['ref']}:{k}"
        an, kn = ops.name(a_pin), ops.name(k_pin)
        if not an or not kn or not has_gnd_pin(kn):
            continue
        # 阳极网成员：LED 阳极 + 恰好一个电阻端
        anode_members = [p for p in ops.members(an) if p != a_pin]
        res_pins = [p for p in anode_members if is_res(p.partition(":")[0])]
        if len(res_pins) != 1 or len(anode_members) != 1:
            continue
        rp = res_pins[0]
        rref, _, num = rp.partition(":")
        other = "2" if num == "1" else "1"
        other_pin = f"{rref}:{other}"
        on = ops.name(other_pin)

        # 阳极侧沿电阻 BFS，确认走不到任何驱动源（GND 是汇点，不往下走）
        seen, stack, reached = {an}, [an], False
        while stack:
            cur = stack.pop()
            if is_driven(cur):
                reached = True
                break
            if has_gnd_pin(cur):
                continue
            for p in ops.members(cur):
                r, n = p.partition(":")[0], p.partition(":")[2]
                if not is_res(r):
                    continue
                op = "2" if n == "1" else "1"
                nn = ops.name(f"{r}:{op}")
                if nn and nn not in seen:
                    seen.add(nn)
                    stack.append(nn)
        if reached:
            continue

        # 情形一：电阻另一端在 GND → 迁到电源（最常见）
        if on and has_gnd_pin(on):
            ops.attach(other_pin, supply)
            fixes.append(
                f"{led['ref']} 的限流电阻 {rref} 原接 GND 使回路无驱动，"
                f"改接电源：{supply}→{rref}→{led['ref']}→GND")
            continue

        # 情形二：电阻另一端是「仅此一脚」的孤立网（LLM 给限流电阻起了
        # LED2限流 之类网名却没接任何驱动），同样迁到电源；若这是用户灯，
        # 后面的 _fix_floating_led_gpio 会再把它从电源挪到空闲 GPIO。
        if on and len(ops.members(on)) == 1:
            ops.attach(other_pin, supply)
            fixes.append(
                f"{led['ref']} 的限流电阻 {rref} 原接孤立网 {on} 使回路无驱动，"
                f"改接电源：{supply}→{rref}→{led['ref']}→GND")
            continue

        # 情形三：电阻另一端落在「另一颗 LED 的阳极网」，而该网同样走不到
        # 驱动源——LLM 把两颗灯的限流电阻互相跨接（假电源网名如 LED1_VCC，
        # 但网内无真实电源脚）。把电阻该端迁到电源，两灯共用限流。
        # 严格判据：另一端网里至少有一颗别的 LED 阳极，且整网无驱动源。
        if not on or is_driven(on):
            continue
        other_led_anode = any(
            p.partition(":")[0] != led["ref"]
            and by_ref.get(p.partition(":")[0], {}).get("symbol", "").lower().endswith(":led")
            for p in ops.members(on))
        if not other_led_anode:
            continue
        ops.attach(other_pin, supply)
        fixes.append(
            f"{led['ref']} 的限流电阻 {rref} 原跨接到另一 LED 阳极使回路无驱动，"
            f"改接电源：{supply}→{rref}→{led['ref']}→GND")


def _fix_one_led(led, items, ops, lib, fixes):
    a = next(iter(_p(led, lib, "A")), "2")
    k = next(iter(_p(led, lib, "K")), "1")
    a_pin, k_pin = f"{led['ref']}:{a}", f"{led['ref']}:{k}"
    a_net = ops.name(a_pin)
    if not a_net or not _SUPPLY_RE.match(a_net):
        return
    anode = ops.members(a_net)
    by_ref = {it["ref"]: it for it in items}
    # 电源网里已有专属限流电阻（该网只有 LED 和一个电阻）→ 已正确
    # ops.members() 返回的是 {"R1:1", "LED1:2"} 这样的**引脚字符串集合**，
    # 不是 (ref, num) 元组。旧版 `for r, _n in anode` 会把字符串当序列解包，
    # 遇到 "R3:2" 这种 4 字符脚名就抛 ValueError: too many values to unpack。
    if (len(anode) == 2 and any(
            p.partition(":")[0] != led["ref"]
            and by_ref.get(p.partition(":")[0], {}).get("symbol", "").lower().startswith("device:r")
            for p in anode)):
        return

    # 找可作限流的电阻。两类候选（优先级从高到低）：
    #   A) 一端接芯片 GPIO、另一端空闲——改成 GPIO→R→LED→GND（可由 MCU 控制）；
    #   B) 一端接电源、另一端接 GND——这是 LLM 把限流电阻误接成「跨电源直连」，
    #      复用它改成 电源→R→LED→GND，避免新增冗余电阻、也不新增线头。
    # 候选里排除 LED 自己正负极所在的网，避免把 LED 短接。
    candidate = None          # (电阻, 接到 LED 阳极的那一端)
    candidate_kind = None
    led_other_net = ops.name(k_pin)
    for it in items:
        if not it.get("symbol", "").lower().startswith("device:r"):
            continue
        for num, other in (("1", "2"), ("2", "1")):
            n = ops.name(f"{it['ref']}:{num}")
            on = ops.name(f"{it['ref']}:{other}")
            other_members = ops.members(on) if on else set()
            # A) 接 GPIO 的一端 + 完全悬空的一端
            if (n and not _SUPPLY_RE.match(n)
                    and not _GND_RE.search(n)
                    and n != led_other_net
                    and any(":" in x and x.partition(":")[0] != led["ref"]
                            for x in ops.members(n))
                    and on and not _SUPPLY_RE.match(on)
                    and other_members == {f"{it['ref']}:{other}"}):
                candidate, candidate_kind = (it, other), "gpio"
                break
            # B) 电源端的电阻，另一端是 GND 或完全悬空，均复用为限流电阻：
            #    B1) 跨在电源与 GND 之间——LLM 把限流电阻误接成「跨电源直连」；
            #    B2) 一端接电源、另一端完全悬空——LLM 标成「限流电阻」却只接了
            #        一端的断头件（此前不匹配任何候选，导致兜底新增 R3、R2 悬空）。
            #    两类都改成 电源→R→LED→GND，避免新增冗余电阻、也不留断头件。
            # 注意：电源/GND 是公共网，此处不能要求「另一端只有它自己」。
            floating = on is None or other_members == {f"{it['ref']}:{other}"}
            if (n and _SUPPLY_RE.match(n)
                    and ((on and _GND_RE.search(on)) or floating)):
                candidate, candidate_kind = (it, num), "supply"
                break
        if candidate:
            break

    led_net = f"LED_A_{led['ref']}"        # 每个 LED 独立网络，避免互相短接
    if candidate and candidate_kind == "gpio":
        res, other = candidate
        ops.attach(a_pin, led_net)
        ops.attach(f"{res['ref']}:{other}", led_net)
        ops.attach(k_pin, "GND")
        fixes.append(f"{led['ref']} 改为 GPIO→{res['ref']}(限流)→LED→GND")
    elif candidate and candidate_kind == "supply":
        res, supply_side = candidate
        # R 的电源端保留，另一端从 GND 改接到 LED 阳极
        gnd_side = "2" if supply_side == "1" else "1"
        ops.attach(a_pin, led_net)
        ops.attach(f"{res['ref']}:{gnd_side}", led_net)
        ops.attach(k_pin, "GND")
        fixes.append(f"复用 {res['ref']} 作 {led['ref']} 的限流电阻：电源→{res['ref']}→{led['ref']}→GND")
    else:
        # 兜底：新建电阻，接到一个空闲 GPIO 网（电源→电阻→LED→GND 会使 LED 常亮，
        # 但至少不会过流；优先找一个 MCU 空闲脚驱动）
        ref = _add_ref(items, "R")
        items.append({"ref": ref, "name": "电阻（LED 限流）", "value": "1k",
                      "qty": 1, "symbol": "Device:R", "footprint": ""})
        ops.attach(a_pin, led_net)
        ops.attach(f"{ref}:2", led_net)
        ops.attach(f"{ref}:1", a_net)
        ops.attach(k_pin, "GND")
        fixes.append(f"新增 {ref}（1kΩ）串在电源与 {led['ref']} 之间限流")


def _fix_mcu_pins(items, ops, lib, fixes):
    """STM32 的 VBAT/VDDA/BOOT0：接到 3.3V 网 / BOOT0 下拉。"""
    mcu = _find(items, "stm32f1")
    if not mcu:
        return
    mcu_ref = mcu["ref"]
    v33 = _v33_net(ops, items, lib)
    if v33 is None:
        for name in ("VDD",):
            for p in _p(mcu, lib, name):
                n = ops.name(f"{mcu_ref}:{p}")
                if n:
                    v33 = n
                    break
    for name in ("VBAT", "VDDA"):
        for p in _p(mcu, lib, name):
            pin = f"{mcu_ref}:{p}"
            if ops.name(pin) is None and v33:
                ops.attach(pin, v33)
                fixes.append(f"补 {mcu_ref} 的 {name} 脚（{p}）接到 {v33}")
    for p in _p(mcu, lib, "BOOT0"):
        pin = f"{mcu_ref}:{p}"
        if ops.name(pin) is None:
            # 必须新建独立电阻，不能复用已有电阻（会把原网络抢走）
            ref = _add_ref(items, "R")
            items.append({"ref": ref, "name": "电阻 10kΩ（BOOT0 下拉）", "value": "10k",
                          "qty": 1, "symbol": "Device:R", "footprint": ""})
            ops.attach(pin, "BOOT0")
            ops.attach(f"{ref}:1", "BOOT0")
            ops.attach(f"{ref}:2", "GND")
            fixes.append(f"新增 {ref}（10kΩ）作为 {mcu_ref} 的 BOOT0 下拉")


def _shorten_connector_net_names(ops):
    """把 `J2_Pin15` 这类自动生成的长网名压成 `J2P15`。

    排针网名本身只用于连通，长名在密集标签区会互相压字；缩短不改变
    任何电气连接，但能让 20 针排针的标签真正排下。只在缩短后仍唯一
    时才改名，避免撞网名。
    """
    names = {n.get("name") for n in ops.nets}
    for net in ops.nets:
        m = re.fullmatch(r"([A-Za-z]+\d+)_Pin(\d+)", net.get("name", ""))
        if not m:
            continue
        new = f"{m.group(1)}P{m.group(2)}"
        if new != net["name"] and new not in names:
            names.discard(net["name"])
            net["name"] = new
            names.add(new)


def _gnd_net(ops) -> str:
    """取现有 GND 网名（按名字识别，兼容 GND/VSS/地）。"""
    for net in ops.nets:
        if _GND_RE.search(net.get("name", "")):
            return net["name"]
    return "GND"


def _is_mcu_item(it: dict) -> bool:
    """是否主控/SoC（含 ESP32 等 RF_Module、STM32、8051、74xx 逻辑）。

    早期只认 "mcu_" 符号前缀，漏掉 RF_Module:ESP32-WROOM-32、8051 等，
    结果 LED 直挂 ESP32 GPIO 这类功能性错误三检全绿。统一以此为唯一判据，
    _chip_vcc 覆盖 esp32/esp8266/stm32/atmega/ne555/74hc/74lvc。
    """
    return (_chip_vcc(it) is not None
            or "mcu_" in it.get("symbol", "").lower()
            or "rf_module" in it.get("symbol", "").lower()
            or "stm32f1" in it.get("symbol", "").lower())


_FP_PINCOUNT_RE = re.compile(r"(?:^|[-_])(\d+)\s*[xX]\s*(\d+)(?:$|[-_.])")


def _footprint_mismatch(items, lib) -> list[tuple[str, str, int, int]]:
    """BOM 封装针脚数明显少于符号引脚数时返回失配列表。

    LLM 常把「4 针串口模块接口」写成 CH340G 符号却保留 PinHeader_1x04 封装。
    原理图/ERC 都正常，BOM 却会让采购拿错件；按脚数做通用判据，不靠位号。
    """
    bad: list[tuple[str, str, int, int]] = []
    if lib is None:
        return bad
    for it in items:
        fp = str(it.get("footprint", "")).strip()
        sym = str(it.get("symbol", ""))
        if not fp or "connector:" in sym.lower() or "conn_01x" in sym.lower():
            continue
        m = _FP_PINCOUNT_RE.search(fp)
        if not m:
            continue
        fp_pins = int(m.group(1)) * int(m.group(2))
        try:
            sym_pins = len(lib.pin_names(sym))
        except Exception:
            continue
        if sym_pins > fp_pins:
            bad.append((it["ref"], fp, fp_pins, sym_pins))
    return bad


_DEFAULT_FOOTPRINTS = {
    "Interface_USB:CH340G": "Package_SO:SOIC-16_3.9x9.9mm_P1.27mm",
}


def _sync_footprints(items, lib, fixes: list[str]) -> None:
    """把与符号脚数冲突的 BOM 封装改成该符号的合理封装（无映射则清空）。"""
    for ref, fp, fp_pins, sym_pins in _footprint_mismatch(items, lib):
        it = next(x for x in items if x["ref"] == ref)
        sym = str(it.get("symbol", ""))
        new = _DEFAULT_FOOTPRINTS.get(sym, "")
        it["footprint"] = new
        fixes.append(
            f"{ref} 的 BOM 封装 {fp} 只有 {fp_pins} 脚、符号 {sym} 有 {sym_pins} 脚，"
            f"已改为 {new or '空（待按符号选型）'}，避免采购错件")


def _sync_uart_bom_text(items, lib, fixes: list[str]) -> None:
    """符号已是串口芯片但 BOM 仍写「模块接口」时同步名称/描述。

    LLM 常把 4 针 USB 转串口模块接口写成 CH340G 芯片；修复后符号、引脚、
    封装都按芯片处理，BOM 的 name/desc 若还写「连接模块（TX/RX/VCC/GND）」
    就会与 16 脚 SOIC 芯片自相矛盾。按符号语义判断，不靠位号。
    """
    for it in items:
        sym = str(it.get("symbol", ""))
        try:
            if lib is None or not _looks_uart_symbol(it, lib):
                continue
        except Exception:
            continue
        text = f"{it.get('name', '')} {it.get('desc', '')}"
        value = str(it.get("value", ""))
        stale_text = bool(re.search(r"模块接口|module\s*interface|连接.{0,8}模块", text, re.I))
        stale_value = bool(re.search(r"模块|接口|module|interface", value, re.I))
        if not stale_text and not stale_value:
            continue
        model = re.sub(r"(模块|接口|module|interface).*$", "", value, flags=re.I).strip()
        model = model or "USB转串口芯片"
        it["value"] = model
        it["name"] = f"USB转串口芯片（{model}）"
        it["desc"] = f"USB 转串口芯片（{model}），需配晶振与负载电容"
        fixes.append(f"{it['ref']} 的 BOM 仍写「模块接口」，已同步为 {model} 芯片描述")


_VOLT_DECL_RE = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)\s*[vV](?![A-Za-z0-9])")


def _declared_voltage(item: dict) -> float | None:
    """从 BOM 的 value/name/desc 读元件自己声明的工作电压（V）。

    典型：电源输入接口 value=5V、desc=5V电源输入；SWD 排针 desc 里虽含
    VCC 但没有数字+V，返回 None，不会被误判成电源输入。
    """
    text = " ".join(str(item.get(k, "")) for k in ("value", "name", "desc"))
    m = _VOLT_DECL_RE.search(text)
    return float(m.group(1)) if m else None


def _regulator_in_net(ops, items, lib, max_voltage: float = 4.5) -> str | None:
    """找低压稳压器的输入网（未稳压的 5V 来源）。"""
    for it in items:
        if "regulator" not in it.get("symbol", "").lower():
            continue
        v = _regulator_output(it)
        if v is None or v >= max_voltage:
            continue
        for p in _p(it, lib, "VI") or _p(it, lib, "VIN"):
            n = ops.name(f"{it['ref']}:{p}")
            if n and not _GND_RE.search(n):
                return n
    return None


def _net_rail_voltage(ops, items, lib, net: str) -> float | None:
    """推断网络电压：优先稳压器 VO/VI 的实际接线，其次网名。

    稳压器 VI 所在网是「未稳压输入」，不能因为网名带 3V3 就当成 3.3V 输出；
    这条顺序避免把 NET_3V3_IN（LDO 的 5V 输入）误判成 3.3V 网。
    """
    if not net or _GND_RE.search(net):
        return None
    for it in items:
        if "regulator" not in it.get("symbol", "").lower():
            continue
        v = _regulator_output(it)
        if v is None:
            continue
        for role in ("VO", "VOUT"):
            for p in _p(it, lib, role):
                if ops.name(f"{it['ref']}:{p}") == net:
                    return v
        for role in ("VI", "VIN"):
            for p in _p(it, lib, role):
                if ops.name(f"{it['ref']}:{p}") == net:
                    return None
    if _V33_NAME_RE.search(net):
        return 3.3
    if re.search(r"(?:^|[_+])5v(?:$|[_+])|vbus", net, re.I):
        return 5.0
    m = re.search(r"(?:^|[_+])(\d+(?:\.\d+)?)v(?:$|[_+])", net, re.I)
    return float(m.group(1)) if m else None


def _v33_rail_nets(ops, items, lib) -> set[str]:
    """所有 3.3V 稳压器输出网（真 3.3V 电源域）。"""
    out: set[str] = set()
    for it in items:
        v = _regulator_output(it)
        if v is None or abs(v - 3.3) >= 0.2:
            continue
        for role in ("VO", "VOUT"):
            for p in _p(it, lib, role):
                n = ops.name(f"{it['ref']}:{p}")
                if n:
                    out.add(n)
    return out


def _fix_power_input_connector(items, ops, lib, fixes: list[str]) -> None:
    """声明 5V 的电源输入接口误接 3.3V 输出网时，改接稳压器未稳压输入。

    ERC 只检查引脚电气类型，网名 VCC 也常被当成普通电源，因此「外部 5V
    直接灌进 3.3V 域」可以五维全绿却烧芯片。判据不看位号：任何连接器只要
    BOM 明确声明 ≥4.5V，且其脚落在 3.3V 稳压器输出网，就迁到稳压器输入网。
    """
    raw = _regulator_in_net(ops, items, lib)
    if not raw:
        raw = _usb_vbus_net(ops, items, lib)
    if not raw:
        return
    v33 = _v33_rail_nets(ops, items, lib)
    if not v33:
        return
    for it in items:
        sym = str(it.get("symbol", "")).lower()
        if not ("connector:" in sym or "conn_01x" in sym):
            continue
        declared = _declared_voltage(it)
        if declared is None or declared < 4.5:
            continue
        ref = it["ref"]
        try:
            nums = [str(n) for n in (lib.pin_names(it["symbol"]) if lib else {})]
        except Exception:
            nums = []
        if not nums:
            nums = ["1", "2"]
        for num in nums:
            pin = f"{ref}:{num}"
            net = ops.name(pin)
            if not net or _GND_RE.search(net) or net == raw:
                continue
            if net not in v33 and _net_rail_voltage(ops, items, lib, net) != 3.3:
                continue
            ops.attach(pin, raw)
            fixes.append(
                f"{ref} 标注 {declared:g}V 电源输入，{pin} 原接 3.3V 网 {net}，"
                f"已改接稳压器输入网 {raw}，避免外部 {declared:g}V 直接灌入 3.3V 芯片")


def _usb_vbus_net(ops, items, lib) -> str | None:
    """找 USB VBUS 的 5V 来源；没有真实来源时返回 None，避免凭空造电源。"""
    for it in items:
        sym = it.get("symbol", "").lower()
        if "conn_01x05" in sym or "usb_micro" in sym or "usb_type" in sym:
            vbus = _connector_vbus_pin(ops, it, lib)
            n = ops.name(f"{it['ref']}:{vbus}") if vbus is not None else None
            if n and not _GND_RE.search(n):
                return n
    for net in ops.nets:
        if re.fullmatch(r"(\+?5v|vbus|usb_vbus)", net.get("name", ""), re.I):
            return net["name"]
    for it in items:
        vout = _regulator_output(it)
        if vout is None:
            continue
        if vout >= 4.5:
            for p in _p(it, lib, "VO"):
                n = ops.name(f"{it['ref']}:{p}")
                if n:
                    return n
        elif abs(vout - 3.3) < 0.2:
            # 网名很笼统（VCC）时的最后兜底：3.3V 稳压器的输入端就是外部
            # 电压来源，可以拿来给 USB VBUS 供电；此时逻辑 VDD 已被
            # `_fix_power` 迁到稳压器输出，不会再有 5V 灌入芯片的问题。
            for p in _p(it, lib, "VI"):
                n = ops.name(f"{it['ref']}:{p}")
                if n and not _GND_RE.search(n):
                    return n
    return None


def _ensure_usb_connector(items, ops, lib, ur, fixes):
    """UART 在但 USB 口被错换成 UART 本体时，补回物理连接器。

    `_fix_uart_symbol` 会把 value=CH340G、symbol=连接器的项就地换成 CH340G；
    这会让电路里只剩串口芯片、没有 USB 口，D+/D− 无处可接。这里按通用判据
    补一个 5 针 USB 连接器，并接到已有 5V/GND，不凭空创建电源网。
    """
    usb = next((it for it in items
                if "conn_01x05" in it.get("symbol", "").lower()
                or "usb_micro" in it.get("symbol", "").lower()), None)
    if usb:
        return usb
    vbus = _usb_vbus_net(ops, items, lib)
    if not vbus:
        return None
    ref = _add_ref(items, "J")
    usb = {"ref": ref, "name": "USB 接口（Micro-B）", "value": "USB-Micro-B",
           "qty": 1, "symbol": "Connector:Conn_01x05_Pin", "footprint": "USB_Micro-B",
           "desc": "USB 5V/D+/D− 输入"}
    items.append(usb)
    d_p = next(iter(_p(usb, lib, "D+")), "3")
    d_m = next(iter(_p(usb, lib, "D-")), "2")
    gnd_p = next(iter(_p(usb, lib, "GND")), "1")
    vbus_p = next(iter(_p(usb, lib, "VBUS")), "5")
    ops.attach(f"{ref}:{gnd_p}", _gnd_net(ops))
    ops.attach(f"{ref}:{d_p}", "USB_D+")
    ops.attach(f"{ref}:{d_m}", "USB_D-")
    ops.attach(f"{ref}:{vbus_p}", vbus)
    fixes.append(f"补回物理 USB 连接器 {ref}（{ref}:{vbus_p}→{vbus}，"
                 f"{ref}:{gnd_p}→GND，D+/D−→{ur}）")
    return usb


def _has_load_cap(ops, items, net: str, gnd: str) -> bool:
    """net 上是否已有 pF 级电容另一端接 GND。"""
    for it in items:
        sym = it.get("symbol", "").lower()
        if not (sym.endswith(":c") or "cap" in sym or "电容" in str(it.get("name", ""))):
            continue
        if "pf" not in str(it.get("value", "")).lower():
            continue
        for p, other in (("1", "2"), ("2", "1")):
            if ops.name(f"{it['ref']}:{p}") == net and ops.name(f"{it['ref']}:{other}") == gnd:
                return True
    return False


def _usb_data_pins(usb, lib):
    """USB 连接器的 D+/D− 脚号：优先符号引脚名，通用 5 针连接器用 Micro-B 物理脚位。"""
    pm = {}
    if lib:
        try:
            pm = {str(k): v for k, v in lib.pin_names(usb.get("symbol", "")).items()}
        except Exception:
            pm = {}
    d_p = [p for p, n in pm.items() if n in ("D+", "USB_D+")]
    d_m = [p for p, n in pm.items() if n in ("D-", "USB_D-")]
    if not d_p or not d_m:
        sym = usb.get("symbol", "").lower()
        if ("conn_01x05" in sym or "usb_micro" in sym
                or "usb_type" in sym or "usb-a" in sym):
            # Micro-B / Type-A 通用 5 针：VBUS=1, D−=2, D+=3, ID=4, GND=5。
            d_p, d_m = (d_p or ["3"]), (d_m or ["2"])
    return next(iter(d_p), "3"), next(iter(d_m), "2")


def _split_shared_crystal(items, ops, lib, fixes) -> None:
    """修复「一颗晶振同时挂在 MCU 主时钟和 UART 的 XI/XO 上」。

    LLM 常生成两颗晶振（8MHz 给 MCU、12MHz 给 USB 转串口），却把它们接成
    同一对网：``Y1_OSC = [U1:OSC_IN, Y2:1, U2:XI, C5:1]``，而真正的 MCU
    晶振 Y1 两脚全悬空。ERC/视觉/安全都不报（脚都在网上），只有功能自检
    识别「主晶振悬空」。两个芯片共用一颗晶振、频率也对不上，主 MCU 无法起振。

    判据（严格，保守）：
      - 板上恰好有两颗 Device:Crystal，且一颗全部悬空、另一颗跨接两条网；
      - 这两条网里同时含 MCU 的时钟脚（OSC_IN/OSC_OUT/PD0/PD1/PB6 等）
        与 UART 的 XI/XO 脚——即两芯片确实被并到同一对晶振网；
      - 悬空晶振频率低于已接晶振（8MHz<12MHz，主时钟应低于 UART 晶振时
        才交换；若无法解析频率则跳过，不猜）。
    修法：把 MCU 时钟脚和它的负载电容从这个共用网摘出，接到悬空晶振上；
    UART 的 XI/XO 与已接晶振留在原网，再由 _fix_usb 补负载电容。
    """
    crystals = [it for it in items
                if "crystal" in it.get("symbol", "").lower()]
    if len(crystals) != 2:
        return

    def mhz(it):
        m = re.search(r"(\d+(?:\.\d+)?)\s*mhz", str(it.get("value", "")), re.I)
        return float(m.group(1)) if m else None

    floating = [c for c in crystals
                if not ops.name(f"{c['ref']}:1") and not ops.name(f"{c['ref']}:2")]
    wired = [c for c in crystals if c not in floating]
    if len(floating) != 1 or len(wired) != 1:
        return
    slow, fast = floating[0], wired[0]
    fs, ff = mhz(slow), mhz(fast)
    if fs is None or ff is None or not (fs < ff):
        return

    n1, n2 = ops.name(f"{fast['ref']}:1"), ops.name(f"{fast['ref']}:2")
    if not n1 or not n2 or n1 == n2:
        return
    by_ref = {it["ref"]: it for it in items}
    mcu_refs, uart_refs = set(), set()
    for it in items:
        if _is_mcu_item(it):
            mcu_refs.add(it["ref"])
        if _looks_uart_symbol(it, lib):
            uart_refs.add(it["ref"])
    if not mcu_refs or not uart_refs:
        return
    # 两条网上必须同时出现 MCU 时钟脚和 UART 的 XI/XO
    def has(net, refs):
        return any(p.split(":", 1)[0] in refs for p in ops.members(net))

    if not (has(n1, mcu_refs) and has(n2, mcu_refs)
            and has(n1, uart_refs) and has(n2, uart_refs)):
        return

    # MCU 时钟脚的引脚号（按符号引脚名，不硬编码）
    mcu_clk_pins = {}
    for ref in mcu_refs:
        it = by_ref[ref]
        if not lib:
            break
        for num, nm in lib.pin_names(it.get("symbol", "")).items():
            if any(k in str(nm).upper()
                   for k in ("OSC_IN", "OSC_OUT", "OSCI", "OSCO",
                             "XTAL1", "XTAL2", "PD0", "PD1")):
                mcu_clk_pins[f"{ref}:{num}"] = True
    if not mcu_clk_pins:
        return

    slow_n1, slow_n2 = f"{slow['ref']}_OSC", f"{slow['ref']}_OSC2"
    ops.ensure(slow_n1)
    ops.ensure(slow_n2)
    # 把 MCU 时钟脚和它们的 pF 负载电容从共用网迁到低频晶振网；
    # UART 的 XI/XO 留在原网，继续用高频晶振。
    for old, target in ((n1, slow_n1), (n2, slow_n2)):
        for pin in list(ops.members(old)):
            ref, _, num = pin.partition(":")
            it = by_ref.get(ref, {})
            is_clk = pin in mcu_clk_pins
            is_cap = ("device:c" in it.get("symbol", "").lower()
                      and "pf" in str(it.get("value", "")).lower())
            if is_clk or is_cap:
                ops.attach(pin, target)
    ops.attach(f"{slow['ref']}:1", slow_n1)
    ops.attach(f"{slow['ref']}:2", slow_n2)
    fixes.append(
        f"{fast['ref']}({fast.get('value','')}) 被 MCU 与 UART 共用；"
        f"已将 MCU 时钟与负载电容迁到悬空的 {slow['ref']}({slow.get('value','')})")


def _is_zero_ohm(item) -> bool:
    """是否 0Ω 电阻/跳线（用于两个电源输出等电位连接且避免 ERC 冲突）。"""
    val = str(item.get("value", "")).strip().lower().replace(" ", "")
    return bool(re.fullmatch(r"0(?:r|ohms?|Ω)?", val, re.I))


def _net_voltage_class(items, ops, lib, net_name: str) -> str:
    """判断网络的供电档位：3v3 / 5v / unknown（unknown 时保守不动 V3）。"""
    if not net_name:
        return "unknown"
    if _V33_NAME_RE.search(net_name) or re.search(r"\bvdd(?:33)?\b", net_name, re.I):
        return "3v3"
    if re.search(r"(?:\b5v\b|\+5\b|vbus|usb5)", net_name, re.I):
        return "5v"
    by_ref = {it["ref"]: it for it in items}
    for pin in ops.members(net_name):
        ref, _, num = pin.partition(":")
        it = by_ref.get(ref)
        if not it:
            continue
        v = _regulator_output(it)
        if v is not None:
            vo = next(iter(_p(it, lib, "VO") or _p(it, lib, "VOUT") or ["2"]))
            if num == vo:
                if abs(v - 3.3) < 0.2:
                    return "3v3"
                if abs(v - 5.0) < 0.5:
                    return "5v"
        if _is_mcu_item(it) and any(num in _p(it, lib, n)
                                    for n in ("VDD", "VDDA", "VBAT")):
            return "3v3"
    return "unknown"


def _fix_usb(items, ops, lib, fixes):
    """CH340G + Micro-USB 时修正 D+/D− 连接、XI/XO 晶振与负载电容。

    旧实现只在同一条 UD+/UD− 网**已经含USB连接器引脚**时才对调，且只要
    XI/XO 网各有两个成员就算「有晶振」——两个负载电容刚好凑够 2 个，
    于是 USB 数据脚整条悬空、晶振缺失都能通过。这里改成按电气实质判断。
    """
    uart = (_find(items, "ch340") or _find(items, "cp210")
            or _find(items, "ft232") or _find(items, "pl2303"))
    if not uart:
        return
    usb = _ensure_usb_connector(items, ops, lib, uart["ref"], fixes)
    if not usb:
        # 没有 USB 口也没有 5V 来源时不硬造电源；但仍需给 UART 补晶振，
        # 否则即使后来外接串口线也无法工作。
        # 注意：这是「未做」的说明而非拓扑修正，不能写进 fixes，
        # 否则 fix_topology 永不幂等，全量审计每次都会误报一个待修项。
        pass
    by_ref = {it["ref"]: it for it in items}
    ur = uart["ref"]
    gnd = _gnd_net(ops)

    # ① USB D+/D−：必须真的落到 UART 的 UD+/UD− 网上；交叉则对调，
    #    并把被 LLM 误接到这两条线上的 MCU USB 引脚摘掉（保留电阻等无源件）。
    ud_p = next(iter(_p(uart, lib, "UD+") or _p(uart, lib, "D+")), "5")
    ud_m = next(iter(_p(uart, lib, "UD-") or _p(uart, lib, "D-")), "6")
    if usb:
        usr = usb["ref"]
        d_p, d_m = _usb_data_pins(usb, lib)
        usb_p, usb_m = f"{usr}:{d_p}", f"{usr}:{d_m}"
        dp, dm = ops.find(f"{ur}:{ud_p}"), ops.find(f"{ur}:{ud_m}")
        dp_name = dp["name"] if dp else "USB_D+"
        dm_name = dm["name"] if dm else "USB_D-"
        swapped = bool(dp and dm and usb_p in dm["pins"] and usb_m in dp["pins"])
        missing = (usb_p not in (dp["pins"] if dp else set())
                   or usb_m not in (dm["pins"] if dm else set()))
        if swapped or missing:
            tgt_p, tgt_m = (dm_name, dp_name) if swapped and not missing else (dp_name, dm_name)
            # UART 侧引脚本身也必须放进目标网：LLM 有时把 UD+/UD− 完全留空，
            # 只接 USB 口仍不通（105400a8 就是这种）。
            ops.attach(f"{ur}:{ud_p}", tgt_p)
            ops.attach(f"{ur}:{ud_m}", tgt_m)
            ops.attach(usb_p, tgt_p)
            ops.attach(usb_m, tgt_m)
            fixes.append(f"USB D+/D− 接入 {ur}：{usb_p}→{tgt_p}，{usb_m}→{tgt_m}")
        # 清掉 LLM 误接到 USB 数据线上的 MCU 引脚；无源串阻/保护件保留。
        dp_name = next((n["name"] for n in ops.nets if usb_p in n["pins"]), dp_name)
        dm_name = next((n["name"] for n in ops.nets if usb_m in n["pins"]), dm_name)
        for netname in (dp_name, dm_name):
            net = next((n for n in ops.nets if n["name"] == netname), None)
            if not net:
                continue
            bad = [p for p in net["pins"]
                   if p.split(":", 1)[0] not in (ur, usr)
                   and _is_mcu_item(by_ref.get(p.split(":", 1)[0], {}))]
            for p in bad:
                net["pins"].remove(p)
            if bad:
                fixes.append(f"从 USB 数据网 {netname} 移除误接的 MCU 引脚：{', '.join(bad)}")
        # 统一网名，避免残留 PA11/PA12 这类误导性网络名。
        for pin, new in ((usb_p, "USB_D+"), (usb_m, "USB_D-")):
            net = next((n for n in ops.nets if pin in n["pins"]), None)
            if net and net["name"] != new and not any(n["name"] == new for n in ops.nets):
                net["name"] = new

    # ② XI/XO：只看「是否有晶振本体跨接两网」，不能再被两个负载电容蒙混。
    xi_names, xo_names = _p(uart, lib, "XI"), _p(uart, lib, "XO")
    if xi_names and xo_names:
        xi, xo = next(iter(xi_names)), next(iter(xo_names))
        xi_net, xo_net = ops.name(f"{ur}:{xi}"), ops.name(f"{ur}:{xo}")
        # LLM（或旧版规则反复补晶振）会把 XI/XO 并进同一条网，并把多颗晶振的
        # 两脚都挂上去：每颗晶振被自己短接，振荡回路根本不存在。此时若按
        # 「没有跨接两网的晶振」继续新增，只会一次次造出更多短路晶振。
        # 先把 XI/XO 拆成两条网，保留一颗晶振跨接，删掉重复件，负载电容平分。
        if xi_net and xi_net == xo_net:
            short_net = xi_net
            xtals = [it for it in items
                     if "crystal" in str(it.get("symbol", "")).lower()
                     and ops.name(f"{it['ref']}:1") == short_net
                     and ops.name(f"{it['ref']}:2") == short_net]
            keep = next((x for x in xtals if "12m" in str(x.get("value", "")).lower()),
                        xtals[0] if xtals else None)
            dup = [x for x in xtals if x is not keep]
            for x in dup:
                ops.remove_component(x["ref"], [f"{x['ref']}:1", f"{x['ref']}:2"])
            items[:] = [x for x in items if x not in dup]
            if dup:
                fixes.append("删除重复且两脚短路的晶振 "
                             + "、".join(x["ref"] for x in dup))
            taken = {net["name"] for net in ops.nets}
            xi_new, xo_new = f"{short_net}_XI", f"{short_net}_XO"
            for suffix in ("_2", "_3", "_4", "_5"):
                if xi_new not in taken:
                    break
                xi_new = f"{short_net}_XI{suffix}"
            for suffix in ("_2", "_3", "_4", "_5"):
                if xo_new not in taken:
                    break
                xo_new = f"{short_net}_XO{suffix}"
            ops.attach(f"{ur}:{xi}", xi_new)
            ops.attach(f"{ur}:{xo}", xo_new)
            if keep is not None:
                ops.attach(f"{keep['ref']}:1", xi_new)
                ops.attach(f"{keep['ref']}:2", xo_new)
            caps = [it for it in items
                    if str(it.get("symbol", "")).lower().startswith("device:c")
                    and "pf" in str(it.get("value", "")).lower()
                    and (ops.name(f"{it['ref']}:1") == short_net
                         or ops.name(f"{it['ref']}:2") == short_net)]
            for i, cap in enumerate(caps):
                target = xi_new if i % 2 == 0 else xo_new
                for p in ("1", "2"):
                    if ops.name(f"{cap['ref']}:{p}") == short_net:
                        ops.attach(f"{cap['ref']}:{p}", target)
            fixes.append(
                f"{ur} 的 XI/XO 原被并在同一网 {short_net}（晶振两脚短路、不起振），"
                f"已拆为 {xi_new}/{xo_new}"
                + (f"，保留 {keep['ref']} 跨接" if keep else "，等待补晶振"))
            xi_net, xo_net = xi_new, xo_new
        has_xtal = False
        if xi_net and xo_net and xi_net != xo_net:
            for it in items:
                if "crystal" not in it.get("symbol", "").lower():
                    continue
                a, b = ops.name(f"{it['ref']}:1"), ops.name(f"{it['ref']}:2")
                if a and b and {a, b} == {xi_net, xo_net}:
                    has_xtal = True
                    break
        if not has_xtal:
            # 必须新建独立晶振，不能复用 MCU 的晶振（两个芯片不能共用）
            xi_name = xi_net or "XTALI"
            xo_name = xo_net or "XTALO"
            ref = _add_ref(items, "Y")
            items.append({"ref": ref, "name": "晶振 12MHz（USB 转串口）", "value": "12MHz",
                          "qty": 1, "symbol": "Device:Crystal", "footprint": ""})
            ops.attach(f"{ur}:{xi}", xi_name)
            ops.attach(f"{ur}:{xo}", xo_name)
            ops.attach(f"{ref}:1", xi_name)
            ops.attach(f"{ref}:2", xo_name)
            xi_net, xo_net = xi_name, xo_name
            fixes.append(f"新增 {ref}（12MHz 晶振）给 {ur} 的 XI/XO，USB 转串口可起振")
        # 无论晶振是原有还是新增，两脚都必须有 pF 负载电容到 GND。
        # 旧实现只在「新增晶振」时补电容，遇到「已有晶振但无负载」会漏掉。
        for xnet in (xi_net, xo_net):
            if _has_load_cap(ops, items, xnet, gnd):
                continue
            cref = _add_ref(items, "C")
            items.append({"ref": cref, "name": "电容（UART 晶振负载）", "value": "22pF",
                          "qty": 1, "symbol": "Device:C", "footprint": ""})
            ops.attach(f"{cref}:1", xnet)
            ops.attach(f"{cref}:2", gnd)
            fixes.append(f"新增 {cref}（22pF）作为 UART 晶振负载电容")

        # ③ XI/XO 负载电容另一端必须接 GND；若错接到 V3/电源，纠正。
        for it in items:
            sym_l = it.get("symbol", "").lower()
            if not (sym_l.endswith(":c") or "cap" in sym_l or "电容" in it.get("name", "")):
                continue
            if "pf" not in str(it.get("value", "")).lower():
                continue
            for p, other in (("1", "2"), ("2", "1")):
                if ops.name(f"{it['ref']}:{p}") not in (xi_net, xo_net):
                    continue
                cur = ops.name(f"{it['ref']}:{other}")
                if not cur or not _GND_RE.search(cur):
                    ops.attach(f"{it['ref']}:{other}", gnd)
                    fixes.append(f"{it['ref']}（{it.get('value','')}）从 {cur or '悬空'} 改接 GND，作为 XI/XO 负载电容")

    # ④ UART 的 VCC / V3 供电处理（CH340G 数据手册）：
    #    VCC=5V（VBUS）时 V3 是内部 3.3V LDO 的旁路点 → 独立 100nF 到 GND；
    #    VCC=3.3V 时 V3 必须与 VCC 短接，悬空只挂电容会导致串口不工作。
    vcc_pins = _p(uart, lib, "VCC")
    logic_vdd = None
    for it in items:
        if (v := _regulator_output(it)) is not None and abs(v - 3.3) < 0.2:
            vo = _p(it, lib, "VO") or _p(it, lib, "VOUT") or ["2"]
            logic_vdd = ops.name(f"{it['ref']}:{vo[0]}")
            if logic_vdd:
                break
    if not logic_vdd:
        for it in items:
            if not _is_mcu_item(it):
                continue
            for vp in _p(it, lib, "VDD"):
                nn = ops.name(f"{it['ref']}:{vp}")
                if nn:
                    logic_vdd = nn
                    break
            if logic_vdd:
                break
    vcc_pin = f"{ur}:{vcc_pins[0]}" if vcc_pins else None
    vcc_net = (ops.name(vcc_pin) if vcc_pin else None) or logic_vdd
    vcc_class = _net_voltage_class(items, ops, lib, vcc_net)

    v3p = next(iter(_p(uart, lib, "V3")), None)
    if v3p:
        v3_pin = f"{ur}:{v3p}"
        v3_net = ops.name(v3_pin)
        if vcc_class == "3v3" and vcc_net:
            bridged = False
            for it in items:
                if (not it.get("symbol", "").lower().startswith("device:r")
                        or not _is_zero_ohm(it)):
                    continue
                if {ops.name(f"{it['ref']}:1"),
                    ops.name(f"{it['ref']}:2")} == {v3_net, vcc_net}:
                    bridged = True
                    break
            if not bridged:
                if not v3_net:
                    # V3 脚整脚漏在网表外时，0Ω 电阻的一端不能悬空：
                    # 先建独立 V3 网并把芯片脚放进去，再用 0Ω 接 VCC。
                    v3_net = f"{ur}_V3"
                    k = 2
                    while any(n.get("name") == v3_net for n in ops.nets):
                        v3_net = f"{ur}_V3_{k}"
                        k += 1
                    ops.attach(v3_pin, v3_net)
                    fixes.append(f"{ur} 的 V3 脚原悬空，已建立独立网 {v3_net}")
                rref = _add_ref(items, "R")
                items.append({"ref": rref, "name": "电阻（V3-VCC 0Ω 等电位）",
                              "value": "0Ω", "qty": 1,
                              "symbol": "Device:R", "footprint": ""})
                if v3_net == vcc_net:
                    # 旧版直接并到 VCC（ERC 会报两个电源输出冲突）：
                    # 新建独立 V3 网，把 V3 及其退耦电容迁回，再用 0Ω 连接。
                    base = f"{ur}_V3"
                    v3_net = base
                    k = 2
                    while ops.members(v3_net):
                        v3_net = f"{base}_{k}"
                        k += 1
                    ops.attach(v3_pin, v3_net)
                    for p in list(ops.members(vcc_net)):
                        if p == v3_pin:
                            continue
                        pref = p.partition(":")[0]
                        pit = next((x for x in items if x.get("ref") == pref), None)
                        if pit and ("v3" in f"{pit.get('name', '')} {pit.get('desc', '')}".lower()
                                    and ("device:c" in pit.get("symbol", "").lower()
                                         or "cap" in pit.get("symbol", "").lower())):
                            ops.attach(p, v3_net)
                ops.attach(f"{rref}:1", v3_net)
                ops.attach(f"{rref}:2", vcc_net)
                fixes.append(
                    f"{ur} 的 V3 在 3.3V 供电时必须与 VCC 等电位；"
                    f"已通过 0Ω 电阻 {rref} 连接（避免两个电源输出直接相冲）")
        else:
            if not v3_net:
                v3_net = f"{ur}_V3"
                ops.attach(v3_pin, v3_net)
                fixes.append(f"为 {ur} 的 V3 建立独立退耦网 {v3_net}")
            has_v3_cap = False
            for it in items:
                sym = it.get("symbol", "").lower()
                val = str(it.get("value", "")).lower()
                if not (sym.endswith(":c") or "cap" in sym
                        or "电容" in str(it.get("name", ""))):
                    continue
                if "n" not in val and "u" not in val:
                    continue
                for p, other in (("1", "2"), ("2", "1")):
                    if (ops.name(f"{it['ref']}:{p}") == v3_net
                            and ops.name(f"{it['ref']}:{other}") == gnd):
                        has_v3_cap = True
            if not has_v3_cap:
                ref = _add_ref(items, "C")
                items.append({"ref": ref, "name": "电容（V3 退耦）", "value": "100nF",
                              "qty": 1, "symbol": "Device:C", "footprint": ""})
                ops.attach(f"{ref}:1", v3_net)
                ops.attach(f"{ref}:2", gnd)
                fixes.append(f"新增 {ref}（100nF）为 {ur} 的 V3 退耦")

    # ⑤ UART 的 GND 脚必须真接到 GND 网。LLM 常把 TXD/RXD/D±/晶振都接对，
    #    却漏掉 GND（悬空），芯片无参考地、整条 USB 转串口不工作；ERC 视
    #    悬空输入合法、视觉看线是连的，三检全绿。按符号真实引脚名定位 GND，
    #    悬空或错接到非 GND 网时改接。
    gnd_pins = _p(uart, lib, "GND")
    if not gnd_pins:
        gnd_pins = ["1"]          # CH340G/CP210x 通用回退：GND=1
    for gp in gnd_pins:
        gpin = f"{ur}:{gp}"
        cur = ops.name(gpin)
        if cur == gnd:
            continue
        ops.attach(gpin, gnd)
        fixes.append(f"{ur} 的 GND 脚 {gpin} 原"
                     f"{'悬空' if not cur else '误接 ' + cur}，已接到 GND")

    # ⑥ UART 的 VCC 供电脚悬空 → 接到板上 3.3V 逻辑电源。
    #    LLM 常把 CH340G 的 TXD/RXD/D±/晶振/GND 都接对，唯独漏掉 VCC(pin16)，
    #    芯片无供电、USB 转串口完全不工作；ERC/视觉/safety 全绿（悬空输入合法、
    #    safety 旧版只查 MCU/稳压器供电脚）。逻辑电源优先取 3.3V 稳压器输出网，
    #    其次取 MCU 的 VDD 网；找不到就不硬造，避免接到 5V/VBUS。
    # vcc_pins / logic_vdd 已在 ④ 中按符号真实引脚名算好，绝不硬编码 pin 号：
    # CH340G 的 VCC=16，但 CP2102 的 16 脚是 WAKEUP/GPIO.3，硬编码会接错。
    if logic_vdd:
        for vp in vcc_pins:
            vpin = f"{ur}:{vp}"
            if not ops.name(vpin):
                ops.attach(vpin, logic_vdd)
                fixes.append(f"{ur} 的 VCC 供电脚 {vpin} 原悬空，已接到 {logic_vdd}")


# USB-UART 芯片型号关键词：value/name 含这些词，但 symbol 却不是 UART 符号时，
# 说明 LLM 把型号写对了、symbol 配错了（如 CP2102 配成稳压器 ADP3336），
# 导致 TXD/RXD 接到电源输出脚等 ERC 违规。需换符号并按网络语义重映射引脚。
_UART_MODEL_KW = ("cp210", "ch34", "ft232", "pl2303", "usb-uart", "usb转串口")
_UART_PIN_ROLES = ("UD+", "UD-", "TXD", "RXD", "D+", "D-", "V3")


def _looks_uart_symbol(item, lib) -> bool:
    """符号引脚里是否含 UART 特征脚（UD+/UD-/TXD/RXD/D+/D-/V3）。"""
    if lib is None:
        return False
    sym = item.get("symbol", "")
    if not sym:
        return False
    try:
        names = set(str(n) for n in lib.pin_names(sym).values())
    except Exception:
        return False
    return bool(names & set(_UART_PIN_ROLES))


def _mcu_uart_role(pin_name: str, net_name: str) -> str | None:
    """按引脚名/网名判断 MCU 脚是串口发送(TX)还是接收(RX)。

    引脚名的 UART 语义优先于网名：LLM 常把网线命名成 UART_TX，
    却把 MCU 的 RX(PA10) 和 UART 的 RXD 都塞进去；若把网名一起混判，
    同时含 TX/RX 会判不出来，交叉修复就永远漏掉。不绑定位号，
    PA9/PA10 仅作为 STM32F1 数据手册常识回退。
    """
    p = str(pin_name or "").upper()
    if _UART_MCU_TX_RE.search(p) or p == "PA9":
        return "TX"
    if _UART_MCU_RX_RE.search(p) or p == "PA10":
        return "RX"
    s = str(net_name or "").upper()
    has_tx, has_rx = "TX" in s, "RX" in s
    if has_tx and not has_rx:
        return "TX"
    if has_rx and not has_tx:
        return "RX"
    return None


# MCU 上的 UART 功能脚：KiCad 的 alternate 名形如
# "PA9 TIM1_CH2 USART1_TX" / "PA10 TIM1_CH3 USART1_RX"。
# 只认 USART/UART 语义或明确的 TXD/RXD，不把 CAN_TX / USB_DP 当串口。
_UART_MCU_TX_RE = re.compile(r"(?:usart|uart)\s*\d*\s*[_-]?\s*tx\b|\btxd\b", re.I)
_UART_MCU_RX_RE = re.compile(r"(?:usart|uart)\s*\d*\s*[_-]?\s*rx\b|\brxd\b", re.I)


def _mcu_uart_pin_candidates(item, lib) -> dict[str, list[tuple[int, str, str]]]:
    """返回 MCU 上按 USART 编号排序的 TX/RX 候选脚：[(优先级, 脚号, 引脚名)]。"""
    out: dict[str, list[tuple[int, str, str]]] = {"TX": [], "RX": []}
    if lib is None:
        return out
    try:
        if hasattr(lib, "pin_names_with_alts"):
            names = lib.pin_names_with_alts(item.get("symbol", ""))
        else:
            names = lib.pin_names(item.get("symbol", ""))
    except Exception:
        return out
    for num, nm in names.items():
        text = str(nm).upper()
        n = str(num)
        order = re.search(r"usart\s*(\d+)", text, re.I)
        prio = int(order.group(1)) if order else 99
        for role, pat in (("TX", _UART_MCU_TX_RE), ("RX", _UART_MCU_RX_RE)):
            if pat.search(text):
                out[role].append((prio, n, text))
    for role in out:
        out[role].sort(key=lambda x: (x[0], int(x[1]) if x[1].isdigit() else 9999))
    return out


def _fix_uart_missing_link(items, ops, lib, fixes: list[str]) -> None:
    """UART 的 TXD/RXD 整对漏接（或只接了一根）时，补到 MCU 的 USART 脚。

    旧版 `_fix_uart_txrx_cross` 只在 TXD/RXD 已经接到 MCU 且方向接反时才对调；
    若 LLM 干脆把 TXD/RXD 全部漏掉，MCU 侧 PA9/PA10 各自成为单脚网名，
    ERC/视觉/安全/功能全绿，但 USB 转串口完全不通。这里按符号引脚名定位
    双方的串口脚，只连接「悬空或仅自身单脚网」的引脚，避免劫持已复用网络。
    """
    if lib is None:
        return
    uarts = [it for it in items if _looks_uart_symbol(it, lib)]
    mcus = [it for it in items if _is_mcu_item(it)]
    if not uarts or not mcus:
        return
    for uart in uarts:
        tx_pins = _p(uart, lib, "TXD") or _p(uart, lib, "TX")
        rx_pins = _p(uart, lib, "RXD") or _p(uart, lib, "RX")
        if not tx_pins or not rx_pins:
            continue
        handled = 0
        for mcu in mcus:
            cands = _mcu_uart_pin_candidates(mcu, lib)
            if not cands["TX"] or not cands["RX"]:
                continue

            def dangling_mcu(role: str):
                for _, num, nm in cands[role]:
                    pin = f"{mcu['ref']}:{num}"
                    net = ops.name(pin)
                    if net is not None and len(ops.members(net)) > 1:
                        continue       # 已被别的电路复用，绝不抢占
                    return pin, net
                return None, None

            targets: dict[str, tuple[str, str]] = {}
            for u_role, m_role in (("TX", "RX"), ("RX", "TX")):
                up = f"{uart['ref']}:{next(iter(tx_pins if u_role == 'TX' else rx_pins))}"
                u_net = ops.name(up)
                if u_net is not None and len(ops.members(u_net)) > 1:
                    continue           # UART 这根已经接好，不动
                mp, m_net = dangling_mcu(m_role)
                if not mp:
                    continue
                targets[m_role] = (up, m_net or f"UART_{m_role}", u_net)
                if m_net is None:
                    ops.attach(mp, targets[m_role][1])
            if not targets:
                continue
            # 两个角色不能落到同一个网（防重复脚/误判）
            if len({v[1] for v in targets.values()}) != len(targets):
                continue
            for m_role, (up, target, old_net) in targets.items():
                ops.attach(up, target)
                fixes.append(
                    f"{uart['ref']} 的 {'TXD' if m_role == 'RX' else 'RXD'} 原"
                    f"{'悬空' if not old_net else '仅单脚网 ' + old_net}，已接到 "
                    f"{mcu['ref']} 的串口{'接收' if m_role == 'RX' else '发送'}脚 {target}")
                handled += 1
        if handled:
            continue


def _fix_uart_txrx_cross(items, ops, lib, fixes: list[str]) -> None:
    """UART 芯片 TXD/RXD 与 MCU 的 TX/RX 直连（TX→TX、RX→RX）时交叉。

    LLM 常把 CH340G 的 TXD 接到 MCU 的 TX(PA9)、RXD 接到 RX(PA10)——
    两边都在「发」，串口收发都不通。ERC/视觉/safety/原功能自检全绿。
    按符号引脚名定位 UART 的 TXD/RXD，按 _mcu_uart_role 判定 MCU 侧角色；
    只有当 TXD 落到 MCU 的 TX 网、RXD 落到 RX 网时才对调，幂等。
    """
    if lib is None:
        return
    uarts = [it for it in items if _looks_uart_symbol(it, lib)]
    mcus = [it for it in items if _is_mcu_item(it)]
    if not uarts or not mcus:
        return
    by_ref = {it["ref"]: it for it in items}
    for uart in uarts:
        txd_pins = _p(uart, lib, "TXD") or _p(uart, lib, "TX")
        rxd_pins = _p(uart, lib, "RXD") or _p(uart, lib, "RX")
        if not txd_pins or not rxd_pins:
            continue
        tx_pin = f"{uart['ref']}:{next(iter(txd_pins))}"
        rx_pin = f"{uart['ref']}:{next(iter(rxd_pins))}"
        tx_net, rx_net = ops.name(tx_pin), ops.name(rx_pin)
        if not tx_net or not rx_net or tx_net == rx_net:
            continue

        def mcu_role_on(net_name: str) -> tuple[str, str] | None:
            for p in ops.members(net_name):
                ref, _, num = p.partition(":")
                it = by_ref.get(ref)
                if not it or not _is_mcu_item(it):
                    continue
                pname = ""
                try:
                    pname = str(lib.pin_names(it["symbol"]).get(int(num), ""))
                except (ValueError, TypeError):
                    pass
                role = _mcu_uart_role(pname, net_name)
                if role:
                    return role, p
            return None

        a, a_pin = mcu_role_on(tx_net) or ("", "")
        b, b_pin = mcu_role_on(rx_net) or ("", "")
        if a == "TX" and b == "RX" and a_pin and b_pin:
            ops.attach(tx_pin, rx_net)
            ops.attach(rx_pin, tx_net)
            fixes.append(
                f"串口交叉：{uart['ref']} 的 TXD 原接 MCU 的 TX({a_pin})、"
                f"RXD 原接 RX({b_pin})（收发直连不通）；已对调为 "
                f"TXD→{b_pin}、RXD→{a_pin}")


def _fix_uart_symbol(items, ops, lib, fixes):
    """USB-UART 型号配错符号时，换成真实 UART 符号并按网络语义重映射引脚。

    典型：value=CP2102、symbol=Regulator_Linear:ADP3336ARMZ（稳压器）。
    ADP3336 的 1/2/3 脚都是 OUT（电源输出），LLM 按 CP2102 引脚把 RXD0/TXD0
    接到这些脚上，ERC 报「双向 ↔ 电源输出」pin_to_pin 违规。

    这里不信任 LLM 写的引脚号，而是按每个引脚所在**网络名的语义**重新定位到
    CH340G 的正确脚（RXD 网↔TXD 交叉、D+/D−、VCC、GND、RESET→RTS），symbol
    也换成真实 UART 符号，供后续 _fix_usb 补晶振/对调 D+/D−。
    """
    if lib is None:
        return
    for it in items:
        # 型号关键词可能出现在描述性名称里（如 _fix_usb 新增的
        # "晶振 12MHz（CH340G）"），无源件绝不能被当成 UART 换符号。
        sym_l = it.get("symbol", "").lower()
        if ("crystal" in sym_l or sym_l.endswith(":c")
                or sym_l.endswith(":r") or "led" in sym_l):
            continue
        clue = f"{it.get('value', '')} {it.get('name', '')}".lower()
        if not any(k in clue for k in _UART_MODEL_KW):
            continue
        if _looks_uart_symbol(it, lib):
            continue  # 已是 UART 符号，不动
        ref = it["ref"]
        old_sym = it.get("symbol", "")
        # 收集该器件每个旧引脚所在的网络名
        pin_nets: dict[str, str] = {}
        for net in ops.nets:
            for p in net["pins"]:
                if p.startswith(ref + ":"):
                    pin_nets[p.split(":", 1)[1]] = net["name"]
        if not pin_nets:
            continue

        # 换成 CH340G（_fix_usb 已支持它补晶振/对调 D+/D−）
        # BOM 的 name/value/footprint/desc 必须跟着符号一起换：LLM 常把
        # 「4 针串口模块接口」写成 CH340G，若只换符号不换封装，BOM 仍写
        # PinHeader_1x04，与 16 脚芯片自相矛盾，采购/打板会拿错件。
        new_sym = "Interface_USB:CH340G"
        it["symbol"] = new_sym
        it["name"] = "USB转串口芯片（CH340G）"
        it["value"] = "CH340G"
        it["footprint"] = "Package_SO:SOIC-16_3.9x9.9mm_P1.27mm"
        it["desc"] = "USB 转串口芯片（CH340G），需配 12MHz 晶振与负载电容"

        # 按网络名语义决定该网络应接到新符号的哪个引脚名
        def role_of(net_name: str):
            n = net_name.lower()
            if "rx" in n and "tx" not in n:
                return "TXD"          # MCU 的 RXD 网 ← UART 的 TXD（交叉）
            if "tx" in n and "rx" not in n:
                return "RXD"          # MCU 的 TXD 网 → UART 的 RXD（交叉）
            if "d+" in n or "ud+" in n:
                return "UD+"
            if "d-" in n or "ud-" in n:
                return "UD-"
            if _GND_RE.search(n):
                return "GND"
            if "reset" in n or "rst" in n:
                return "~{RTS}"       # 自动复位/下载用 RTS
            if _SUPPLY_RE.match(n) or "vbus" in n or "vcc" in n:
                return "VCC"
            return None

        # 旧引脚全部摘除，再按语义接到新脚（同 role 只接一次，避免两脚并一网）
        for pin in list(pin_nets):
            ops.clear_pin(f"{ref}:{pin}")
        used_roles = set()
        remapped = []
        for old_pin, net_name in pin_nets.items():
            role = role_of(net_name)
            if not role or role in used_roles:
                continue
            pins = _pins(it, lib, (role,)).get(role, [])
            if not pins:
                continue
            used_roles.add(role)
            ops.attach(f"{ref}:{pins[0]}", net_name)
            remapped.append(f"{net_name}→{role}({pins[0]})")
        if remapped:
            fixes.append(
                f"{ref} 型号与符号不符（{old_sym}），已改用 CH340G、同步 BOM 封装为 SOIC-16 "
                f"并按网络语义重映射引脚：" + "，".join(remapped))


def _drop_stale_migration_fixes(ops, fixes: list[str]) -> None:
    """删掉与最终网表不符的「某脚从 X 迁到 Y」提示。

    后续规则可能把同一引脚再挪走：典型是 CH340G 的 pin4 先被 `_fix_power`
    当作 5V 逻辑电源迁到 3.3V，`_fix_usb` 又把它独立成 V3 退耦网。早先那条
    「迁到 3.3V」若留在提示里，就会和用户看到的最终网表矛盾。
    """
    pattern = re.compile(r"^([A-Za-z]+\d+:\d+)（[^）]*）从 .* 迁到 (.+)$")
    keep = []
    for f in fixes:
        m = pattern.match(f)
        if m and ops.name(m.group(1)) != m.group(2):
            continue
        keep.append(f)
    fixes[:] = keep


def _fix_isolated_switch_pins(items, ops, fixes: list[str]) -> None:
    """补全「只有一侧接进电路」的轻触按键：另一侧归位 GND。

    LLM 有时只把复位键一侧接到 NRST，另一侧留在 `SW1:2` 这种单脚网；
    ERC 不报（引脚"已连接"），视觉看着也有线，但按键按下不会拉低 NRST，
    复位电路实际失效。判据严格：
      - 轻触按键（Switch:SW_Push 系列）恰好只有一侧出现在网表；
      - 已接的那一侧含真实活跃芯片脚（MCU/接口/连接器），不是空网；
      - 另一侧悬空或落在只有自己的孤立网，且不是电源/GND。
    修法：把另一侧接到现有 GND，形成「信号→按键→GND」。
    """
    gnd = _gnd_net(ops)
    for it in items:
        sym = str(it.get("symbol", "")).lower()
        if not (sym.startswith("switch:") and "push" in sym):
            continue
        ref = it["ref"]
        attached, isolated = [], []
        for num in ("1", "2"):
            name = ops.name(f"{ref}:{num}")
            # 「只有自己一脚」的网名等同悬空：按键这一侧并没有接到任何器件
            (isolated if not name or len(ops.members(name)) <= 1
             else attached).append((num, name))
        if len(attached) != 1 or len(isolated) != 1:
            continue
        num, net = attached[0]
        other, other_net = isolated[0]
        if _GND_RE.search(net) or _SUPPLY_RE.match(net):
            continue
        if not _has_active_chip(ops, net, items):
            continue
        if other_net and (_GND_RE.search(other_net) or _SUPPLY_RE.match(other_net)):
            continue
        ops.attach(f"{ref}:{other}", gnd)
        fixes.append(
            f"{ref} 的 {other} 脚原"
            f"{'悬空' if not other_net else '在孤立网 ' + other_net}，"
            f"已接到 GND，形成 {net}→{ref}→GND（按键按下才真正拉低信号）")


def _fix_reset_pullup(items, ops, lib, fixes: list[str]) -> None:
    """把 NRST 网上的复位电阻从 GND 改到 3.3V，复位按键保持接 GND。"""
    for it in items:
        clue = f"{it.get('value', '')} {it.get('symbol', '')}".lower()
        if "stm32" not in clue:
            continue
        pmap = lib.pin_names(it["symbol"]) if lib else {}
        nrst = [num for num, name in pmap.items() if name == "NRST"]
        if not nrst:
            continue
        pin = f"{it['ref']}:{nrst[0]}"
        net = ops.name(pin)
        if not net:
            continue
        vdd = _v33_net(ops, items, lib)
        if not vdd:
            for num, name in pmap.items():
                if name in ("VDD", "VDDA", "VBAT"):
                    cand = ops.name(f"{it['ref']}:{num}")
                    if cand and not _GND_RE.search(cand):
                        vdd = cand
                        break
        if not vdd or vdd == net:
            continue
        for p in list(ops.members(net)):
            ref, num = p.split(":", 1)
            rit = next((x for x in items if x["ref"] == ref), None)
            if not rit or not rit.get("symbol", "").lower().startswith("device:r"):
                continue
            other = f"{ref}:{'2' if num == '1' else '1'}"
            on = ops.name(other)
            isolated_pu = (
                bool(on)
                and len(ops.members(on)) == 1
                and (re.search(r"(?:^|_)(?:pu|pullup|pull_up)(?:$|_)", on, re.I)
                     or ref.lower() in on.lower()
                     or "nrst" in on.lower())
            )
            if on and not (_GND_RE.search(on) or isolated_pu):
                continue
            prev = "悬空" if not on else ("GND" if _GND_RE.search(on) else f"孤立网 {on}")
            ops.attach(other, vdd)
            rit["name"] = "电阻（NRST 上拉）"
            rit["desc"] = "NRST 上拉电阻，复位按键按下时接 GND"
            fixes.append(
                f"{ref} 从 {prev} 改接 {vdd}，作为 {it['ref']} 的 NRST 上拉"
                f"（复位按键仍接 GND）")


def _drop_duplicate_ghost_items(items, ops, fixes: list[str]) -> None:
    """删除 BOM 中「网表完全未引用、且与某个已接入元件型号相同」的重复幽灵元件。

    典型：早期 _fix_usb 给 UART 补了 Y2 晶振，但 LLM 原本给的 Y1 仍留在 BOM
    里却不在网表——原理图不会画它，BOM 却让用户多买一颗。判据严格：
      - 该元件 ref 在网表所有引脚中都找不到；
      - 存在另一个已接入元件，symbol 与 value 均相同（真正的重复件）。
    不满足则不删，避免误伤故意预留的元件。
    """
    used = {p.partition(":")[0] for net in ops.nets for p in net["pins"]}
    ghost = [it for it in items if it.get("ref") not in used]
    if not ghost:
        return
    for g in ghost:
        dup = next((it for it in items
                    if it.get("ref") in used
                    and it.get("symbol") == g.get("symbol")
                    and it.get("value") == g.get("value")), None)
        if dup:
            items[:] = [x for x in items if x is not g]
            fixes.append(
                f"删除 BOM 中重复的幽灵元件 {g['ref']}（{g.get('value','')}，"
                f"未接任何网络且网表中已有相同的 {dup['ref']}）")


def _drop_fully_isolated_passives(items, ops, fixes: list[str]) -> None:
    """删除两端都落在单脚孤立网的 R/C/L：这种件不在任何回路里。

    LLM 偶尔会把一颗电容的两脚各放进一个「只有自己」的网（C5_1/C5_2），
    ERC/视觉都不报，功能自检会报两脚悬空。它既不是负载电容也不是退耦，
    直接删除并清理 BOM，避免用户多买料。
    """
    for it in list(items):
        sym = it.get("symbol", "").lower()
        if not (sym.startswith("device:c") or sym.startswith("device:r")
                or sym.startswith("device:l")):
            continue
        ref = it["ref"]
        isolated = []
        for num in ("1", "2"):
            pin = f"{ref}:{num}"
            name = ops.name(pin)
            if not name or len(ops.members(name)) == 1:
                isolated.append((pin, name))
        if len(isolated) != 2:
            continue
        for pin, _name in isolated:
            ops.clear_pin(pin)
        items.remove(it)
        fixes.append(f"删除完全悬空的 {ref}（{it.get('value', '')}）："
                     f"两脚各占一个单脚网，不构成任何回路")


def _drop_floating_passives(items, ops, fixes: list[str]) -> None:
    """删除「一端接 GND、另一端完全悬空」的残留无源件（C/R/L）。

    LLM 常重复生成负载电容：UART 晶振的 XI/XO 已各有一颗正确接地的负载电容，
    却还留着一颗标成「晶振负载」的电容，其一端接 GND、另一端不在任何网络。
    这颗件不起任何作用，ERC/视觉都不报，BOM 却让用户多买料、原理图带线头。
    判据严格（避免误删设计上故意单端的件）：
      - 仅 Device:C/R/L，且恰好一端接 GND、另一端完全不接网；
      - 网络中存在同 symbol + value 的元件两端都接了网（真正多余的副本）。
    """
    used = {p: n["name"] for n in ops.nets for p in n["pins"]}
    used_refs = {p.partition(":")[0] for p in used}
    for it in list(items):
        sym = it.get("symbol", "").lower()
        if not (sym.startswith("device:c") or sym.startswith("device:r")
                or sym.startswith("device:l")):
            continue
        ref = it["ref"]
        p1, p2 = f"{ref}:1", f"{ref}:2"
        n1, n2 = used.get(p1), used.get(p2)
        # 恰好一端接 GND、另一端完全悬空
        if bool(n1) == bool(n2):
            continue
        attached = p1 if n1 else p2
        net_name = n1 or n2
        if not net_name or not _GND_RE.search(net_name):
            continue
        # 必须存在同型号、两端都接网的副本（说明这颗是多余残留）
        twin = next((o for o in items
                     if o is not it
                     and o.get("symbol") == it.get("symbol")
                     and o.get("value") == it.get("value")
                     and all(f"{o['ref']}:{k}" in used for k in ("1", "2"))), None)
        if not twin:
            continue
        ops.remove_component(ref, [p1, p2])
        items[:] = [x for x in items if x is not it]
        fixes.append(
            f"删除残留无源件 {ref}（{it.get('value','')}，仅一端接 {net_name}、"
            f"另一端悬空；同型号的 {twin['ref']} 已正确接入）")


def _reattach_orphan_ground_pins(items, ops, fixes: list[str], lib=None) -> None:
    """修复「无源件的地脚接在只有自己的孤立 GND 网」——如晶振负载电容
    C1:2 落在 C1_GND、C2:2 落在 C2_GND，实际并未接到真正的 GND 网。

    LLM 常把本该接地的脚写成带 GND 字样的独立网名，造成网表里出现
    ``C1_GND = ['C1:2']`` 这种单成员网。ERC 不报（脚确实"已连接"），
    视觉也看不出（有网名有连线），但功能上负载电容没接地、振荡回路不成立，
    只有功能自检的孤立网检查能发现。

    判据（严格，避免误伤）：
      - 单成员网，且网名含 GND/VSS/地 语义；
      - 唯一成员是 Device:C/R/L 等无源件的引脚；
      - 网表里另有一个"真 GND"网：成员≥2 且含非本件引脚；
      - 该引脚在符号里确实是地脚（若符号有 GND 引脚名），或网名与位号
        对应（C1_GND↔C1）——避免把误命名的信号网当地。
    修法：把该脚改接到真 GND 网，孤立网随之清空。
    """
    gnd_nets = [n for n in ops.nets
                if len(n["pins"]) >= 2 and _GND_RE.search(n["name"])]
    if not gnd_nets:
        return
    # 选成员最多的 GND 网作为"真地"
    real = max(gnd_nets, key=lambda n: len(n["pins"]))

    for net in list(ops.nets):
        pins = list(net["pins"])
        if len(pins) != 1 or net is real or not _GND_RE.search(net["name"]):
            continue
        pin = pins[0]
        ref, _, num = pin.partition(":")
        item = next((it for it in items if it["ref"] == ref), None)
        if not item:
            continue
        # 不限定器件类型：LED 阴极、二极管、任意芯片的地脚都可能被 LLM 写成
        # "<位号>_GND" 孤立网（如 LED_GND=[LED1:1]）。原来只认 C/R/L 无源件，
        # LED 这类被漏掉，功能自检报「1 脚接在孤立网、未连任何器件」。
        # 安全由后面两条严格判据保证：网名含 GND 语义 + 网名与本件位号对应，
        # 因此不会把信号网或别人的地误判进来。
        # 网名须与本件对应：完整位号（C1_GND↔C1）或去数字前缀（LED_GND↔LED1）。
        # 同时已要求网名含 GND/VSS 语义，故 LED_CTRL 这类信号网不会被误判。
        name_l = net["name"].lower().replace("-", "_")
        ref_l = ref.lower().replace("-", "_")
        prefix = re.match(r"[a-z]+", ref_l)
        prefix = prefix.group(0) if prefix else ref_l
        if not (ref_l in name_l or (len(prefix) >= 2 and prefix in name_l)):
            continue
        # 该脚在符号里必须确实是地/阴极脚（若符号有 GND/K/Cathode 引脚名）；
        # 查不到引脚名时退回位号对应判据（网名含本件位号已足够严格）。
        try:
            pname = str(lib.pin_names(item.get("symbol", "")).get(int(num), "")).lower() \
                if lib is not None else ""
        except Exception:
            pname = ""
        # Device:C/R/L 的引脚名常写成 "~"（非极性无源件），不是"非地脚"；
        # 网名已同时满足 GND 语义和位号对应，足够安全。
        passive = item.get("symbol", "").lower().startswith(("device:c", "device:r", "device:l"))
        if (pname and not any(k in pname for k in ("gnd", "vss", "k", "cath", "ground"))
                and not (passive and pname in ("~", ""))):
            continue
        real["pins"].append(pin)
        net["pins"].remove(pin)
        fixes.append(
            f"{ref} 的地脚从孤立网 {net['name']} 归位到 {real['name']}")


def _fix_crystal_load_caps(items, ops, lib, fixes: list[str]) -> None:
    """给缺少 pF 负载电容的晶振网补电容，优先复用「断头」pF 电容。

    LLM 常生成 22pF 电容却只把一端接 GND、另一端留在孤立网（如 C5_1），
    随后 _drop_dead_end_passives 把它当断头件删掉，主晶振因此没有负载电容、
    MCU 无法起振。此时晶振两脚都在网上，ERC/视觉/safety/function 全绿。
    按结构识别：晶振脚所在网若无 pF 电容到 GND，先找一脚接 GND、另一脚在
    孤立网的 pF 电容改接过去；没有空闲电容才新增。幂等。
    """
    gnd = _gnd_net(ops)
    if not gnd:
        return
    crystals = [it for it in items if "crystal" in it.get("symbol", "").lower()]
    if not crystals:
        return

    # 可复用的断头 pF 电容：一脚在 GND、另一脚在「只有它自己」的孤立网
    spare: list[tuple[dict, str, str]] = []
    for it in items:
        sym = it.get("symbol", "").lower()
        if not (sym.endswith(":c") or "电容" in str(it.get("name", ""))):
            continue
        if "pf" not in str(it.get("value", "")).lower():
            continue
        p1, p2 = f"{it['ref']}:1", f"{it['ref']}:2"
        n1, n2 = ops.name(p1), ops.name(p2)
        if not n1 or not n2:
            continue
        for fixed, gpin, lone, lpin in ((n1, p1, n2, p2), (n2, p2, n1, p1)):
            if _GND_RE.search(fixed) and len(ops.members(lone)) == 1:
                spare.append((it, lpin, lone))
                break

    # 情形一：两颗负载电容的另一端聚在一个「无 GND/电源/芯片成员的浮空网」
    #（如 OSC_CAP=[C5:2, C6:2]）——把该网并入 GND，比新增电容更干净。
    cap_nets = set()
    for it in items:
        if not (it.get("symbol", "").lower().endswith(":c")
                and "pf" in str(it.get("value", "")).lower()):
            continue
        for p in ("1", "2"):
            pn = f"{it['ref']}:{p}"
            n = ops.name(pn)
            if n and n != gnd:
                cap_nets.add(n)
    for n in list(cap_nets):
        if _GND_RE.search(n) or _SUPPLY_RE.match(n):
            continue
        members = ops.members(n)
        if len(members) < 2:
            continue
        refs = {p.partition(":")[0] for p in members}
        its = [next((x for x in items if x["ref"] == r), None) for r in refs]
        if not all(it and it.get("symbol", "").lower().endswith(":c")
                   and "pf" in str(it.get("value", "")).lower() for it in its):
            continue
        # 全是可疑浮空 pF 电容的另一脚 → 整网并入 GND
        for pin in list(members):
            ops.attach(pin, gnd)
        fixes.append(f"晶振负载电容的公共端 {n}（仅 {len(members)} 个电容脚、无地/电源）"
                     f"实际浮空，已并入 {gnd}（否则振荡回路不成立）")

    used = 0
    for xt in crystals:
        for p in ("1", "2"):
            pin = f"{xt['ref']}:{p}"
            net = ops.name(pin)
            if not net or _has_load_cap(ops, items, net, gnd):
                continue
            if used < len(spare):
                it, lpin, lone = spare[used]
                used += 1
                ops.attach(lpin, net)
                fixes.append(
                    f"复用断头电容 {it['ref']}（{it.get('value','')}）从孤立网 {lone} "
                    f"改接 {net}，作为 {xt['ref']} 的负载电容（否则晶振不起振）")
            else:
                refs = [int(m.group(1)) for i in items
                        if (m := re.fullmatch(r"C(\d+)", i["ref"]))]
                cref = f"C{max(refs, default=0) + 1}"
                items.append({"ref": cref, "name": "晶振负载电容", "value": "22pF",
                              "qty": 1, "symbol": "Device:C", "footprint": ""})
                ops.attach(f"{cref}:1", net)
                ops.attach(f"{cref}:2", gnd)
                fixes.append(f"新增 {cref}（22pF）作为 {xt['ref']} 的负载电容 {net}↔GND")


def _drop_dead_end_passives(items, ops, fixes: list[str]) -> None:
    """删除「一端接电源/信号网、另一端完全悬空」的断头无源件（R/C/L）。

    LLM 常生成只接了一端的限流电阻/滤波电容（如 R2 只接 VCC、另一端悬空），
    或修复流程复用/新增了新电阻后，旧的断头电阻残留在网表里。这类件不构成
    任何回路、永远不起作用，ERC/视觉都不报，功能自检会报悬空，BOM 还让用户
    多买料。判据严格（避免误删设计上故意单端的件）：
      - 仅 Device:C/R/L，且恰好一端接网、另一端完全不接网；
      - 悬空端不属于任何网（不是「接了个只有自己的网」这种 Tuning 场景）；
      - 元件未被其它修正规则在本轮复用（复用后两端都有网，自然不满足）。
    """
    used = {p: n["name"] for n in ops.nets for p in n["pins"]}
    members = {n["name"]: set(n["pins"]) for n in ops.nets}
    for it in list(items):
        sym = it.get("symbol", "").lower()
        if not (sym.startswith("device:c") or sym.startswith("device:r")
                or sym.startswith("device:l")):
            continue
        ref = it["ref"]
        p1, p2 = f"{ref}:1", f"{ref}:2"
        n1, n2 = used.get(p1), used.get(p2)
        dead_pin = None
        reason = None
        if bool(n1) != bool(n2):
            # 情形一：一端接网、另一端完全不在网表（经典断头）
            dead_pin = (p1, p2)[not bool(n1)]
            name = n1 or n2
            if _GND_RE.search(name or ""):     # 交给 _drop_floating_passives
                continue
            reason = f"另一脚悬空"
        elif n1 and n2 and n1 != n2:
            # 情形二：一端接 GND/电源/GPIO、另一端接在「只有它自己」的孤立网
            # （LLM 把限流电阻接成 "GPIO→R→孤立LED网" 的死支路，但 BOM 里
            #  并没有第二颗 LED；ERC/视觉都看不出，只有功能自检报孤立脚。
            #  所有修复 LED 的规则都已在前面试过，这里剩下的就是无用件。）
            for fixed_pin, fixed_net, lone_pin, lone_net in (
                    (p1, n1, p2, n2), (p2, n2, p1, n1)):
                driven = (_GND_RE.search(fixed_net)
                          or _SUPPLY_RE.match(fixed_net)
                          or _has_active_chip(ops, fixed_net, items))
                if not driven:
                    continue
                if len(members.get(lone_net, ())) == 1 and lone_pin in members[lone_net]:
                    dead_pin = lone_pin
                    reason = f"{lone_net} 网仅有此一脚（孤立线头）"
                    break
        if not dead_pin:
            continue
        ops.remove_component(ref, [p1, p2])
        items[:] = [x for x in items if x is not it]
        fixes.append(
            f"删除断头无源件 {ref}（{it.get('value','')}，{reason}，不构成回路）")


def _drop_half_wired_leds(items, ops, fixes: list[str]) -> None:
    """删除「阴极接 GND、阳极完全悬空」的残缺 LED。

    LLM 偶尔生成只接了一根线的 LED（如只有 K 脚接 GND，A 脚不在任何网络）——
    这颗灯永远不会亮，ERC 不报（悬空输入对 ERC 无害），BOM 却让用户多买料。
    判据严格：LED 有引脚出现在网表中，但只出现阴极一侧、阳极一侧完全缺席；
    且已存在至少一颗接线完整的 LED（避免把唯一一颗灯删掉）。
    """
    used = {p.partition(":")[0] for net in ops.nets for p in net["pins"]}
    leds = [it for it in items
            if it.get("symbol", "").lower().endswith(":led")
            or "led" in it.get("symbol", "").lower()]
    complete = 0
    for led in leds:
        pins = [p.partition(":")[2] for net in ops.nets for p in net["pins"]
                if p.startswith(led["ref"] + ":")]
        if len(set(pins)) >= 2:
            complete += 1
    if complete < 1:
        return
    for led in leds:
        ref = led["ref"]
        pins = {p.partition(":")[2] for net in ops.nets for p in net["pins"]
                if p.startswith(ref + ":")}
        if not pins or len(pins) >= 2:
            continue
        # 只有一侧引脚接网；且这一侧是阴极(GND) → 残缺 LED，无驱动能力
        only = next(iter(pins))
        a_pins = set(_p(led, None, "A")) or {"2"}
        if only in a_pins:
            continue
        net_name = next((n["name"] for n in ops.nets
                         if f"{ref}:{only}" in n["pins"]), "")
        if not _GND_RE.search(net_name):
            continue
        ops.remove_component(ref, [f"{ref}:{only}"])
        items[:] = [x for x in items if x.get("ref") != ref]
        fixes.append(
            f"删除残缺 LED {ref}（仅阴极接 {net_name}、阳极完全悬空，永远不亮）")


def _fix_swd_clock(items, ops, lib, fixes: list[str]) -> None:
    """SWD 调试口的 SWCLK 悬空、而连接器有冗余 GND 脚时，把冗余地脚改接 SWCLK。

    LLM 常把 4 针 SWD 接口画成 SWDIO/NRST/GND/GND——SWCLK 只挂在 MCU 的
    PA14 上、没引到连接器，调试器无法提供时钟，SWD 下载/调试完全不可用。
    ERC 不报（悬空输入合法）、视觉不报（线连着）、安全不报，属三检盲区。
    判据严格：SWDIO 已接到某 4 针连接器的脚；SWCLK 网只有 MCU 一个成员；
    该连接器存在两个及以上 GND 脚（多余的地可让给时钟）。
    """
    gnd = _gnd_net(ops)
    # SWDIO 所在连接器
    swdio = next((n for n in ops.nets
                  if re.search(r"swdio", str(n.get("name") or ""), re.I)), None)
    if not swdio:
        return
    conn_ref = None
    for p in swdio["pins"]:
        ref = p.partition(":")[0]
        it = next((x for x in items if x.get("ref") == ref), None)
        if it and "conn_01x04" in it.get("symbol", "").lower():
            conn_ref = ref
            break
    if not conn_ref:
        return
    # SWCLK 只有 MCU 单成员（未引出）
    swclk = next((n for n in ops.nets
                  if re.search(r"swclk|swdclk", str(n.get("name") or ""), re.I)), None)
    if not swclk or len(swclk["pins"]) != 1:
        return
    mcu_ref = swclk["pins"][0].partition(":")[0]
    if not _is_mcu_item(next((x for x in items if x.get("ref") == mcu_ref), {})):
        return
    # 该连接器的冗余 GND 脚（>=2）
    gnd_pins = [p for p in ops.members(gnd) if p.startswith(conn_ref + ":")]
    if len(gnd_pins) < 2:
        return
    target = sorted(gnd_pins)[-1]
    ops.attach(target, swclk["name"])
    fixes.append(
        f"{conn_ref} 的 SWD 口 SWCLK 未引出，将冗余地脚 {target} 改接 {swclk['name']}"
        f"（调试器可提供时钟）")


def _unique_net_name(ops: _Nets, base: str) -> str:
    """给新引出的 IO 取一个不与现有网络冲突的名字。"""
    name = base
    i = 2
    while any(n.get("name") == name for n in ops.nets):
        name = f"{base}_{i}"
        i += 1
    return name


def _break_out_missing_io_ports(items, ops, lib, fixes: list[str]) -> None:
    """整组 IO 一个都没接、而同芯片别的端口已引出时，自动加排针逐脚引出。

    这是「功能自检报整组 IO 漏接」对应的修复动作。规则不绑定具体芯片或
    端口字母：按符号引脚名里的 PA0/PB3/PC14 这类端口组识别，只要某组
    完全没出现在网表里，就新增一个与组内 IO 数量相同的排针并逐脚连接。
    单个未用 GPIO 不在此列——只处理「整组全丢」这种明显漏接。
    """
    if not lib:
        return
    connected = {p for n in ops.nets for p in n.get("pins", [])}
    for it in items:
        sym = str(it.get("symbol", ""))
        sl = sym.lower()
        if not ("mcu_" in sl or "rf_module" in sl or "soc_" in sl):
            continue
        ref = it.get("ref", "")
        try:
            names = lib.pin_names(sym)
        except Exception:
            continue
        groups: dict[str, list[tuple[int, int]]] = {}
        for pin, nm in names.items():
            m = _PORT_GROUP_RE.match(str(nm).strip())
            if m:
                groups.setdefault(m.group(1).upper(), []).append((int(m.group(2)), int(pin)))
        if len(groups) < 2:
            continue
        wired_pins = {
            int(p.partition(":")[2]) for p in connected
            if p.partition(":")[0] == ref and p.partition(":")[2].isdigit()
        }
        wired_groups = {
            g for g, entries in groups.items()
            if any(pin in wired_pins for _logical, pin in entries)
        }
        if not wired_groups:
            continue
        for prefix, entries in sorted(groups.items()):
            if prefix in wired_groups:
                continue
            entries.sort()
            count = len(entries)
            sym_id = f"Connector:Conn_01x{count:02d}_Pin"
            try:
                lib.pin_numbers(sym_id)
            except Exception:
                continue                      # 符号库没有对应排针，跳过而不猜引脚号
            jref = _add_ref(items, "J")
            items.append({
                "ref": jref,
                "name": f"{prefix} IO 扩展排针",
                "value": f"{prefix}0-{prefix}{entries[-1][0]}",
                "qty": 1,
                "symbol": sym_id,
                "footprint": "",
                "desc": f"引出 {it.get('value', '')} 的 {prefix} 端口 IO",
            })
            for idx, (logical, pin) in enumerate(entries, start=1):
                netname = _unique_net_name(ops, f"{prefix}{logical}")
                ops.attach(f"{ref}:{pin}", netname)
                ops.attach(f"{jref}:{idx}", netname)
            fixes.append(
                f"{ref}（{it.get('value', '')}）整组 {prefix} IO 未引出，"
                f"新增 {jref}（{count}Pin）并逐脚连接到 {prefix}0…{prefix}{entries[-1][0]}")


def _mcu_supply_net(ops, items, lib) -> str | None:
    """主控实际所在的供电网名（不看网名先不看芯片脚，避免 VCC_IO 这类假电源）。"""
    for it in items:
        if not _is_mcu_item(it):
            continue
        for wanted in ("VCC", "VDD"):
            for p in _p(it, lib, wanted):
                nn = ops.name(f"{it['ref']}:{p}")
                if nn and len(ops.members(nn)) > 1:
                    return nn
    v33 = _v33_net(ops, items, lib)
    if v33:
        return v33
    for net in ops.nets:
        if _SUPPLY_RE.search(str(net.get("name") or "")) and len(net["pins"]) > 1:
            return net["name"]
    return None


def _fix_icsp_power_pins(items, ops, lib, fixes: list[str]) -> None:
    """2×3 ICSP 编程口只接出信号脚、VCC/GND 脚悬空时补接电源与地。

    LLM 常把 ICSP 的 MISO/MOSI/SCK/RESET 四根信号写完，却把 2 脚(VCC)、
    6 脚(GND)整个漏掉：网表里根本没有这两个引脚，writer 按规范打
    no_connect（×）。ERC 0、视觉 0、功能自检 0（连接器针脚允许悬空），
    但编程器插上后没有供电和回流地，烧录完全不可用。

    判据不绑定位号：6 针连接器 + 至少两脚网名符合标准 2×3 ICSP 引脚映射
    （1=MISO、3=SCK、4=MOSI、5=RESET）或标注含 ICSP/编程。引脚号优先取
    符号里的 VCC/GND 引脚名，取不到才用 ICSP 标准排列。只在引脚完全悬空
    （或只挂一个同义单脚网）时补，已接到其它网的不动，保证幂等。
    """
    sig_re = re.compile(r"miso|mosi|(^|_)sck|reset|nrst|复位", re.I)
    std = {"1": r"miso", "3": r"(^|_)sck", "4": r"mosi", "5": r"reset|nrst"}
    for it in items:
        sym = str(it.get("symbol", "")).lower()
        if not ("conn_01x06" in sym or "conn_02x03" in sym):
            continue
        ref = it["ref"]
        ident = f"{it.get('name','')} {it.get('desc','')} {it.get('value','')}"
        attached: dict[str, str] = {}
        for net in ops.nets:
            for p in net["pins"]:
                if p.startswith(ref + ":"):
                    attached[p.partition(":")[2]] = str(net.get("name") or "")
        sig = {pn: nn for pn, nn in attached.items() if sig_re.search(nn)}
        matched = sum(1 for pn, pat in std.items()
                      if pn in sig and re.search(pat, sig[pn], re.I))
        if matched < 2 and not re.search(r"icsp|isp|编程|program", ident, re.I):
            continue
        vcc_pin = gnd_pin = None
        if lib:
            try:
                for num, nm in lib.pin_names(it["symbol"]).items():
                    if re.search(r"vcc|vdd|\+?5v", str(nm), re.I):
                        vcc_pin = str(num)
                    elif _GND_RE.search(str(nm)):
                        gnd_pin = str(num)
            except Exception:
                pass
        vcc_pin = vcc_pin or "2"           # 标准 2×3 ICSP：2=VCC，6=GND
        gnd_pin = gnd_pin or "6"
        vcc = _mcu_supply_net(ops, items, lib)
        gnd = _gnd_net(ops)
        for pin, target, label, sem in (
            (vcc_pin, vcc, "VCC", _SUPPLY_RE),
            (gnd_pin, gnd, "GND", _GND_RE),
        ):
            if not target:
                continue
            full = f"{ref}:{pin}"
            cur = ops.name(full)
            if cur == target:
                continue
            # 只补完全悬空、或只挂在同义单脚网上的脚； intentional 连接不动
            if cur and not (len(ops.members(cur)) == 1 and sem.search(cur)):
                continue
            ops.attach(full, target)
            fixes.append(f"ICSP 编程排针 {ref} 的 {pin} 脚({label})原悬空，"
                         f"已接 {target}（否则编程器无供电/回流地，无法烧录）")


def _fix_usb_connector_ground(items, ops, lib, fixes: list[str]) -> None:
    """接了 USB D± 的连接器，GND 脚必须真的接到 GND 网。

    LLM 常把 Micro-B 画成 VBUS/D−/D+ 三脚，5 脚(GND)留空；ERC 不报
    （连接器针脚悬空合法）、视觉不报，但 USB 没有回流地，转串口完全不可用。
    仅在 GND 脚**悬空**时补接；已接到别的网则不动，避免破坏有意设计。
    """
    gnd = _gnd_net(ops)
    for it in items:
        sym = str(it.get("symbol", "")).lower()
        if not ("conn_01x05" in sym or "usb_micro" in sym or "usb_type" in sym):
            continue
        ref = it["ref"]
        netnames = [str(n.get("name") or "") for n in ops.nets
                    if any(p.startswith(ref + ":") for p in n["pins"])]
        if not any(re.search(r"usb_d[+-]|(^|_)d[+-]$", n, re.I) for n in netnames):
            continue
        d_p, d_m = _usb_data_pins(it, lib)
        gnd_pin = None
        if lib:
            try:
                for num, nm in lib.pin_names(it["symbol"]).items():
                    if re.search(r"gnd|ground|vss", str(nm), re.I):
                        gnd_pin = str(num)
                        break
            except Exception:
                pass
        if gnd_pin is None and d_p == "3" and d_m == "2":
            gnd_pin = "5"          # Micro-B / Type-A：1=VBUS 2=D− 3=D+ 4=ID 5=GND
        if gnd_pin is None:
            continue
        pin = f"{ref}:{gnd_pin}"
        current = ops.name(pin)
        if current and _GND_RE.search(current):
            continue                   # 已正确接地，幂等
        if current and not (re.search(r"nrst|reset|复位", current, re.I)
                            or (d_p == "3" and d_m == "2")):
            continue                   # 已接到别的合理网就不动
        if current:
            ops.attach(pin, gnd)
            fixes.append(f"USB 连接器 {ref} 的 GND 脚 {gnd_pin} 原接 {current}，"
                         f"已改接 {gnd}（否则 USB 无回流地，转串口不可用）")
        else:
            ops.attach(pin, gnd)
            fixes.append(f"USB 连接器 {ref} 的 GND 脚 {gnd_pin} 原悬空，已接 {gnd}"
                         f"（否则 USB 无回流地，转串口不可用）")
        # 标注错配：写着「SWD/调试」的口却接成 USB D±，会把 BOM/贴装带偏。
        ident = f"{it.get('name', '')} {it.get('desc', '')}"
        if re.search(r"swd|jtag|调试", ident, re.I):
            it["name"] = "USB 接口（Micro-B）"
            it["value"] = "USB-Micro-B"
            it["desc"] = "USB 5V/D+/D− 输入"
            fixes.append(f"连接器 {ref} 标注为「SWD 调试口」却实际接 USB D±，"
                         f"已更正为 USB 连接器标注")


def _fix_swd_breakout(items, ops, lib, fixes: list[str]) -> None:
    """网名含 SWDIO/SWCLK 却只挂 MCU 单脚时，新增 4 针 SWD 排针逐脚引出。

    LLM 常生成 PA13_SWDIO / PA14_SWCLK 这类网名，却只把它们接在 MCU 一个
    脚上，调试器没有可插的排针，下载/调试完全不可用。ERC 不报（悬空输入
    合法）、视觉不报，属功能盲区。判据只看网名语义 + 单成员，不绑定位号；
    已引出到任意连接器的不再重复添加。
    """
    if not lib or not any(_is_mcu_item(it) for it in items):
        return
    swdio = next((n for n in ops.nets
                  if re.search(r"swdio|swdat|jtms",
                               str(n.get("name") or ""), re.I)
                  and len(n["pins"]) == 1), None)
    swclk = next((n for n in ops.nets
                  if re.search(r"swclk|swck|jtc",
                               str(n.get("name") or ""), re.I)
                  and len(n["pins"]) == 1), None)

    # 更隐蔽的一种：LLM 连网名都没生成，SWD 脚完全不在网表里（被写成
    # no_connect）。此时网名语义规则找不到任何东西，但芯片照样烧不进程序。
    # 按符号真实引脚名（SWDIO/SWDAT/JTMS、SWCLK/SWCK/JTCK）定位 MCU 脚，
    # 若该脚悬空则新建网名，交给下面统一加排针。
    def _mcu_swd_pin(kind: str, require_unconnected: bool = True) -> str | None:
        pat = re.compile(r"swdio|swdat|jtms" if kind == "io" else r"swclk|swck|jtc", re.I)
        for it in items:
            if not _is_mcu_item(it):
                continue
            try:
                names = (lib.pin_names_with_alts(it["symbol"])
                         if hasattr(lib, "pin_names_with_alts")
                         else lib.pin_names(it["symbol"]))
            except Exception:
                continue
            for num, nm in names.items():
                if pat.search(str(nm)):
                    pin = f"{it['ref']}:{num}"
                    if require_unconnected and ops.name(pin):
                        continue               # 完全悬空才处理
                    return pin
        return None

    if not swdio and not swclk:
        # 两路都没有单脚网：只有「引脚完全悬空」才需要新建网名并加排针；
        # 若两路都已接到连接器，则已引出，直接返回，保证幂等。
        io_pin = _mcu_swd_pin("io", require_unconnected=True)
        clk_pin = _mcu_swd_pin("clk", require_unconnected=True)
        if not io_pin and not clk_pin:
            return
    elif not swdio or not swclk:
        # 不对称情形：SWDIO 已引出、SWCLK 只有 MCU 单脚（或反过来）。
        # 不能因为其中一路上已有排针就放弃补另一路，否则调试器仍接不上。
        io_pin = _mcu_swd_pin("io", require_unconnected=False) if not swdio else None
        clk_pin = _mcu_swd_pin("clk", require_unconnected=False) if not swclk else None
    else:
        io_pin = clk_pin = None

    if io_pin:
        nn = ops.name(io_pin) or f"{io_pin.replace(':', '_')}_SWDIO"
        swdio = ops.ensure(nn)
        if io_pin not in swdio["pins"]:
            swdio["pins"].append(io_pin)
    if clk_pin:
        nn = ops.name(clk_pin) or f"{clk_pin.replace(':', '_')}_SWCLK"
        swclk = ops.ensure(nn)
        if clk_pin not in swclk["pins"]:
            swclk["pins"].append(clk_pin)
    if not swdio or not swclk:
        return
    sym_id = "Connector:Conn_01x04_Pin"
    try:
        lib.pin_numbers(sym_id)
    except Exception:
        return
    jref = _add_ref(items, "J")
    items.append({
        "ref": jref, "name": "SWD 调试接口", "value": "4Pin", "qty": 1,
        "symbol": sym_id, "footprint": "",
        "desc": "引出 SWDIO/SWCLK/GND/Vref",
    })
    vref = _v33_net(ops, items, lib) or ""
    ops.attach(f"{jref}:1", swdio["name"])
    ops.attach(f"{jref}:2", swclk["name"])
    ops.attach(f"{jref}:3", _gnd_net(ops))
    if vref:
        ops.attach(f"{jref}:4", vref)
    fixes.append(f"新增 SWD 调试排针 {jref}：SWDIO/SWCLK/GND/Vref 逐脚引出"
                 f"（原 {swdio['name']}/{swclk['name']} 只挂 MCU 单脚，调试器接不上）")


def _ensure_usb_uart_bridge(items, ops, lib, fixes: list[str]) -> None:
    """USB 口 D± 直接落到 MCU 的 TXD/RXD 且板上没有 USB-UART 芯片时补 CH340G。

    ERC/视觉/安全都只看「脚是否接上网」，Micro-B 的 D−/D+ 直接接 UART 是
    单端串口 ↔ 差分 USB 的硬 functional 错误：烧录/通信完全不成立。判据
    必须同时满足：①存在 5 针 USB 连接器；②它的 D± 两条网里各有一个 MCU
    UART 脚；③板上没有 UART 特征符号。修法只做结构性重连：把 D± 改挂
    新建的 USB_D±，插入 CH340G 的 UD±，TXD/RXD 交叉回原 MCU 串口网；
    XI/XO、V3 退耦交给后续 _fix_usb 用同一规则补齐，不在此重复造件。
    """
    if lib is None or any(_looks_uart_symbol(it, lib) for it in items):
        return
    usb = next((it for it in items
                if any(k in str(it.get("symbol", "")).lower()
                       for k in ("conn_01x05", "usb_micro", "usb_type"))), None)
    if not usb:
        return
    d_p, d_m = _usb_data_pins(usb, lib)
    dp_pin, dm_pin = f"{usb['ref']}:{d_p}", f"{usb['ref']}:{d_m}"
    dp_net, dm_net = ops.name(dp_pin), ops.name(dm_pin)
    if not dp_net or not dm_net or dp_net == dm_net:
        return

    tx_nets: set[str] = set()
    rx_nets: set[str] = set()
    for it in items:
        if not _is_mcu_item(it):
            continue
        try:
            pin_names = lib.pin_names(it.get("symbol", ""))
        except Exception:
            continue
        for num, pname in pin_names.items():
            net = ops.name(f"{it['ref']}:{num}")
            if not net:
                continue
            role = _mcu_uart_role(str(pname), net)
            if role == "TX":
                tx_nets.add(net)
            elif role == "RX":
                rx_nets.add(net)
    if not tx_nets or not rx_nets:
        return
    if not ((dm_net in tx_nets and dp_net in rx_nets)
            or (dm_net in rx_nets and dp_net in tx_nets)):
        return
    tx_net = dm_net if dm_net in tx_nets else dp_net
    rx_net = dp_net if dp_net in rx_nets else dm_net

    vbus = ops.name(f"{usb['ref']}:1") or _usb_vbus_net(ops, items, lib)
    gnd = _gnd_net(ops)
    if not vbus or not gnd:
        return
    ur = _add_ref(items, "U")
    uart = {"ref": ur, "name": "USB 转串口", "value": "CH340G", "qty": 1,
            "symbol": "Interface_USB:CH340G", "footprint": "",
            "desc": "USB 转 TTL 串口，负责 ESP32 下载与通信"}
    items.append(uart)

    usb_dp = _unique_net_name(ops, "USB_D+")
    usb_dm = _unique_net_name(ops, "USB_D-")
    ops.attach(dp_pin, usb_dp)
    ops.attach(dm_pin, usb_dm)
    ud_p = next(iter(_p(uart, lib, "UD+") or _p(uart, lib, "D+")), "5")
    ud_m = next(iter(_p(uart, lib, "UD-") or _p(uart, lib, "D-")), "6")
    txd = next(iter(_p(uart, lib, "TXD") or _p(uart, lib, "TX")), "2")
    rxd = next(iter(_p(uart, lib, "RXD") or _p(uart, lib, "RX")), "3")
    vcc = next(iter(_p(uart, lib, "VCC")), "16")
    gndp = next(iter(_p(uart, lib, "GND")), "1")
    v3p = next(iter(_p(uart, lib, "V3")), "4")
    ops.attach(f"{ur}:{ud_p}", usb_dp)
    ops.attach(f"{ur}:{ud_m}", usb_dm)
    # UART 的发送必须落到 MCU 的 RX 网，接收落到 TX 网（交叉），否则收发都不通。
    ops.attach(f"{ur}:{txd}", rx_net)
    ops.attach(f"{ur}:{rxd}", tx_net)
    ops.attach(f"{ur}:{vcc}", vbus)
    ops.attach(f"{ur}:{gndp}", gnd)
    ops.attach(f"{ur}:{v3p}", _unique_net_name(ops, f"{ur}_V3"))
    fixes.append(
        f"USB 口 {usb['ref']} 的 D± 原直连 MCU 串口（{dm_net}/{dp_net}），"
        f"已补 {ur}(CH340G)：D±→UD±，TXD→{rx_net}、RXD→{tx_net}")


def _sync_connector_value_to_symbol(items, lib, fixes: list[str]) -> None:
    """排针 BOM 标称 2xN 但符号实际是 1xN 时，按符号纠正标称。"""
    if lib is None:
        return
    for it in items:
        sym = str(it.get("symbol", ""))
        sm = re.search(r"Conn_01x(\d+)_Pin", sym, re.I)
        if not sm:
            continue
        n = int(sm.group(1))
        value = str(it.get("value", "")).strip()
        vm = re.fullmatch(r"(\d+)\s*[xX]\s*(\d+)", value)
        if not vm:
            continue
        rows, cols = int(vm.group(1)), int(vm.group(2))
        if rows * cols == n:  # 标称脚数与符号一致，不动
            continue
        new = f"1x{n}"
        it["value"] = new
        desc = str(it.get("desc", ""))
        if value and value in desc:
            it["desc"] = desc.replace(value, new)
        fixes.append(f"{it['ref']} 标称 {value} 与符号 {n} 脚不符，已改为 {new}")


def fix_topology(bom: dict, netlist: dict, symbol_lib=None) -> list[str]:
    """就地修正可检测到的拓扑错误，返回修正说明列表。"""
    items = bom.get("items", [])
    ops = _Nets(netlist)
    fixes: list[str] = []
    if not items:
        return fixes
    # LLM 偶尔会生成 name=null / 空串的网络；下游规则普遍假定 name 是字符串，
    # 一旦为 None，re.search / .lower() 会抛 TypeError（网页曾实测复现）。
    # 统一在这里补成唯一的安全网名，后续规则不必各自防御。
    used_names = {str(n.get("name")) for n in ops.nets if isinstance(n.get("name"), str)}
    for i, net in enumerate(ops.nets, 1):
        if isinstance(net.get("name"), str) and net["name"].strip():
            continue
        name = f"NET_{i}"
        while name in used_names:
            name = f"NET_{i}_{len(used_names) + 1}"
        net["name"] = name
        used_names.add(name)
    _shorten_connector_net_names(ops)
    _fix_vbus_on_3v3_output(items, ops, symbol_lib, fixes)  # 先分离倒灌的 USB 5V，再让 _fix_power 拆 3.3V
    _fix_power(items, ops, symbol_lib, fixes)
    _fix_regulator_input(items, ops, symbol_lib, fixes)  # 稳压器 VI 悬空→补输入电源网/接口
    _fix_power_input_connector(items, ops, symbol_lib, fixes)  # 标注 5V 的输入口误接 3.3V 网→回到稳压器输入
    _fix_dangling_chip_power(items, ops, symbol_lib, fixes)  # VDD/VBAT/VDDA 悬空或单脚网→并到 3.3V
    _fix_isolated_switch_pins(items, ops, fixes)  # 按键只有一侧接线→另一侧归 GND
    _fix_reset_pullup(items, ops, symbol_lib, fixes)
    _fix_led(items, ops, symbol_lib, fixes)
    _fix_mcu_pins(items, ops, symbol_lib, fixes)
    _fix_swd_clock(items, ops, symbol_lib, fixes)
    _fix_uart_symbol(items, ops, symbol_lib, fixes)   # 先纠正型号/符号错配，再让 _fix_usb 补晶振
    _ensure_usb_uart_bridge(items, ops, symbol_lib, fixes)  # USB D± 直连 MCU 串口→补 CH340G
    _split_shared_crystal(items, ops, symbol_lib, fixes)  # 两芯片共用晶振→拆分，MCU 改用悬空的那颗
    _fix_usb(items, ops, symbol_lib, fixes)
    _fix_uart_missing_link(items, ops, symbol_lib, fixes)  # TXD/RXD 整对漏接→补到 MCU USART 脚
    _fix_uart_txrx_cross(items, ops, symbol_lib, fixes)  # TXD/RXD 与 MCU TX/RX 直连→对调
    _fix_usb_connector_ground(items, ops, symbol_lib, fixes)  # USB 口 GND 悬空 → 补地
    _fix_icsp_power_pins(items, ops, symbol_lib, fixes)  # ICSP 编程口 VCC/GND 悬空 → 补电源/地
    _fix_swd_breakout(items, ops, symbol_lib, fixes)  # SWD 信号只挂单脚 → 加排针引出
    _break_out_missing_io_ports(items, ops, symbol_lib, fixes)  # 整组 IO 漏接 → 加排针逐脚引出
    ops.drop_empty()
    _reattach_orphan_ground_pins(items, ops, fixes, symbol_lib)  # 孤立地脚先归位，避免被下面当断头件删掉
    _drop_half_wired_leds(items, ops, fixes)
    ops.drop_empty()
    _fix_crystal_load_caps(items, ops, symbol_lib, fixes)  # 晶振负载电容先补齐，别被下面断头件规则删掉
    _drop_fully_isolated_passives(items, ops, fixes)  # 两端都悬空的 R/C/L→删除
    _drop_dead_end_passives(items, ops, fixes)
    _drop_floating_passives(items, ops, fixes)
    _drop_duplicate_ghost_items(items, ops, fixes)
    _drop_stale_migration_fixes(ops, fixes)
    _sync_footprints(items, symbol_lib, fixes)  # BOM 封装脚数必须与符号一致
    _sync_connector_value_to_symbol(items, symbol_lib, fixes)  # 2xN 标称 vs 1xN 符号
    _sync_uart_bom_text(items, symbol_lib, fixes)  # 串口芯片的 BOM 文案同步
    validate_netlist(netlist, bom)
    return fixes
