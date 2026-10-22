"""步骤④：网表 JSON → KiCad 8 原理图 (.kicad_sch)。

通过引用 KiCad 已安装的标准符号库（lib_id）来放置元件，
并用「短线 + 网络标签」把同一网络的所有引脚连起来（KiCad 中同名标签即同一网络）。
引脚坐标从本地符号库中实时解析，无需手工硬编码。

布局与坐标要点（踩过的坑）：
1. 符号库局部坐标是 Y 轴向上，而原理图实例坐标 Y 轴向下。
   因此引脚绝对坐标 = (sx + px, sy - py)。
2. 引脚的 (at ... angle) 中 angle 是「从连接点指向符号体」的方向，
   引出线必须走**反方向**（外侧），否则线会穿进符号体、与别的网络交叉
   （曾导致 multiple_net_names / label_dangling）。
3. ERC 的「栅格」是 50 mil = 1.27 mm。KiCad 标准符号的引脚偏移都是 1.27 的
   整数倍，所以只要把符号原点对齐到 1.27 栅格，所有引脚/线端点即自动在栅格上。
4. 布局按每个符号「引脚包围盒」动态计算格子大小，贪心逐行排布在 A4 页内，
   避免元件重叠或超出页面。
5. VCC/GND 等电源网络自动补 PWR_FLAG（电源输出），否则 ERC 必报
   「输入电源引脚不受任何输出电源引脚驱动」。
6. 没接入任何网络的引脚按 KiCad 规范打 no_connect（×）标记，ERC 不再报错。
7. LLM 可能把相同元件合并成一个位号（如 "C1,C2"），这里拆成独立实例。
"""
from __future__ import annotations
import json
import re
import uuid
from pathlib import Path
from .symbol_lib import SymbolLib

# 符号库局部坐标的四个朝向单位向量（y 向上，指向符号体）。
_DIRS = {0: (1, 0), 180: (-1, 0), 90: (0, 1), 270: (0, -1)}

_POWER_RE = re.compile(
    r"^(vcc|vdd|vss|vee|gnd|agnd|dgnd|avcc|avdd|\+?\d+(?:\.\d+)?v)$", re.I)

GRID = 1.27          # KiCad 默认栅格 50 mil
STUB = 7.62          # 引脚引出线长度（6mm：引出点在符号体外，短线+标签即可；
                     # 且 |引脚偏移|+7.62 ≤ 包围盒半高(=|引脚偏移|+12)，导线永远
                     # 伸不出自己的格子，结构上杜绝与相邻元件的导线共线重叠）
MARGIN = 12.0        # 符号包围盒外扩（给 Reference/Value 文字留空间）
CELL_GAP = 18.0      # 相邻元件格子的间距（容纳引出线与标签）
# (页宽, 页高)：A4 纵向装不下时自动升 A3
PAPERS = {"A4": (297.0, 210.0), "A3": (420.0, 297.0)}
PAGE_MX, PAGE_MY = 20.0, 20.0   # 页边距


def _u() -> str:
    return str(uuid.uuid4())


def _snap(v: float, grid: float = GRID) -> float:
    return round(v / grid) * grid


def _fmt(v: float) -> str:
    s = f"{v:.4f}".rstrip("0").rstrip(".")
    return s if s else "0"


def _expand_refs(items: list) -> tuple[list, dict]:
    """把 LLM 合并的位号（如 "C1,C2"）拆成独立实例。

    返回 (展开后的 items, {原合并位号: 第一个拆分位号})，
    后者用于网表引用合并位号时兜底连接。
    """
    out, alias = [], {}
    for it in items:
        ref = str(it.get("ref", "")).strip()
        parts = [r.strip() for r in ref.split(",") if r.strip()]
        if len(parts) > 1:
            alias[ref] = parts[0]
            for r in parts:
                out.append({**it, "ref": r})
        else:
            out.append(it)
    return out, alias


