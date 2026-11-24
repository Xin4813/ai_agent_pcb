"""确定性修补某个会话的网表：模拟排针接模拟口、ICSP 单脚接回 SPI 脚。

用于 LLM 生成时把「模拟IO排针」错接到数字口、或把 ICSP 的 MISO/MOSI/SCK
只写排针一端的情况。修补完全按「排针标注 + 芯片引脚语义名」定位，
不写死任何具体位号/引脚号，可安全用于同类其它会话；找不到明确依据时
宁可不动，绝不臆造连接。

用法：python tests/patch_session_io.py <sid>
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.config import find_kicad_symbols_dir  # noqa: E402
from src.symbol_lib import SymbolLib  # noqa: E402

SESS = ROOT / "examples" / "output" / "web_sessions"


def port_groups(pin_names: dict) -> dict[str, dict[int, str]]:
    """把芯片引脚名按端口分组：PC0-5 → {'PC': {23:'PC0', ...}}。"""
    groups: dict[str, dict[int, str]] = {}
    for num, nm in pin_names.items():
        m = re.match(r"^([A-Za-z]{1,2})(\d+)$", str(nm).strip())
        if m:
            groups.setdefault(m.group(1).upper(), {})[int(num)] = str(nm)
    return groups


def main(argv: list[str]) -> int:
    if not argv:
        print("用法: python tests/patch_session_io.py <sid>")
        return 2
    sid = argv[0]
    d = SESS / sid
    bom_path, nl_path = d / "bom.json", d / "netlist.json"
    bom = json.loads(bom_path.read_text(encoding="utf-8"))
    nl = json.loads(nl_path.read_text(encoding="utf-8"))
    items = bom["items"]
    by_ref = {it["ref"]: it for it in items}

    symdir = find_kicad_symbols_dir()
    lib = SymbolLib(str(symdir)) if symdir else None

    # --- 找主控芯片与其端口分组 ---
    mcu = next((it for it in items if "mcu_" in it.get("symbol", "").lower()), None)
    if not mcu or not lib:
        print("未找到主控芯片或符号库，跳过")
        return 1
    mref = mcu["ref"]
    names = lib.pin_names(mcu["symbol"])
    groups = port_groups(names)
    name_to_pin = {v: int(k) for k, v in names.items()}

    # --- 找模拟IO排针（desc/name 含「模拟」或 analog/adc）---
    analog_hdr = None
    for it in items:
        txt = f"{it.get('desc','')} {it.get('name','')} {it.get('value','')}".lower()
        if str(it.get("symbol", "")).lower().startswith("connector") and \
           re.search(r"模拟|analog|adc", txt):
            analog_hdr = it["ref"]
            break

    # --- 当前每个引脚所在网络 ---
    pin_net: dict[str, str] = {}
    for net in nl["nets"]:
        for p in net["pins"]:
            pin_net[p] = net["name"]
    net_by_name = {n["name"]: n for n in nl["nets"]}

    fixes: list[str] = []

    # 规则 A：模拟排针应接模拟输入端口。
    # 判定：模拟排针已接的芯片脚都是「非模拟端口」，而芯片某端口整组未接。
    if analog_hdr:
        hdr_nets = [n for n in nl["nets"]
                    if any(p.startswith(analog_hdr + ":") for p in n["pins"])]
        # 模拟排针当前接到的芯片端口集合
        cur_ports = set()
        for n in hdr_nets:
            for p in n["pins"]:
                if not p.startswith(mref + ":"):
                    continue
                num = int(p.split(":")[1])
                for port, pins in groups.items():
                    if num in pins:
                        cur_ports.add(port)
        # 整组未接的端口
        free_groups = []
        for port, pins in sorted(groups.items()):
            if all(f"{mref}:{pn}" not in pin_net for pn in pins):
                free_groups.append(port)
        analog_like = [g for g in free_groups if re.match(r"^(PC|ADC|AIN|AN)", g, re.I)]
        # 模拟排针当前接的不是模拟口，且存在整组未接的模拟口 → 改接
        if analog_like and cur_ports and not (cur_ports & set(analog_like)):
            target = analog_like[0]
            tps = sorted(groups[target].items())
            hdr_nums = sorted({int(p.split(":")[1])
                               for n in hdr_nets for p in n["pins"]
                               if p.startswith(analog_hdr + ":")})
            if len(tps) >= len(hdr_nums):
                released: list[str] = []
                for i, hn in enumerate(hdr_nums):
                    pin = f"{analog_hdr}:{hn}"
                    old = pin_net.get(pin)
                    chip_pin = f"{mref}:{tps[i][0]}"
                    new_name = groups[target][tps[i][0]]  # 用真实引脚名，如 PC0
                    # 用模拟输入的通用名 A{i}，便于阅读与标签
                    new_name = f"A{i}"
                    # 从旧网移除排针脚
                    if old in net_by_name:
                        net_by_name[old]["pins"] = [x for x in net_by_name[old]["pins"]
                                                     if x != pin]
                        # 记录旧网上被释放的芯片脚（原数字口）
                        for x in nl["nets"]:
                            if x["name"] == old:
                                released += [x for x in x["pins"] if x.startswith(mref + ":")]
                    # 接到新网
                    if new_name in net_by_name:
                        net_by_name[new_name]["pins"].append(chip_pin)
                        net_by_name[new_name]["pins"].append(pin)
                    else:
                        nn = {"name": new_name, "pins": [chip_pin, pin]}
                        nl["nets"].append(nn)
                        net_by_name[new_name] = nn
                    fixes.append(f"{pin} 从数字口改接模拟输入 {chip_pin}（{new_name}）")
                # 清理释放出来的芯片脚所在的空网 / 孤立网
                for x in list(nl["nets"]):
                    if x["name"] in net_by_name and len(x["pins"]) == 1 and \
                            x["pins"][0].startswith(mref + ":"):
                        # 保留，稍后规则 B 可能接回 SPI；其余悬空合法
                        pass

    # 规则 B：ICSP 的 MISO/MOSI/SCK 单脚网接回芯片 SPI 脚。
    # 用引脚语义名定位（PB3=MOSI/PB4=MISO/PB5=SCK），找不到就不动。
    spi_by_sem = {"MISO": "PB4", "MOSI": "PB3", "SCK": "PB5"}
    for netname, sem in spi_by_sem.items():
        net = net_by_name.get(netname)
        if not net or len(net["pins"]) != 1:
            continue
        only = net["pins"][0]
        if not only.startswith("J"):
            continue
        target_num = name_to_pin.get(sem)
        if target_num is None:
            continue
        chip_pin = f"{mref}:{target_num}"
        chip_net = pin_net.get(chip_pin)
        if chip_net == netname:
            continue
        if chip_net is not None:
            # 已在别的网：只有当它是「芯片单脚网」（刚从模拟排针释放出来的
            # 未用数字口）时才移走；若真接了别的器件则不动，绝不臆造。
            src = net_by_name.get(chip_net)
            if not src or len(src["pins"]) != 1 or src["pins"][0] != chip_pin:
                continue
            src["pins"] = []
        net["pins"].append(chip_pin)
        pin_net[chip_pin] = netname
        fixes.append(f"ICSP {netname} 接回芯片 {sem}({chip_pin})")

    # 丢弃空网
    nl["nets"] = [n for n in nl["nets"] if n["pins"]]

    print(f"会话 {sid}: 修补 {len(fixes)} 处")
    for f in fixes:
        print("  -", f)
    if fixes:
        nl_path.write_text(json.dumps(nl, ensure_ascii=False, indent=2), encoding="utf-8")
        print("已写回", nl_path)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
