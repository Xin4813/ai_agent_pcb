"""步骤④：网表 JSON → KiCad 8 原理图 (.kicad_sch)。

通过引用 KiCad 已安装的标准符号库（lib_id）来放置元件，
并用「短线 + 网络标签」把同一网络的所有引脚连起来（KiCad 中同名标签即同一网络）。
引脚坐标从本地符号库中实时解析，无需手工硬编码。
"""
from __future__ import annotations
import uuid
from pathlib import Path
from .symbol_lib import SymbolLib

_DIRS = {0: (1, 0), 180: (-1, 0), 90: (0, 1), 270: (0, -1)}


def _u() -> str:
    return str(uuid.uuid4())


def write_schematic(netlist: dict, bom: dict, output_path, symbols_dir) -> Path:
    lib = SymbolLib(symbols_dir)
    items = bom["items"]
    positions = {}
    cols, sx, sy, ox, oy = 4, 300, 250, 100, 100
    for i, it in enumerate(items):
        c, r = i % cols, i // cols
        positions[it["ref"]] = (ox + c * sx, oy + r * sy)

    L = []
    L.append("(kicad_sch")
    L.append("  (version 20231120)")
    L.append('  (generator "ai-agent-pcb")')
    L.append('  (generator_version "8.0")')
    L.append(f'  (uuid "{_u()}")')
    L.append('  (paper "A4")')
    L.append("  (title_block")
    L.append(f'    (title "{bom.get("project", "Schematic")}")')
    L.append('    (date "2026-10-21")')
    L.append('    (rev "1")')
    L.append("  )")

    # 内嵌符号定义，使原理图自带 lib_symbols，可被 kicad-cli 独立做 ERC/DRC。
    # 必须放在 (symbol ...) 实例之前（KiCad 8 schema 顺序要求）。
    L.append("  (lib_symbols")
    seen_syms = set()
    for it in items:
        for sdef in lib.get_symbol_definitions(it["symbol"]):
            if sdef not in seen_syms:
                seen_syms.add(sdef)
                L.append("    " + sdef)
    L.append("  )")

    pin_cache = {}
    for it in items:
        ref, sym = it["ref"], it["symbol"]
        # 实例引用使用「裸符号名」（去掉库前缀），让 KiCad 只从内嵌的 lib_symbols 解析，
        # 避免 kicad-cli 在缺少项目/符号库表时去查全局库而导致 ERC/加载崩溃。
        bare = sym.split(":", 1)[1] if ":" in sym else sym
        x, y = positions[ref]
        pins = lib.get_pins(sym)
        pin_cache[ref] = (sym, pins)
        nums = sorted(pins.keys())
        L.append("  (symbol")
        L.append(f'    (lib_id "{bare}")')
        L.append(f"    (at {x} {y} 0)")
        L.append("    (unit 1)")
        L.append("    (exclude_from_sim no)")
        L.append("    (in_bom yes)")
        L.append("    (on_board yes)")
        L.append("    (dnp no)")
        L.append(f'    (uuid "{_u()}")')
        L.append(f'    (property "Reference" "{ref}"')
        L.append(f"      (at {x} {y + 2.54} 0)")
        L.append('      (effects (font (size 1.27 1.27)))')
        L.append("    )")
        L.append(f'    (property "Value" "{it.get("value", "")}"')
        L.append(f"      (at {x} {y} 0)")
        L.append('      (effects (font (size 1.27 1.27)))')
        L.append("    )")
        for prop in ("Footprint", "Datasheet", "Description"):
            val = it.get(prop.lower(), "")
            L.append(f'    (property "{prop}" "{val}"')
            L.append(f"      (at {x} {y} 0)")
            L.append('      (effects (font (size 1.524 1.524)) (hide yes))')
            L.append("    )")
        for n in nums:
            L.append(f'    (pin "{n}" (uuid "{_u()}"))')
        L.append("    (instances")
        L.append('      (project "ai-agent-pcb"')
        L.append(f'        (path "/{_u()}"')
        L.append(f'          (reference "{ref}")')
        L.append("          (unit 1)")
        L.append("        )")
        L.append("      )")
        L.append("    )")
        L.append("  )")

    stub = 25.0
    for net in netlist["nets"]:
        for pinref in net["pins"]:
            ref, _, num = pinref.partition(":")
            sym, pins = pin_cache[ref]
            key = int(num)
            if key not in pins:
                continue
            px, py, ang = pins[key]
            ix, iy = positions[ref]
            ax, ay = ix + px, iy + py
            dx, dy = _DIRS.get(int(ang) % 360, (1, 0))
            ex, ey = ax + dx * stub, ay + dy * stub
            L.append("  (wire")
            L.append("    (pts")
            L.append(f"      (xy {ax} {ay}) (xy {ex} {ey})")
            L.append("    )")
            L.append('    (stroke (width 0) (type solid))')
            L.append(f'    (uuid "{_u()}")')
            L.append("  )")
            L.append(f'  (label "{net["name"]}"')
            L.append(f"    (at {ex} {ey} 0)")
            L.append('    (effects (font (size 1.27 1.27)))')
            L.append(f'    (uuid "{_u()}")')
            L.append("  )")

    L.append(")")
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(L), encoding="utf-8")
    return out