def _layout(items, lib) -> tuple[dict, str]:
    """逐列紧凑排布（按高度降序装箱），返回 ({ref: (x, y)}, 纸张规格)。

    - 每个元件占一个 (2×半宽, 2×半高+间距) 的格子，半宽/半高由引脚包围盒决定；
    - 在一列内自上而下堆叠，列满则开新列；A4 装不下自动换 A3；
    - 坐标对齐 1.27 栅格，保证所有引脚/线端点落在 KiCad 栅格上。
    """
    cells = []
    for it in items:
        pins = lib.get_pins(it["symbol"])
        if pins:
            mx = max(abs(p[0]) for p in pins.values())
            my = max(abs(p[1]) for p in pins.values())
        else:
            mx = my = 5.0
        # 高度降序：大元件（如 MCU）先放，装箱更紧凑
        cells.append((it["ref"], mx + MARGIN, my + MARGIN))
    cells.sort(key=lambda c: -c[2])

    for paper in ("A4", "A3"):
        pw, ph = PAPERS[paper]
        usable_w, usable_h = pw - 2 * PAGE_MX, ph - 2 * PAGE_MY
        positions = {}
        x_left = 0.0            # 当前列左边界（相对页边距）
        col_w = 0.0             # 当前列最大格宽
        y = 0.0                 # 当前列已用高度
        overflow = False
        for ref, hw, hh in cells:
            ch = 2 * hh + CELL_GAP
            if y + ch > usable_h and y > 0:     # 当前列放不下 → 开新列
                x_left += col_w + CELL_GAP
                y, col_w = 0.0, 0.0
            if 2 * hw > usable_w:               # 单元件超页宽（极端情况）
                overflow = True
            positions[ref] = (_snap(PAGE_MX + x_left + col_w / 2 + hw),
                              _snap(PAGE_MY + y + hh))
            y += ch
            col_w = max(col_w, 2 * hw)
        if not overflow and x_left + col_w <= usable_w:
            return positions, paper
    # A3 仍装不下（几乎不可能）：放宽为不检查，直接用 A3 结果
    return positions, "A3"


