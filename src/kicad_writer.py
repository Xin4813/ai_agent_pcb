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
import heapq
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

# KiCad 图纸右下角标题栏（Title Block）禁区：元件和走线都不得进入。
# A4 标题栏约从 x=187 到 297（宽 110）、y=175 到 210（高 35），留缓冲取
# 宽 122 / 高 42，即从 (pw-122, ph-42) 到图纸右下角。A3 标题栏同尺寸级别。
TITLE_W, TITLE_H = 122.0, 42.0


def _in_title_block(x: float, y: float, hw: float = 0.0, hh: float = 0.0,
                    paper: str = "A4", whole_row: bool = False) -> bool:
    """点/矩形是否侵入标题栏禁区（hw/hh 为半宽半高）。

    whole_row=True 时把整个底部横带（y>页高-TITLE_H）都视为禁区，用于布局：
    因为元件的引出线会朝各个方向走，只按 x/y 矩形挡会让「位于标题栏左侧、
    但引脚已低于顶边」的元件（如 USB 座）出线时穿进标题栏。布局阶段整行留空
    最稳妥；走线阶段仍按真实矩形挡，尽量利用空间。
    """
    pw, ph = PAPERS[paper]
    tx0, ty0 = pw - TITLE_W, ph - TITLE_H
    if whole_row:
        return y + hh > ty0
    return (x + hw > tx0 and y + hh > ty0)


def _pin_abs(px: float, py: float, ang: float, ix: float, iy: float,
             mirrored: bool = False):
    """引脚绝对坐标 + 引出方向。

    库坐标 Y 向上、原理图 Y 向下：正常时 (ix+px, iy-py)。
    mirrored=True 表示元件左右镜像（KiCad `(mirror y)`，X 取反）：
    引脚 x 变号，方向角 angle 也左右翻转（0↔180，90/270 不变），
    这样排针的引脚可以朝向左侧的芯片。
    返回 (ax, ay, dx, dy)：dx/dy 为「从连接点指向符号体」的单位向量
    （引出线走反方向）。
    """
    if mirrored:
        ax, ay = ix - px, iy - py
        a = (180 - int(ang)) % 360
    else:
        ax, ay = ix + px, iy - py
        a = int(ang) % 360
    dx, dy = _DIRS.get(a, (1, 0))
    return ax, ay, dx, dy


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


def _to_grid(x: float, y: float) -> tuple[int, int]:
    """原理图坐标 → 1.27mm 栅格索引。"""
    return int(round(x / GRID)), int(round(y / GRID))


def _from_grid(i: int, j: int) -> tuple[float, float]:
    return i * GRID, j * GRID


