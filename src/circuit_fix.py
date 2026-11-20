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
from .safety import _pins, _regulator_output, _chip_vcc

_SUPPLY_RE = re.compile(r"^(vcc|vdd|vbus|\+?\d+(?:\.\d+)?v|3v3|5v)", re.I)
_V33_NAME_RE = re.compile(r"3v3|\+3\.3|3\.3v", re.I)


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


def _hot_nets(ops, items, lib) -> set[str]:
    """收集 5V 来源网络：USB VBUS + 所有 5V 稳压器输出。"""
    hot: set[str] = set()
    for it in items:
        t = f"{it.get('value','')} {it.get('symbol','')}".lower()
        if "conn_01x05" in it.get("symbol", "").lower() or "usb_micro" in t:
            vbus = next(iter(_p(it, lib, "VBUS")), "1")
            n = ops.name(f"{it['ref']}:{vbus}")
            if n:
                hot.add(n)
        vout = _regulator_output(it)
        if vout is not None and vout >= 4.5:
            for p in _p(it, lib, "VO"):
                n = ops.name(f"{it['ref']}:{p}")
                if n:
                    hot.add(n)
    return hot


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

    v33 = _v33_net(ops, items, lib)
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
        if not (it.get("symbol", "").lower().startswith("device:r")
                or it.get("symbol", "").lower().startswith("device:c")):
            continue
        for p, other in (("1", "2"), ("2", "1")):
            if ops.name(f"{it['ref']}:{p}") in hot:
                other_net = ops.name(f"{it['ref']}:{other}")
                if other_net and any(x.startswith(r + ":") for x in ops.members(other_net)
                                     for r in chip_refs):
                    ops.attach(f"{it['ref']}:{p}", v33)
    fixes.append(f"把 {', '.join(chip_refs)} 的 {'/'.join(moved_names)} 从 {old} 迁到 {v33}")


def _fix_led(items, ops, lib, fixes):
    """逐个 LED 检查：阳极若直接跨电源则补串联限流电阻。"""
    leds = [it for it in items if "led" in
            f"{it.get('value','')} {it.get('symbol','')}".lower()]
    for led in leds:
        _fix_one_led(led, items, ops, lib, fixes)


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
    if (len(anode) == 2 and any(
            r != led["ref"]
            and by_ref.get(r, {}).get("symbol", "").lower().startswith("device:r")
            for r, _n in anode)):
        return

    # 优先找一个「一端接芯片 GPIO、另一端空闲」的电阻作限流电阻
    candidate = None
    for it in items:
        if not it.get("symbol", "").lower().startswith("device:r"):
            continue
        for num, other in (("1", "2"), ("2", "1")):
            n = ops.name(f"{it['ref']}:{num}")
            on = ops.name(f"{it['ref']}:{other}")
            other_members = ops.members(on) if on else set()
            # 另一端必须是「只有它自己」的悬空脚，避免抢走别的元件连线
            if (n and not _SUPPLY_RE.match(n)
                    and any(":" in x and x.partition(":")[0] != led["ref"]
                            for x in ops.members(n))
                    and on and not _SUPPLY_RE.match(on)
                    and other_members == {f"{it['ref']}:{other}"}):
                candidate = (it, other)
                break
        if candidate:
            break

    led_net = f"LED_A_{led['ref']}"        # 每个 LED 独立网络，避免互相短接
    if candidate:
        res, other = candidate
        ops.attach(a_pin, led_net)
        ops.attach(f"{res['ref']}:{other}", led_net)
        ops.attach(k_pin, "GND")
        fixes.append(f"{led['ref']} 改为 GPIO→{res['ref']}(限流)→LED→GND")
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


def _fix_usb(items, ops, lib, fixes):
    """CH340G + Micro-USB 时修正 D+/D− 交叉，并给 XI/XO 补晶振。"""
    uart = _find(items, "ch340")
    usb = next((it for it in items
                if "conn_01x05" in it.get("symbol", "").lower()
                or "usb_micro" in it.get("symbol", "").lower()), None)
    if not (uart and usb):
        return
    ur, usr = uart["ref"], usb["ref"]
    ud_p = next(iter(_p(uart, lib, "UD+")), "5")
    ud_m = next(iter(_p(uart, lib, "UD-")), "6")
    d_p = next(iter(_p(usb, lib, "D+")), "3")
    d_m = next(iter(_p(usb, lib, "D-")), "2")
    dp = ops.find(f"{ur}:{ud_p}")
    dm = ops.find(f"{ur}:{ud_m}")
    if dp and dm:
        dp_usb = [p for p in dp["pins"] if p.startswith(usr + ":")]
        dm_usb = [p for p in dm["pins"] if p.startswith(usr + ":")]
        if dp_usb and f"{usr}:{d_p}" not in dp["pins"]:
            # 用 attach 统一处理：它会先把引脚从所有网络移除再加入，
            # 避免两网直接改 list 造成同一引脚残留两处（validate 冲突）。
            ops.attach(f"{usr}:{d_p}", dp["name"])
            ops.attach(f"{usr}:{d_m}", dm["name"])
            for p in dp_usb + dm_usb:
                if p not in (f"{usr}:{d_p}", f"{usr}:{d_m}"):
                    # 其它 USB 引脚保持原网络，不处理
                    pass
            fixes.append(f"USB D+/D− 对调：{usr}:{d_p}→{ur}:{ud_p}，{usr}:{d_m}→{ur}:{ud_m}")

    xi = next(iter(_p(uart, lib, "XI")), "7")
    xo = next(iter(_p(uart, lib, "XO")), "8")
    xi_net, xo_net = ops.name(f"{ur}:{xi}"), ops.name(f"{ur}:{xo}")
    if (not xi_net or len(ops.members(xi_net)) < 2
            or not xo_net or len(ops.members(xo_net)) < 2):
        # 必须新建独立晶振，不能复用 MCU 的晶振（两个芯片不能共用）
        ref = _add_ref(items, "Y")
        items.append({"ref": ref, "name": "晶振 12MHz（CH340G）", "value": "12MHz",
                      "qty": 1, "symbol": "Device:Crystal", "footprint": ""})
        ops.attach(f"{ur}:{xi}", "XTALI")
        ops.attach(f"{ur}:{xo}", "XTALO")
        ops.attach(f"{ref}:1", "XTALI")
        ops.attach(f"{ref}:2", "XTALO")
        fixes.append(f"新增 {ref}（12MHz 晶振）给 {ur} 的 XI/XO，USB 转串口可起振")


def fix_topology(bom: dict, netlist: dict, symbol_lib=None) -> list[str]:
    """就地修正可检测到的拓扑错误，返回修正说明列表。"""
    items = bom.get("items", [])
    ops = _Nets(netlist)
    fixes: list[str] = []
    if not items:
        return fixes
    _fix_power(items, ops, symbol_lib, fixes)
    _fix_led(items, ops, symbol_lib, fixes)
    _fix_mcu_pins(items, ops, symbol_lib, fixes)
    _fix_usb(items, ops, symbol_lib, fixes)
    ops.drop_empty()
    validate_netlist(netlist, bom)
    return fixes