def write_schematic(netlist: dict, bom: dict, output_path, symbols_dir) -> Path:
    lib = SymbolLib(symbols_dir)
    items, alias = _expand_refs(bom["items"])
    positions, paper = _layout(items, lib)
    pin_cache = {it["ref"]: (it["symbol"], lib.get_pins(it["symbol"])) for it in items}

    # 每个符号的占位包围盒 (x, y, 半宽, 半高)，供 PWR_FLAG 放置避让
    boxes = []
    for it in items:
        x, y = positions[it["ref"]]
        _, pins = pin_cache[it["ref"]]
        hw = max((abs(p[0]) for p in pins.values()), default=5.0) + MARGIN
        hh = max((abs(p[1]) for p in pins.values()), default=5.0) + MARGIN
        boxes.append((x, y, hw, hh))

    def _hits_symbol(fx: float, fy: float) -> bool:
        """电源旗标放置点是否压到任何符号包围盒（留 2mm 缓冲）。"""
        return any(abs(fx - bx) < bw + 2 and abs(fy - by) < bh + 2
                   for bx, by, bw, bh in boxes)

    def _resolve(ref: str) -> str | None:
        """网表位号 → 展开后的实例位号（兼容 LLM 合并位号的引用）。"""
        if ref in pin_cache:
            return ref
        return alias.get(ref)

    def _net_valid_pins(net):
        """返回网络中合法且可解析的 (位号, 引脚号) 列表。"""
        out = []
        for pinref in net.get("pins", []):
            ref, _, num = pinref.partition(":")
            r = _resolve(ref)
            if r is None:
                continue
            try:
                key = int(num)
            except ValueError:
                continue
            if key in pin_cache[r][1]:
                out.append((r, key))
        return out

    # 收集已连接的 (位号, 引脚号)，其余引脚打 no_connect。
    # 单引脚网络（只有 1 个有效引脚）没有信号意义，且 KiCad 8 会把这种
    # 网络的标签判为悬空（label_dangling），因此直接按未连接处理。
    connected = set()
    for net in netlist["nets"]:
        ok = _net_valid_pins(net)
        if len(ok) >= 2:
            connected.update(ok)

    # 需要补 PWR_FLAG 的电源网络（每网 1 个）
    power_nets = [n for n in netlist["nets"]
                  if _POWER_RE.match(n.get("name", ""))
                  and len(_net_valid_pins(n)) >= 2]

    L = []
    L.append("(kicad_sch")
    L.append("  (version 20231120)")
    L.append('  (generator "ai-agent-pcb")')
    L.append('  (generator_version "8.0")')
    L.append(f'  (uuid "{_u()}")')
    L.append(f'  (paper "{paper}")')
    L.append("  (title_block")
    L.append(f'    (title "{bom.get("project", "Schematic")}")')
    L.append('    (date "2026-10-21")')
    L.append('    (rev "1")')
    L.append("  )")

    # 内嵌符号定义（带库前缀全名，与实例 lib_id 一致），使原理图可被 kicad-cli
    # 独立做 ERC/DRC。必须放在 (symbol ...) 实例之前（KiCad 8 schema 顺序要求）。
    L.append("  (lib_symbols")
    seen_syms = set()
    for it in items:
        for sdef in lib.get_symbol_definitions(it["symbol"], qualified=True):
            if sdef not in seen_syms:
                seen_syms.add(sdef)
                L.append("    " + sdef)
    if power_nets:
        for sdef in lib.get_symbol_definitions("power:PWR_FLAG", qualified=True):
            if sdef not in seen_syms:
                seen_syms.add(sdef)
                L.append("    " + sdef)
    L.append("  )")

    def _emit_symbol(ref, sym, x, y, pins, value, in_bom="yes", hide_text=False):
        """输出一个符号实例（带库前缀 lib_id，内嵌定义同名）。

        hide_text=True 时隐藏 Reference/Value 文字（用于 PWR_FLAG 这类
        内部标记——KiCad 规范里它们本就不显示，否则 "PWR_FLAG" 长文字
        会压住旁边元件的网络标签与 Reference/Value）。
        """
        hw = hh = MARGIN
        if pins:
            hw = max(abs(p[0]) for p in pins.values()) + MARGIN
            hh = max(abs(p[1]) for p in pins.values()) + MARGIN
        hide = " (hide yes)" if hide_text else ""
        L.append("  (symbol")
        L.append(f'    (lib_id "{sym}")')
        L.append(f"    (at {_fmt(x)} {_fmt(y)} 0)")
        L.append("    (unit 1)")
        L.append("    (exclude_from_sim no)")
        L.append(f"    (in_bom {in_bom})")
        L.append("    (on_board yes)")
        L.append("    (dnp no)")
        L.append(f'    (uuid "{_u()}")')
        L.append(f'    (property "Reference" "{ref}"')
        L.append(f"      (at {_fmt(x)} {_fmt(y - hh - 2.54)} 0)")
        L.append(f'      (effects (font (size 1.27 1.27)){hide})')
        L.append("    )")
        L.append(f'    (property "Value" "{value}"')
        L.append(f"      (at {_fmt(x)} {_fmt(y + hh + 2.54)} 0)")
        L.append(f'      (effects (font (size 1.27 1.27)){hide})')
        L.append("    )")
        for prop in ("Footprint", "Datasheet", "Description"):
            L.append(f'    (property "{prop}" ""')
            L.append(f"      (at {_fmt(x)} {_fmt(y)} 0)")
            L.append('      (effects (font (size 1.524 1.524)) (hide yes))')
            L.append("    )")
        for n in sorted(pins.keys()):
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

    for it in items:
        ref, sym = it["ref"], it["symbol"]
        x, y = positions[ref]
        _, pins = pin_cache[ref]
        _emit_symbol(ref, sym, x, y, pins, it.get("value", ""))

    # 引脚引出线 + 网络标签 + 电源 PWR_FLAG + 未连引脚的 no_connect 标记。
    # 方向要点：pin 的 angle 指向符号体，引出线走外侧（反向）；Y 轴要翻转。
    # 同一位置 + 同一网络名只发一次：KiCad 官方符号存在同坐标叠放的引脚
    # （如 ATmega328P 的 8/22 脚都是 GND，定义在同一点），重复发线既冗余又难看。
    # 同一网络上相距很近的端点（如 7/20 脚都在顶边、仅隔 2.54mm）不用两个
    # 标签——文字会互相压住——而是用短导线互连、只挂一个标签。
    emitted = set()
    flag_no = 0
    for net in netlist["nets"]:
        ok = _net_valid_pins(net)
        if len(ok) < 2:                           # 单引脚网络 → 引脚打 no_connect
            continue
        name = net.get("name", "")
        is_power = bool(_POWER_RE.match(name))
        flag_done = False

        # ① 收集该网络所有引出线端点（同坐标去重）
        ends = []
        for r, key in ok:
            sym, pins = pin_cache[r]
            px, py, ang = pins[key]
            ix, iy = positions[r]
            ax, ay = ix + px, iy - py             # Y 取反（库 Y 向上 → 原理图 Y 向下）
            dx, dy = _DIRS.get(int(ang) % 360, (1, 0))
            ex, ey = ax - dx * STUB, ay + dy * STUB
            k = (round(ax, 3), round(ay, 3), name)
            if k in emitted:
                continue
            emitted.add(k)
            ends.append((ax, ay, ex, ey, -dx, dy))

        # ② 同网邻近端点互连（水平同线且间距 ≤15.24mm，且两根引出线同朝上/下；
        #    垂直同理）。被连接的第二端不再挂标签。
        merged = set()
        links = []
        for i in range(len(ends)):
            ax, ay, ex, ey, odx, ody = ends[i]
            for j in range(i + 1, len(ends)):
                bx, by, fx, fy, pdx, pdy = ends[j]
                if (ex == ax and fx == bx and ay == by
                        and 0 < abs(ax - bx) <= 15.24
                        and (ey - ay) * (fy - by) > 0):
                    links.append((i, j))
                    merged.add(j)
                elif (ey == ay and fy == by and ax == bx
                        and 0 < abs(ay - by) <= 15.24
                        and (ex - ax) * (fx - bx) > 0):
                    links.append((i, j))
                    merged.add(j)

        def _wire(x1, y1, x2, y2):
            L.append("  (wire")
            L.append("    (pts")
            L.append(f"      (xy {_fmt(x1)} {_fmt(y1)}) (xy {_fmt(x2)} {_fmt(y2)})")
            L.append("    )")
            L.append('    (stroke (width 0) (type solid))')
            L.append(f'    (uuid "{_u()}")')
            L.append("  )")

        # ③ 输出引出线、互连线、标签、PWR_FLAG
        for idx, (ax, ay, ex, ey, odx, ody) in enumerate(ends):
            _wire(ax, ay, ex, ey)
            if is_power and not flag_done:
                # 电源网络：在第一条引出线末端补 PWR_FLAG（电源输出，消
                # 「输入电源引脚不受任何输出电源引脚驱动」）。标签省略，
                # 网络名由其余引脚的标签提供。放置方向带碰撞检测：
                # 依次尝试上方/下方/外侧，选中不压任何符号包围盒的位置。
                flag_no += 1
                cands = [(0.0, -12.7), (0.0, 12.7),
                         (odx * 12.7, ody * 12.7), (-odx * 12.7, -ody * 12.7)]
                fx = fy = None
                for ox, oy in cands:
                    tx, ty = _snap(ex + ox), _snap(ey + oy)
                    if not _hits_symbol(tx, ty):
                        fx, fy = tx, ty
                        break
                if fx is None:                      # 全部冲突：退回外侧最远点
                    fx, fy = _snap(ex + odx * 12.7), _snap(ey + ody * 12.7)
                _wire(ex, ey, fx, fy)
                _emit_symbol(f"#FLG{flag_no:02d}", "power:PWR_FLAG", fx, fy,
                             {1: (0.0, 0.0, 90.0)}, "PWR_FLAG", in_bom="no",
                             hide_text=True)
                flag_done = True
            if idx not in merged:
                langle = 180 if odx > 0 else 0      # 外侧朝左时标签转 180°，可读性更好
                L.append(f'  (label "{name}"')
                L.append(f"    (at {_fmt(ex)} {_fmt(ey)} {langle})")
                L.append('    (effects (font (size 1.27 1.27)))')
                L.append(f'    (uuid "{_u()}")')
                L.append("  )")
        for i, j in links:
            _wire(ends[i][2], ends[i][3], ends[j][2], ends[j][3])

    # 未接入任何网络的引脚打 no_connect（KiCad 规范的「故意悬空」标记）
    for it in items:
        ref = it["ref"]
        _, pins = pin_cache[ref]
        ix, iy = positions[ref]
        for n, (px, py, _a) in pins.items():
            if (ref, n) not in connected:
                ax, ay = ix + px, iy - py
                L.append("  (no_connect")
                L.append(f"    (at {_fmt(ax)} {_fmt(ay)})")
                L.append(f'    (uuid "{_u()}")')
                L.append("  )")

    L.append(")")
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(L), encoding="utf-8")
    _write_project_file(out)
    return out