def _mst_edges(points: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """按曼哈顿距离给端点集合生成最小生成树边（Prim）。"""
    n = len(points)
    if n < 2:
        return []
    used = {0}
    edges = []
    while len(used) < n:
        best = None
        for a in used:
            for b in range(n):
                if b in used:
                    continue
                d = abs(points[a][0] - points[b][0]) + abs(points[a][1] - points[b][1])
                if best is None or d < best[0]:
                    best = (d, a, b)
        if best is None:
            break
        edges.append((best[1], best[2]))
        used.add(best[2])
    return edges


# 穿越一条「别人已占的走线」的代价。设为软惩罚而非硬禁止：硬禁止会
# 把密集区域彻底封死，导致大量网络走不通、退化成断线+标签（实测标签
# 24→38）。软惩罚让路由器「能绕则绕、绕不开才穿」，兼顾少交叉与连通。
CROSS_COST = 40


def _route_path(start, goal, blocked, soft, hard, key, nx, ny, end_cells=()):
    """在栅格上找一条「少拐弯、少穿越」的正交路径。

    Dijkstra 代价 = 步长 + 转向惩罚 + 穿越其它网络既有走线的软惩罚。
    两类障碍：
      • 符号本体 (blocked) 与「别人的端点」(hard) 是硬障碍——绝对不可穿。
        端点若被穿越，两条不同网络的线会在同一点相连，触发 ERC
        multiple_net_names（踩过：ICSP_MISO/SCK 被竖线同时穿过）。
      • 别人的走线中段 (soft) 只是「贵」不是「禁」——能绕则绕、绕不开
        才穿（十字交叉在 KiCad 里不相连），兼顾少交叉与连通。
    soft/hard: {栅格点: {网络 key}}；end_cells 为本网端点，穿越不罚。
    返回栅格节点列表（含起终点），失败返回 None。
    """
    if start == goal:
        return [start]

    own_ends = set(end_cells)
    own_ends.add(start)
    own_ends.add(goal)

    def cross_penalty(node):
        if node in own_ends:
            return 0
        users = soft.get(node)
        if not users:
            return 0
        if users == {key}:            # 只有本网占用
            return 0
        return CROSS_COST

    dirs = ((1, 0), (-1, 0), (0, 1), (0, -1))
    init = (start[0], start[1], -1)
    dist = {init: 0}
    prev = {}
    heap = [(0, init)]
    while heap:
        cost, state = heapq.heappop(heap)
        if cost != dist.get(state):
            continue
        x, y, direction = state
        if (x, y) == goal:
            path = []
            cur = state
            while cur is not None:
                path.append((cur[0], cur[1]))
                cur = prev.get(cur)
            return path[::-1]
        for nd, (dx, dy) in enumerate(dirs):
            nxt = (x + dx, y + dy)
            if not (1 <= nxt[0] < nx - 1 and 1 <= nxt[1] < ny - 1):
                continue
            if nxt in blocked and nxt not in own_ends:   # 符号本体硬障碍
                continue
            hard_users = hard.get(nxt)
            if hard_users and key not in hard_users:     # 别人的端点硬障碍
                continue
            nstate = (nxt[0], nxt[1], nd)
            ncost = cost + 1 + (0 if direction in (-1, nd) else 4) \
                    + cross_penalty(nxt)
            if ncost < dist.get(nstate, 10 ** 9):
                dist[nstate] = ncost
                prev[nstate] = state
                heapq.heappush(heap, (ncost, nstate))
    return None


def _shorten_path(path, blocked, soft, hard, key):
    """在仍可通行的前提下合并绕路产生的多余拐角，减少线段数。

    与 _route_path 一致：别的网络的走线是软障碍——短缩时不主动穿过它们
    （避免引入新交叉），但符号本体仍是硬障碍。
    """
    if len(path) <= 2:
        return path

    def clear(a, b):
        if a[0] == b[0]:
            lo, hi = sorted((a[1], b[1]))
            cells = [(a[0], j) for j in range(lo, hi + 1)]
        elif a[1] == b[1]:
            lo, hi = sorted((a[0], b[0]))
            cells = [(i, a[1]) for i in range(lo, hi + 1)]
        else:
            return False
        for c in cells:
            if c in (a, b):
                continue
            if c in blocked:                      # 符号本体不可穿
                return False
            hard_users = hard.get(c)
            if hard_users and key not in hard_users:   # 别人的端点不可穿
                return False
            users = soft.get(c)
            if users and users != {key}:          # 别人的线：短缩不越过
                return False
        return True

    out = [path[0]]
    i = 0
    while i < len(path) - 1:
        best = i + 1
        for j in range(len(path) - 1, i + 1, -1):
            if clear(path[i], path[j]):
                best = j
                break
        out.append(path[best])
        i = best
    return out


def _compress_segments(path):
    """把栅格路径压成尽量少的水平/垂直线段（坐标为 mm）。"""
    pts = [_from_grid(i, j) for i, j in path]
    if len(pts) < 2:
        return []
    segs = []
    begin = pts[0]
    direction = None
    for a, b in zip(pts, pts[1:]):
        d = (b[0] - a[0], b[1] - a[1])
        if direction is None:
            begin, direction = a, d
        elif d != direction:
            segs.append((begin, a))
            begin, direction = a, d
    segs.append((begin, pts[-1]))
    return segs


def _layout(items, lib, netlist=None, alias=None) -> tuple[dict, str]:
    """按连接关系布局，返回 ({ref: (x, y)}, 纸张规格)。

    旧版只按元件高度逐列装箱，结果同一网络的元件被隔得很远，走线像迷宫。
    现在以引脚最多的芯片为锚点，按「与小网络共享端点」的亲密度依次把相关
    元件放到邻居周围，从源头缩短走线；A4 放不下再升 A3。
    """
    cells = {}
    for it in items:
        try:
            pins = lib.get_pins(it["symbol"])
        except Exception:
            pins = {}
        if pins:
            mx = max(abs(p[0]) for p in pins.values())
            my = max(abs(p[1]) for p in pins.values())
        else:
            mx = my = 5.0
        cells[it["ref"]] = (mx + MARGIN, my + MARGIN)

    # 小网络优先：两个元件只在 R1/C1/SW1 这种 3 节点网里相连，比在 VCC/GND
    # 这种十几节点网里相连更能说明它们应该挨在一起。
    nets = []
    if netlist:
        for net in netlist.get("nets", []):
            members = set()
            for pinref in net.get("pins", []):
                ref = str(pinref).partition(":")[0]
                ref = alias.get(ref, ref) if alias else ref
                if ref in cells:
                    members.add(ref)
            if len(members) >= 2:
                nets.append(members)

    def affinity(a, b) -> float:
        return sum(1.0 / max(1, len(m) - 1) for m in nets if a in m and b in m)

    for paper in ("A4", "A3"):
        pw, ph = PAPERS[paper]
        positions, boxes = {}, {}
        anchor = max(cells, key=lambda r: (cells[r][0] * cells[r][1], r))
        remaining = set(cells) - {anchor}
        ahw, ahh = cells[anchor]
        positions[anchor] = (_snap(PAGE_MX + ahw + 12),
                             _snap(PAGE_MY + ahh + 10))
        boxes[anchor] = (*positions[anchor], ahw, ahh)

        placed_ok = True
        while remaining:
            # 先放主芯片的直接外围，再放二级外围；否则 USB 这类二级模块会
            # 先占满主芯片右侧，把晶振/复位等本应贴近 MCU 的元件挤到远处。
            best = max(sorted(remaining),
                       key=lambda r: (affinity(r, anchor),
                                      sum(affinity(r, p) for p in boxes), r))
            hw, hh = cells[best]
            neighbors = sorted(boxes, key=lambda p: (-affinity(best, p), p))

            def overlap(cx, cy):
                return any(abs(cx - ox) < hw + ohw + 3
                           and abs(cy - oy) < hh + ohh + 3
                           for ox, oy, ohw, ohh in boxes.values())

            def cost(cx, cy):
                return sum(affinity(best, p) *
                           (abs(cx - boxes[p][0]) + abs(cy - boxes[p][1]))
                           for p in neighbors[:6])

            candidates = []
            for nb in neighbors[:6]:
                bx, by, bhw, bhh = boxes[nb]
                dx, dy = bhw + hw + 6, bhh + hh + 6
                for ox, oy in ((dx, 0), (0, dy), (-dx, 0), (0, -dy),
                               (dx, dy), (-dx, dy), (dx, -dy), (-dx, -dy)):
                    if affinity(best, nb) <= 0 and (ox, oy) not in ((dx, 0), (0, dy)):
                        continue
                    cx, cy = _snap(bx + ox), _snap(by + oy)
                    if (cx - hw < PAGE_MX or cx + hw > pw - PAGE_MX
                            or cy - hh < PAGE_MY or cy + hh > ph - PAGE_MY):
                        continue
                    if _in_title_block(cx, cy, hw, hh, paper, whole_row=True):
                        continue
                    if overlap(cx, cy):
                        continue
                    candidates.append((cost(cx, cy), cx, cy))

            if not candidates:
                # 兜底：粗网格扫描第一个不重叠的位置
                step = GRID * 4
                cx0 = _snap(PAGE_MX + hw)
                cy0 = _snap(PAGE_MY + hh)
                for cy in range(int(cy0), int(ph - PAGE_MY - hh) + 1, int(step)):
                    for cx in range(int(cx0), int(pw - PAGE_MX - hw) + 1, int(step)):
                        if _in_title_block(cx, cy, hw, hh, paper, whole_row=True):
                            continue
                        if not overlap(cx, cy):
                            candidates.append((cost(cx, cy), cx, cy))
                            break
                    if candidates:
                        break
            if not candidates:
                placed_ok = False
                break
            _, cx, cy = min(candidates)
            positions[best] = (cx, cy)
            boxes[best] = (cx, cy, hw, hh)
            remaining.remove(best)

        if placed_ok:
            return positions, paper
    # 极端情况：A3 仍放不下，返回最后一次结果（至少不会崩）
    return positions, "A3"


def _align_connector_buses(items, lib, netlist, positions, alias, paper):
    """把「芯片 ↔ 排针」的并行总线摆成镜像对齐的整齐线束。

    典型场景：ATmega 的 D0-D7 连到 8 针排针、A0-A5 连到 6 针排针。
    默认布局会把排针横放在芯片右侧，引脚引出方向与芯片相同（都朝右），
    于是十几根线必须绕到排针背后再折回，交叉成团。

    本函数识别这种「排针第 n 脚 ↔ 芯片某脚」一一对应的信号组，把排针
    左右镜像（引脚改朝左、正对芯片），竖直放到芯片右侧，并让第 n 脚的
    y 对齐其芯片脚的 y —— 于是每条总线变成一组水平平行线，零交叉。

    仅当排针与芯片之间存在足够多（≥3）的一一对应信号脚时才对齐；
    且目标位置不得与其它元件/标题栏冲突。任何不确定都保持原布局。

    返回 (新 positions, 需要镜像的位号集合)。
    """
    by_ref = {it["ref"]: it for it in items}
    pins_of = {}
    for it in items:
        try:
            pins_of[it["ref"]] = lib.get_pins(it["symbol"])
        except Exception:
            pins_of[it["ref"]] = {}

    # 主芯片：引脚最多的元件（ATmega/STM32 等）
    anchor = max((r for r in pins_of if pins_of[r]),
                 key=lambda r: (len(pins_of[r]), r), default=None)
    if not anchor:
        return positions, set()
    ax, ay = positions[anchor]

    def resolve(ref):
        return alias.get(ref, ref) if alias else ref

    # 收集每个排针到主芯片的一一对应信号脚：{排针脚号: 芯片脚号}
    nets = netlist.get("nets", [])
    bus = {}
    for it in items:
        ref = it["ref"]
        lid = it.get("symbol", "")
        if not re.search(r"Connector:Conn_01x\d+_Pin", lid):
            continue
        pairs = {}
        for net in nets:
            chip_pins, conn_pins = [], []
            for pr in net.get("pins", []):
                r, _, num = pr.partition(":")
                if not num.isdigit():
                    continue
                rr = resolve(r)
                if rr == anchor:
                    chip_pins.append(int(num))
                elif rr == ref:
                    conn_pins.append(int(num))
            # 一个排针脚只连一个芯片脚（且网络里没有其它排针脚）才算干净总线
            if len(conn_pins) == 1 and len(chip_pins) == 1:
                pairs[conn_pins[0]] = chip_pins[0]
        if len(pairs) >= 3:
            bus[ref] = pairs

    if not bus:
        return positions, set()

    # 芯片右侧边界（引脚最大 x），排针放到其右侧一列。
    # 需要有足够间距让两侧引出线（各 STUB=7.62）错开：若太近，芯片脚与
    # 排针脚的引出端点会落到同一栅格列、把相邻的不同网络短接（ERC
    # multiple_net_names）。实测需 ≥ 芯片引脚右缘 + 25mm。
    right_edge = ax + max((p[0] for p in pins_of[anchor].values()), default=0)
    bus_x = _snap(right_edge + 25.4)

    # 每个排针：镜像后引脚相对 y 不变（_pin_abs 镜像只翻 x），
    # 目标中心 y 取「芯片脚绝对 y - 引脚局部 y」的中位数，使各脚尽量水平对齐。
    plans = []
    for ref, pairs in bus.items():
        cpins = pins_of[ref]
        targets = []
        for conn_pin, chip_pin in pairs.items():
            if conn_pin not in cpins or chip_pin not in pins_of[anchor]:
                continue
            py = cpins[conn_pin][1]
            _, chip_ay, _, _ = _pin_abs(*pins_of[anchor][chip_pin], ax, ay, False)
            # 镜像只翻 x；排针脚绝对 y = 中心 y - 引脚库 y。
            # 要让它等于芯片脚 y，则 中心 y = 芯片脚 y + 引脚库 y。
            targets.append(chip_ay + py)     # 期望的排针中心 y
        if not targets:
            continue
        targets.sort()
        cy = _snap(targets[len(targets) // 2])
        ys = [cy - cpins[p][1] for p in pairs if p in cpins]
        plans.append({"ref": ref, "y": cy, "y0": min(ys), "y1": max(ys),
                      "pairs": pairs})

    # 按中心 y 排列，若 y 区间重叠则右移一列（避免排针互相压住）
    plans.sort(key=lambda p: p["y"])
    occupied = []   # (x0,x1,y0,y1) 已规划的排针占位（含间距）
    new_pos = dict(positions)
    mirrored = set()
    col_x = bus_x
    def body_half(ref):
        """元件本体半宽/半高（引脚实际范围 + 小缓冲，不含文字 MARGIN）。"""
        ps = pins_of[ref].values()
        return (max((abs(p[0]) for p in ps), default=5) + 2.0,
                max((abs(p[1]) for p in ps), default=5) + 2.0)

    for plan in plans:
        ref = plan["ref"]
        cpins = pins_of[ref]
        hw, hh = body_half(ref)
        y0, y1 = plan["y0"] - 2, plan["y1"] + 2
        placed = False
        for try_x in (col_x, col_x + 22.86, col_x + 45.72, col_x + 68.58):
            tx0, tx1 = try_x - hw - 1, try_x + hw + 1
            # 与其它已摆排针冲突 → 试下一列
            if any(not (tx1 < o[0] or tx0 > o[1] or y1 < o[2] or y0 > o[3])
                   for o in occupied):
                continue
            # 与未参与总线的元件冲突 → 放弃该排针对齐（保守）
            clash = False
            for it in items:
                r = it["ref"]
                if r == ref or r in bus:
                    continue
                ox, oy = positions[r]
                ohw, ohh = body_half(r)
                if not (tx1 < ox - ohw or tx0 > ox + ohw
                        or y1 < oy - ohh or y0 > oy + ohh):
                    clash = True
                    break
            if clash:
                continue
            pw, ph = PAPERS[paper]
            if try_x + hw > pw - PAGE_MX or _in_title_block(try_x, plan["y"], hw, hh, paper):
                continue
            new_pos[ref] = (_snap(try_x), _snap(plan["y"]))
            mirrored.add(ref)
            occupied.append((tx0, tx1, y0, y1))
            placed = True
            break
        if placed:
            col_x = max(col_x, try_x)   # 后续排针优先同列，重叠才右移
    return new_pos, mirrored


def write_schematic(netlist: dict, bom: dict, output_path, symbols_dir,
                    layout_override: dict | None = None,
                    mirror_override: set | None = None,
                    auto_bus: bool = True) -> Path:
    """生成原理图。

    layout_override / mirror_override 供测试使用；auto_bus 控制是否自动把
    芯片↔排针的并行总线摆成镜像对齐的整齐线束（默认开启）。
    """
    lib = SymbolLib(symbols_dir)
    items, alias = _expand_refs(bom["items"])
    positions, paper = _layout(items, lib, netlist, alias)
    auto_mirror = set()
    if auto_bus and not layout_override and not mirror_override:
        positions, auto_mirror = _align_connector_buses(
            items, lib, netlist, positions, alias, paper)
    if layout_override:
        positions.update(layout_override)
    mirrored = set(auto_mirror) | set(mirror_override or ())
    pin_cache = {}
    etype_cache = {}   # ref -> {引脚号: 电气类型(power_in/power_out/...)}
    for it in items:
        try:
            pins = lib.get_pins(it["symbol"])
        except Exception:
            pins = {}
        pin_cache[it["ref"]] = (it["symbol"], pins)
        try:
            etype_cache[it["ref"]] = lib.pin_electrical_types(it["symbol"])
        except Exception:
            etype_cache[it["ref"]] = {}

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

    def _net_needs_flag(net) -> bool:
        """该网络是否需要补 PWR_FLAG（电源输出旗标）。

        判定依据引脚的**电气类型**而非网络名——这是修复 ERC 两条顽固违规的关键：
          • 只有当网络含 power_in 引脚、且不含任何 power_out 引脚时才需要旗标。
          • 若网络里已有 power_out（如稳压器 VO、USB 电源输出），再补旗标会触发
            ERC `[pin_to_pin] 电源输出与电源输出相连`（曾发生在 VCC 网上）。
          • 若网络含 power_in 但名字不像电源（如稳压器 VI 所在的 NET25），
            过去按网络名判定会漏补，触发 `[power_pin_not_driven]`。

        没有电气类型信息时（符号库缺失）退回按网络名判定，保持旧行为。
        """
        valid = _net_valid_pins(net)
        if len(valid) < 2:
            return False
        kinds = set()
        for r, key in valid:
            kinds.add(etype_cache.get(r, {}).get(key, ""))
        if not any(k for k in kinds if k):        # 无任何电气类型信息 → 退回网络名规则
            return bool(_POWER_RE.match(net.get("name", "")))
        return "power_in" in kinds and "power_out" not in kinds

    # 需要补 PWR_FLAG 的电源网络（每网 1 个），按电气类型判定
    power_nets = [n for n in netlist["nets"] if _net_needs_flag(n)]

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

    def _emit_symbol(ref, sym, x, y, pins, value, in_bom="yes", hide_text=False,
                     mirror_y=False):
        """输出一个符号实例（带库前缀 lib_id，内嵌定义同名）。

        hide_text=True 时隐藏 Reference/Value 文字（用于 PWR_FLAG 这类
        内部标记——KiCad 规范里它们本就不显示，否则 "PWR_FLAG" 长文字
        会压住旁边元件的网络标签与 Reference/Value）。
        mirror_y=True 输出 `(mirror y)`，让元件左右翻转、引脚朝向相反侧，
        用于把排针的引脚转向芯片以形成整齐的平行总线。
        """
        hw = hh = MARGIN
        if pins:
            hw = max(abs(p[0]) for p in pins.values()) + MARGIN
            hh = max(abs(p[1]) for p in pins.values()) + MARGIN
        hide = " (hide yes)" if hide_text else ""
        mirror = "\n    (mirror y)" if mirror_y else ""
        L.append("  (symbol")
        L.append(f'    (lib_id "{sym}")')
        L.append(f"    (at {_fmt(x)} {_fmt(y)} 0){mirror}")
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
        _emit_symbol(ref, sym, x, y, pins, it.get("value", ""),
                     mirror_y=(ref in mirrored))

    def _wire(x1, y1, x2, y2):
        L.append("  (wire")
        L.append("    (pts")
        L.append(f"      (xy {_fmt(x1)} {_fmt(y1)}) (xy {_fmt(x2)} {_fmt(y2)})")
        L.append("    )")
        L.append('    (stroke (width 0) (type solid))')
        L.append(f'    (uuid "{_u()}")')
        L.append("  )")

    # 引脚引出线 + 网络标签 + 同网导线互连 + PWR_FLAG + 未连引脚 no_connect。
    # 方向要点：pin 的 angle 指向符号体，引出线走外侧（反向）；Y 轴要翻转。
    # 过去只有「短线 + 同名标签」，电气上连通但视觉上没连起来；现在额外用
    # 1.27mm 栅格上的正交走线把同一网络的所有端点真正连成一张网。
    pw, ph = PAPERS[paper]
    emitted = set()
    net_jobs = []
    flag_no = 0
    for net in netlist["nets"]:
        ok = _net_valid_pins(net)
        if len(ok) < 2:                           # 单引脚网络 → 引脚打 no_connect
            continue
        name = net.get("name", "")

        # ① 收集该网络所有引出线端点（同坐标去重）
        ends = []
        for r, key in ok:
            sym, pins = pin_cache[r]
            px, py, ang = pins[key]
            ix, iy = positions[r]
            ax, ay, dx, dy = _pin_abs(px, py, ang, ix, iy, r in mirrored)
            ex, ey = ax - dx * STUB, ay + dy * STUB
            k = (round(ax, 3), round(ay, 3), name)
            if k in emitted:
                continue
            emitted.add(k)
            ends.append((ax, ay, ex, ey, -dx, dy))
        if len(ends) < 2:
            continue

        # ② PWR_FLAG 位置（仍按电气类型判定，带符号避让）
        flag = None
        if _net_needs_flag(net):
            ax, ay, ex, ey, odx, ody = ends[0]
            cands = [(0.0, -12.7), (0.0, 12.7),
                     (odx * 12.7, ody * 12.7), (-odx * 12.7, -ody * 12.7)]
            fx = fy = None
            for ox, oy in cands:
                tx, ty = _snap(ex + ox), _snap(ey + oy)
                if not _hits_symbol(tx, ty) and not _in_title_block(tx, ty, 3, 3, paper):
                    fx, fy = tx, ty
                    break
            if fx is None:
                fx, fy = _snap(ex + odx * 12.7), _snap(ey + ody * 12.7)
            flag_no += 1
            flag = (fx, fy, f"#FLG{flag_no:02d}")

        net_jobs.append({"name": name, "ends": ends, "flag": flag,
                         "touched": set(),
                         "refs": {r for r, _k in ok}})

    # ④ 障碍物：元件符号本体（引脚包围盒 + 2.54mm，比布局占位盒小，
    #    保证引出线端点仍在障碍外）。
    nx, ny = int(pw / GRID) + 1, int(ph / GRID) + 1
    blocked = set()
    # 标题栏禁区列为障碍：任何网络的走线都不得穿进右下角标题栏。
    tgi0, tgj0 = _to_grid(pw - TITLE_W, ph - TITLE_H)
    for gi in range(max(0, tgi0), nx):
        for gj in range(max(0, tgj0), ny):
            blocked.add((gi, gj))
    for it in items:
        ref = it["ref"]
        _, pins = pin_cache[ref]
        if not pins:
            continue
        ix, iy = positions[ref]
        mir = ref in mirrored
        xs = [(-p[0] if mir else p[0]) for p in pins.values()]
        ys = [p[1] for p in pins.values()]
        pad = 2.54
        x0, x1 = ix + min(xs) - pad, ix + max(xs) + pad
        y0, y1 = iy - max(ys) - pad, iy - min(ys) + pad
        i0, j0 = _to_grid(x0, y0)
        i1, j1 = _to_grid(x1, y1)
        for gi in range(max(0, i0), min(nx, i1 + 1)):
            for gj in range(max(0, j0), min(ny, j1 + 1)):
                blocked.add((gi, gj))

    # PWR_FLAG 的菱形本体画在引脚点上方约 2 格（库轮廓 (0,0)→(0,2.54)），
    # 但引脚点才是电气端点。若只保护引脚点，其它网络的走线会从菱形符号底下
    # 穿过，视觉上像「叠线」（电气并未相连）。把菱形占据的上方格也列为障碍，
    # 让走线绕开；但排除所有真实端点格（端点必须可达）。
    endpoint_cells = set()
    for _job in net_jobs:
        for _e in _job["ends"]:
            endpoint_cells.add(_to_grid(_e[0], _e[1]))
            endpoint_cells.add(_to_grid(_e[2], _e[3]))
        if _job["flag"]:
            endpoint_cells.add(_to_grid(_job["flag"][0], _job["flag"][1]))
    for _job in net_jobs:
        if not _job["flag"]:
            continue
        _gi, _gj = _to_grid(_job["flag"][0], _job["flag"][1])
        for _dj in (1, 2):
            _c = (_gi, _gj - _dj)
            if _c not in endpoint_cells:
                blocked.add(_c)

    # ④ 预先登记各网络的引出线与端点。
    #    关键：引脚引出线（stub）整段都要登记！过去只登记端点 (ex,ey)，
    #    导致其它网络的走线可以横穿某引脚的整段 stub——这正是截图里芯片脚
    #    附近那一小簇“不必要交叉”的来源。
    # soft: {栅格点: {网络 key}} —— 已被哪些网络的走线占用（软障碍）。
    # 与旧的硬 owner 不同：完整线段的每一格都登记，穿越要付 CROSS_COST，
    # 但不是硬墙——密集区不会因为被封死而断线（旧硬墙方案标签 24→38）。
    soft = {}
    hard = {}   # 各网络的真实端点（含 stub 两端、PWR_FLAG）：别的网络不可穿越

    def _occupy(key, a, b):
        """把栅格 a→b 的水平/垂直线段登记到 soft。"""
        if a[0] == b[0]:
            step = 1 if b[1] >= a[1] else -1
            cells = [(a[0], j) for j in range(a[1], b[1] + step, step)]
        else:
            step = 1 if b[0] >= a[0] else -1
            cells = [(i, a[1]) for i in range(a[0], b[0] + step, step)]
        for c in cells:
            soft.setdefault(c, set()).add(key)

    # 引脚引出线（stub）全程登记：别的网络横穿它要付代价（少交叉），
    # 但真绕不开时仍能穿（保连通），自家网络穿越不罚。
    for key, job in enumerate(net_jobs):
        for e in job["ends"]:
            ga, gb = _to_grid(e[0], e[1]), _to_grid(e[2], e[3])
            _occupy(key, ga, gb)
            # 引脚端与 stub 末端都是电气连接点，设为硬障碍
            for c in (ga, gb):
                hard.setdefault(c, set()).add(key)
        if job["flag"]:
            fc = _to_grid(job["flag"][0], job["flag"][1])
            soft.setdefault(fc, set()).add(key)
            hard.setdefault(fc, set()).add(key)

    def _commit(key, path):
        path = _shorten_path(path, blocked, soft, hard, key)
        for (x1, y1), (x2, y2) in _compress_segments(path):
            if (x1, y1) != (x2, y2):
                _occupy(key, _to_grid(x1, y1), _to_grid(x2, y2))
                _wire(x1, y1, x2, y2)

    def _mst_route(key, job, pts):
        end_cells = set(pts)
        if job["flag"]:
            end_cells.add(_to_grid(job["flag"][0], job["flag"][1]))
        for a, b in _mst_edges(pts):
            path = _route_path(pts[a], pts[b], blocked, soft, hard, key, nx, ny, end_cells)
            if not path:
                continue                       # 走不通时仍保留网络标签兜底
            job["touched"].update((a, b))
            _commit(key, path)

    # 布线顺序：端点多的网络先走、同等规模下总长度短的优先。
    # 实测大网（VCC/GND）先占好主干，小网绕行，比小网先走少几处交叉。
    def _order(kv):
        job = kv[1]
        pts = [_to_grid(e[2], e[3]) for e in job["ends"]]
        total = sum(abs(pts[a][0] - pts[b][0]) + abs(pts[a][1] - pts[b][1])
                    for a, b in _mst_edges(pts))
        return (-len(pts), total, kv[0])

    for key, job in sorted(enumerate(net_jobs), key=_order):
        pts = [_to_grid(e[2], e[3]) for e in job["ends"]]
        if job["flag"]:
            pts.append(_to_grid(job["flag"][0], job["flag"][1]))
        _mst_route(key, job, pts)

    # ⑥ 输出引出线、标签和 PWR_FLAG。
    # 已用导线连通的网络只挂一个标签；未接通的端点保留标签兜底。
    junc_pts = set()   # 引脚电气连接点：加实心圆点，明确「线接在这里」
    for job in net_jobs:
        name = job["name"]
        for ax, ay, ex, ey, odx, ody in job["ends"]:
            _wire(ax, ay, ex, ey)
            junc_pts.add((round(ax, 3), round(ay, 3)))
        touched = sorted(job["touched"])
        primary = touched[0] if touched else 0
        for i, (ax, ay, ex, ey, odx, ody) in enumerate(job["ends"]):
            if i != primary and i in job["touched"]:
                continue
            langle = 180 if odx > 0 else 0
            L.append(f'  (label "{name}"')
            L.append(f"    (at {_fmt(ex)} {_fmt(ey)} {langle})")
            L.append('    (effects (font (size 1.27 1.27)))')
            L.append(f'    (uuid "{_u()}")')
            L.append("  )")
        if job["flag"]:
            fx, fy, fref = job["flag"]
            _emit_symbol(fref, "power:PWR_FLAG", fx, fy,
                         {1: (0.0, 0.0, 90.0)}, "PWR_FLAG", in_bom="no",
                         hide_text=True)

    # 引脚电气连接点画实心圆点（junction）：纯视觉标记，让「导线确实接到了
    # 针脚」一眼可见，消除 stub 末端那种「悬空线头」的观感。不改电气连接，
    # ERC 不检查 junction 是否必要，因此不会影响违规数。多脚同坐标会去重。
    for jx, jy in sorted(junc_pts):
        L.append("  (junction")
        L.append(f"    (at {_fmt(jx)} {_fmt(jy)})")
        L.append("    (diameter 0)")
        L.append('    (color 0 0 0 0)')
        L.append(f'    (uuid "{_u()}")')
        L.append("  )")

    # 未接入任何网络的引脚打 no_connect（KiCad 规范的「故意悬空」标记）。
    # 两个必须避开的坑（ESP32 这类多 GND 脚芯片上踩过）：
    #  ① 电气类型本身就是 no_connect 的引脚（如 32 脚 NC）：库已声明，再打标记反
    #     而触发 no_connect_dangling / no_connect_connected。
    #  ② 多个引脚共用同一坐标（ESP32 的 1/15/38/39 脚都在 (0,-35.56)）：只要其中
    #     任意一个接入了网络，整个坐标都不能打 no_connect，否则同点既接线又打标，
    #     ERC 报 no_connect_connected；反之未接坐标也只打一次，不重复叠加。
    connected_xy = set()
    for it in items:
        ref = it["ref"]
        _, pins = pin_cache[ref]
        ix, iy = positions[ref]
        for n, (px, py, a) in pins.items():
            if (ref, n) in connected:
                ax, ay, _dx, _dy = _pin_abs(px, py, a, ix, iy, ref in mirrored)
                connected_xy.add((round(ax, 3), round(ay, 3)))
    nc_xy = set()
    for it in items:
        ref = it["ref"]
        _, pins = pin_cache[ref]
        ix, iy = positions[ref]
        for n, (px, py, a) in pins.items():
            if (ref, n) in connected:
                continue
            if etype_cache.get(ref, {}).get(n) == "no_connect":   # 坑①
                continue
            ax, ay, _dx, _dy = _pin_abs(px, py, a, ix, iy, ref in mirrored)
            key = (round(ax, 3), round(ay, 3))
            if key in connected_xy or key in nc_xy:              # 坑②
                continue
            nc_xy.add(key)
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
