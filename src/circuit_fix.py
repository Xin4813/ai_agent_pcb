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
        if not a_net or not k_net or not _GND_RE.search(k_net):
            continue
        members = ops.members(a_net)
        if not any(p.partition(":")[0] in mcu_refs for p in members):
            continue                      # 阳极网没有 MCU 驱动脚 → 不是 GPIO 直驱
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
        if "led" not in net_l:
            continue                      # 只处理含 LED 语义的悬空网
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
    _fix_floating_led_gpio(items, ops, lib, fixes)
    _fix_led_dead_loop(items, ops, lib, fixes)


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

    # 找主电源网：按引脚语义（芯片 VCC/VDD 脚、稳压器 VO 脚），网名仅兜底。
    # 不能只靠 _SUPPLY_RE 匹配网名——LLM 会把电源网命名成 PWR_5V 之类，
    # 换名后网名判据失效（曾导致换名验证里规则整个跳过）。
    supply = None
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

        # 情形二：电阻另一端落在「另一颗 LED 的阳极网」，而该网同样走不到
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
    if (len(anode) == 2 and any(
            r != led["ref"]
            and by_ref.get(r, {}).get("symbol", "").lower().startswith("device:r")
            for r, _n in anode)):
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
        fixes.append(f"{uart['ref']} 未找到 USB 5V 来源，跳过 D+/D− 修正")
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

    # ④ CH340G 的 V3 退耦：内部 3.3V 输出需要 100nF 到 GND（即使悬空也要补，
    #    不能把 V3 输出直接并到板上 3.3V 电源网，否则两个电源输出相冲）。
    v3p = next(iter(_p(uart, lib, "V3")), "4")
    v3_pin = f"{ur}:{v3p}"
    v3_net = ops.name(v3_pin)
    if not v3_net:
        v3_net = f"{ur}_V3"
        ops.attach(v3_pin, v3_net)
        fixes.append(f"为 {ur} 的 V3 建立独立退耦网 {v3_net}")
    has_v3_cap = False
    for it in items:
        sym = it.get("symbol", "").lower()
        val = str(it.get("value", "")).lower()
        if not (sym.endswith(":c") or "cap" in sym or "电容" in str(it.get("name", ""))):
            continue
        if "n" not in val and "u" not in val:
            continue
        for p, other in (("1", "2"), ("2", "1")):
            if ops.name(f"{it['ref']}:{p}") == v3_net and ops.name(f"{it['ref']}:{other}") == gnd:
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
    vcc_pins = _p(uart, lib, "VCC")
    if not vcc_pins:
        # 查不到符号里的 VCC 脚就跳过，绝不硬编码 pin 号：
        # CH340G 的 VCC=16，但 CP2102 的 16 脚是 WAKEUP/GPIO.3，硬编码会接错。
        vcc_pins = []
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
        new_sym = "Interface_USB:CH340G"
        it["symbol"] = new_sym
        if not it.get("name") or "串口" not in str(it.get("name", "")):
            it["name"] = "USB转串口（CH340G）"
        it["value"] = "CH340G"

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
                f"{ref} 型号与符号不符（{old_sym}），已改用 CH340G 并按网络语义重映射引脚："
                + "，".join(remapped))


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
            if not on or not _GND_RE.search(on):
                continue
            ops.attach(other, vdd)
            rit["name"] = "电阻（NRST 上拉）"
            rit["desc"] = "NRST 上拉电阻，复位按键按下时接 GND"
            fixes.append(
                f"{ref} 从 GND 改接 {vdd}，作为 {it['ref']} 的 NRST 上拉"
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
            # 情形二：一端接 GND/电源、另一端接在「只有它自己」的孤立网
            # （LLM 把限流电阻接成 "R→孤立LED网" 的死支路，ERC/功能自检曾漏过）
            for fixed_pin, fixed_net, lone_pin, lone_net in (
                    (p1, n1, p2, n2), (p2, n2, p1, n1)):
                if not (_GND_RE.search(fixed_net) or _SUPPLY_RE.match(fixed_net)):
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
    swdio = next((n for n in ops.nets if re.search(r"swdio", n["name"], re.I)), None)
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
    swclk = next((n for n in ops.nets if re.search(r"swclk|swdclk", n["name"], re.I)), None)
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


def fix_topology(bom: dict, netlist: dict, symbol_lib=None) -> list[str]:
    """就地修正可检测到的拓扑错误，返回修正说明列表。"""
    items = bom.get("items", [])
    ops = _Nets(netlist)
    fixes: list[str] = []
    if not items:
        return fixes
    _shorten_connector_net_names(ops)
    _fix_vbus_on_3v3_output(items, ops, symbol_lib, fixes)  # 先分离倒灌的 USB 5V，再让 _fix_power 拆 3.3V
    _fix_power(items, ops, symbol_lib, fixes)
    _fix_reset_pullup(items, ops, symbol_lib, fixes)
    _fix_led(items, ops, symbol_lib, fixes)
    _fix_mcu_pins(items, ops, symbol_lib, fixes)
    _fix_swd_clock(items, ops, symbol_lib, fixes)
    _fix_uart_symbol(items, ops, symbol_lib, fixes)   # 先纠正型号/符号错配，再让 _fix_usb 补晶振
    _fix_usb(items, ops, symbol_lib, fixes)
    ops.drop_empty()
    _drop_half_wired_leds(items, ops, fixes)
    _drop_dead_end_passives(items, ops, fixes)
    _drop_floating_passives(items, ops, fixes)
    _drop_duplicate_ghost_items(items, ops, fixes)
    _drop_stale_migration_fixes(ops, fixes)
    validate_netlist(netlist, bom)
    return fixes