def _write_project_file(sch_path: Path) -> Path:
    """在原理图旁生成同名 .kicad_pro，让 KiCad「打开已有工程」能直接识别。"""
    pro = sch_path.with_suffix(".kicad_pro")
    data = {
        "board": {"design_settings": {}},
        "boards": [],
        "cvpcb": {"equivalence_files": []},
        "libraries": {"pinned_footprint_libs": [], "pinned_symbol_libs": []},
        "meta": {"filename": pro.name, "version": 2},
        "net_settings": {"classes": [
            {
                "bus_width": 12, "clearance": 0.2, "diff_pair_gap": 0.25,
                "diff_pair_via_gap": 0.25, "diff_pair_width": 0.2, "line_style": 0,
                "microvia_diameter": 0.3, "microvia_drill": 0.1, "name": "Default",
                "pcb_color": "rgba(0, 0, 0, 0.000)", "schematic_color": "rgba(0, 0, 0, 0.000)",
                "track_width": 0.2, "via_diameter": 0.6, "via_drill": 0.3, "wire_width": 6,
            }
        ]},
        "pcbnew": {"last_paths": {
            "gencad": "", "idf": "", "netlist": "", "plot": "", "pos_files": "",
            "specctra_dsn": "", "step": "", "svg": "", "vrml": "",
        }, "page_layout_descr_file": ""},
        "schematic": {"legacy_lib_dir": "", "legacy_lib_list": []},
        "sheets": [["Root", sch_path.with_suffix("").name]],
        "text_variables": {},
    }
    pro.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return pro
