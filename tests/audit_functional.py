"""功能性不变量审计：找出 ERC / 视觉 / safety 都可能漏掉的结构性错误。

与 audit_full.py 的区别：后者调用现有规则做回归；本脚本独立检查「物理上
不可能正确」的硬不变量，用来发现规则缺口。

用法：python tests/audit_functional.py [<sid> ...]
退出码：发现任一不变量违例返回 1。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.symbol_lib import SymbolLib  # noqa: E402

SYMDIR = Path(r"C:/Users/Gray/AppData/Local/Programs/KiCad/8.0/share/kicad/symbols")
SESS = ROOT / "examples" / "output" / "web_sessions"
GND_RE = re.compile(r"gnd|vss|ground|地", re.I)


def audit(sid: str, lib: SymbolLib) -> list[str]:
    d = SESS / sid
    bom = json.loads((d / "bom.json").read_text(encoding="utf-8"))
    nl = json.loads((d / "netlist.json").read_text(encoding="utf-8"))
    items = bom.get("items", [])
    nets = nl.get("nets", [])
    issues: list[str] = []

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
                if (r, n) in pin_net:
                    issues.append(f"引脚 {p} 同时出现在 {pin_net[(r, n)]} 与 {name}")
                pin_net[(r, n)] = name
        net_pins[name] = members

    # 1) BOM 与网表元件集合一致（旧 bug：BOM 有 Y1、网表挂 Y2）
    bom_refs, net_refs = set(by_ref), {r for r, _ in pin_net}
    for r in sorted(bom_refs - net_refs):
        issues.append(f"BOM 中 {r}（{by_ref[r].get('value','')}）在网表里无任何引脚")
    for r in sorted(net_refs - bom_refs):
        issues.append(f"网表引用了 BOM 中不存在的位号 {r}")

    # 2) 单成员网（悬空网络）
    for name, members in net_pins.items():
        if GND_RE.search(name):
            continue
        if len(members) == 1:
            issues.append(f"网络 {name} 只有 1 个成员 {next(iter(members))}，是个悬空线头")

    # 3) 无源器件引脚悬空（电阻/电容/LED/晶振的每个脚都必须接网）
    for it in items:
        sym = it.get("symbol", "").lower()
        if not any(k in sym for k in (":r", ":c", ":led", "crystal", ":l")):
            continue
        # 探测实际引脚号：优先符号库
        nums: set[str] = set()
        if lib:
            try:
                nums = {str(n) for n in lib.get_pins(it.get("symbol", "")).keys()}
            except Exception:
                nums = set()
        if not nums:
            nums = {"1", "2"}
        for n in sorted(nums):
            if (it["ref"], n) not in pin_net:
                issues.append(f"{it['ref']}（{it.get('value','')}）的 {n} 脚悬空，未接任何网络")

    # 4) 有源芯片的电源/地脚必须接网
    for it in items:
        sym = it.get("symbol", "").lower()
        if not any(k in sym for k in ("mcu_", "regulator", "interface_", "interface_usb")):
            continue
        if lib:
            try:
                names = lib.pin_names(it.get("symbol", ""))
            except Exception:
                names = {}
        else:
            names = {}
        if names:
            for pname in ("VCC", "VDD", "GND", "VSS", "VI", "VIN"):
                ps = [num for num, nm in names.items()
                      if str(nm).upper() in (pname,)]
                for p in ps:
                    if (it["ref"], str(p)) not in pin_net:
                        issues.append(f"{it['ref']} 的 {pname}({p}) 供电脚悬空")

    # 5) 稳压器输入/输出不能同网，GND 脚必须接地
    for it in items:
        sym = it.get("symbol", "").lower()
        if "regulator" not in sym:
            continue
        if not lib:
            continue
        try:
            names = lib.pin_names(it.get("symbol", ""))
        except Exception:
            names = {}
        vi = [str(n) for n, nm in names.items() if nm in ("VI", "VIN")]
        vo = [str(n) for n, nm in names.items() if nm in ("VO", "VOUT")]
        gnd = [str(n) for n, nm in names.items() if nm in ("GND",)]
        if vi and vo:
            ni = {pin_net.get((it["ref"], p)) for p in vi}
            no = {pin_net.get((it["ref"], p)) for p in vo}
            if ni & no and None not in ni | no:
                issues.append(f"稳压器 {it['ref']} 输入脚 {vi} 与输出脚 {vo} 同网 {ni & no}，被短接")
        for p in gnd:
            n = pin_net.get((it["ref"], p))
            if n and not GND_RE.search(n):
                issues.append(f"稳压器 {it['ref']} 的 GND({p}) 接到了非地网 {n}")

    # 6) LED 不能短路、阴阳极都要接网
    for it in items:
        sym = it.get("symbol", "").lower()
        if "led" not in sym:
            continue
        a, k = pin_net.get((it["ref"], "2")), pin_net.get((it["ref"], "1"))
        if a is None or k is None:
            continue
        if a == k:
            issues.append(f"LED {it['ref']} 阳极与阴极同网 {a}，被短路")

    return issues


def main(argv: list[str]) -> int:
    lib = SymbolLib(str(SYMDIR)) if SYMDIR.exists() else None
    only = set(argv[1:])
    total = 0
    for d in sorted(SESS.iterdir()):
        if not d.is_dir() or (only and d.name not in only):
            continue
        if not (d / "bom.json").exists():
            continue
        issues = audit(d.name, lib)
        if issues:
            print(f"\n{d.name}: {len(issues)} 处功能性疑点")
            for s in issues:
                print(f"    - {s}")
            total += len(issues)
    print(f"\n合计 {total} 处功能性疑点")
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
