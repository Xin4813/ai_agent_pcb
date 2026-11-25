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
# 长距离点对点信号网改用「两端同名全局标签」连接的跨度阈值（mm）。
#  rationale：跨越大半个芯片/排针的长导线既容易绕不开、退化成两端小标签的
# 「视觉断线」，又让图面拥挤。主动统一改用带方框的全局标签（一眼可辨是同名
# 端口连接），图面清爽且风格一致。仅限 2 端点、非电源的网络；多端点网仍走实线。
LABEL_SPAN_MM = 35.0
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


def _text_cells(cell: tuple[int, int], text: str, angle: int = 0) -> set:
    """文字在 1.27mm 栅格上的近似占位（用于通用文字避让，不改电气）。

    SVG 实证 KiCad 8 的 label 恒为 text-anchor=middle：0/180 都是横排
    （仅小箭头朝向不同），90/270 才是竖排。旧版按「180 向右、0 向左」
    预留避让区，与真实文字错位半个词宽，是标签互压/被穿的根源。
    """
    gi, gj = cell
    half = max(1, len(str(text))) // 2
    if angle in (90, 270):
        return {(gi, gj + k) for k in range(-half, half + 1)}
    return {(gi + k, gj) for k in range(-half, half + 1)}

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
# 40 会让 VCC 之类网络为了绕开 PWR_FLAG 引线走出很大的 U 形（如 LM1117
# 里 VCC 绕到 x=25.4 再折回）；降到 25 后走线走直线、交叉仍为 0，且
# Arduino 大板的走线/标签/交叉统计完全不变（实测）。
CROSS_COST = 25


def _edge_key(a, b):
    """无向栅格边的稳定标识（用于阻止异网复用同一段导线）。"""
    return tuple(sorted((a, b)))


def _route_path(start, goal, blocked, soft, hard, key, nx, ny, end_cells=(),
                edges=None, vertices=None):
    """在栅格上找一条「少拐弯、少穿越」的正交路径。

    Dijkstra 代价 = 步长 + 转向惩罚 + 穿越其它网络既有走线的软惩罚。
    三类硬障碍：
      • 符号本体 (blocked)；
      • 别人的电气端点/stub (hard)；
      • 别人的**已画导线边** (edges) 和**拐点/端点** (vertices)。
        前两类防止穿引脚，后两类防止两条异网导线复用同一段或在同一点
        收口——KiCad 会把它们当成同一条线，触发 multiple_net_names。
      • 别人的走线中段 (soft) 只是「贵」不是「禁」——能绕则绕、绕不开
        才穿（十字交叉在 KiCad 里不相连），兼顾少交叉与连通。
    soft/hard/vertices: {栅格点: {网络 key}}；edges: {无向边: {网络 key}}。
    end_cells 为本网端点，穿越不罚。返回栅格节点列表或 None。
    """
    edges = edges or {}
    vertices = vertices or {}
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
            # 异网不能复用同一条边，也不能在对方拐点/端点落脚：
            # 过去 _wire 去重把异网同段静默丢掉，造成一条网络被另一条
            # 网络「搭桥」，是 multiple_net_names 的隐藏根因。
            edge_users = edges.get(_edge_key((x, y), nxt))
            if edge_users and key not in edge_users:
                continue
            vertex_users = vertices.get(nxt)
            if vertex_users and key not in vertex_users:
                continue
            nstate = (nxt[0], nxt[1], nd)
            ncost = cost + 1 + (0 if direction in (-1, nd) else 4) \
                    + cross_penalty(nxt)
            if ncost < dist.get(nstate, 10 ** 9):
                dist[nstate] = ncost
                prev[nstate] = state
                heapq.heappush(heap, (ncost, nstate))
    return None


def _shorten_path(path, blocked, soft, hard, key, edges=None, vertices=None):
    """在仍可通行的前提下合并绕路产生的多余拐角，减少线段数。

    与 _route_path 一致：别的网络的走线是软障碍——短缩时不主动穿过它们
    （避免引入新交叉），但符号本体仍是硬障碍。edges/vertices 进一步
    阻止异网复用同一段或同一拐点。
    """
    if len(path) <= 2:
        return path
    edges = edges or {}
    vertices = vertices or {}

    def clear(a, b):
        if a[0] == b[0]:
            lo, hi = sorted((a[1], b[1]))
            cells = [(a[0], j) for j in range(lo, hi + 1)]
        elif a[1] == b[1]:
            lo, hi = sorted((a[0], b[0]))
            cells = [(i, a[1]) for i in range(lo, hi + 1)]
        else:
            return False
        for u, v in zip(cells, cells[1:]):
            edge_users = edges.get(_edge_key(u, v))
            if edge_users and key not in edge_users:
                return False
        for c in cells:
            if c in (a, b):
                continue
            if c in blocked:                      # 符号本体不可穿
                return False
            hard_users = hard.get(c)
            if hard_users and key not in hard_users:   # 别人的端点不可穿
                return False
            vertex_users = vertices.get(c)
            if vertex_users and key not in vertex_users:
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
    return _straighten_steps(out, blocked, soft, hard, key, edges, vertices)


def _straighten_steps(path, blocked, soft, hard, key, edges=None, vertices=None):
    """消除「一格回折台阶」：路径末段 A→B→C→D 中，若把中间的下探/
    上探折回改走直角能少一个拐点、且新段通道干净，就重写成 A→A'→D。

    典型场景：走到引脚 stub 末端时先下探一格再左折回来，视觉上像一个小
    台阶（如 LM1117 里 C3 下脚 GND 线）。_shorten_path 只能合并共线段，
    无法重建这种 L 形，这里补一步。只处理 4 点的回折，且要求新路径的
    两段都是正交、通道 clear，避免引入新交叉或穿过别的网络。
    """
    if len(path) < 4:
        return path
    edges = edges or {}
    vertices = vertices or {}

    def clear2(a, b):
        if a[0] == b[0]:
            lo, hi = sorted((a[1], b[1]))
            cells = [(a[0], j) for j in range(lo, hi + 1)]
        elif a[1] == b[1]:
            lo, hi = sorted((a[0], b[0]))
            cells = [(i, a[1]) for i in range(lo, hi + 1)]
        else:
            return False
        for u, v in zip(cells, cells[1:]):
            edge_users = edges.get(_edge_key(u, v))
            if edge_users and key not in edge_users:
                return False
        for c in cells:
            if c in (a, b):
                continue
            if c in blocked:
                return False
            hu = hard.get(c)
            if hu and key not in hu:
                return False
            vu = vertices.get(c)
            if vu and key not in vu:
                return False
            u = soft.get(c)
            if u and u != {key}:
                return False
        return True

    def bends(p):
        n = 0
        for i in range(1, len(p) - 1):
            a, b, c = p[i - 1], p[i], p[i + 1]
            if (a[0] == b[0]) != (b[0] == c[0]):
                n += 1
        return n

    out = list(path)
    # 反复扫描，直到没有可消除的台阶
    for _ in range(len(out)):
        changed = False
        for i in range(len(out) - 3):
            a, b, c, d = out[i], out[i + 1], out[i + 2], out[i + 3]
            # 只处理 AB⊥BC 且 BC⊥CD（两次直角，形成台阶/Z 形）
            ab_v = a[0] == b[0]
            bc_v = b[0] == c[0]
            cd_v = c[0] == d[0]
            if not (ab_v != bc_v and bc_v != cd_v):
                continue
            # 候选：把 b,c 换成一个角点 e，使 a→e→d 为正交两段
            for e in ((d[0], a[1]), (a[0], d[1])):
                if e == a or e == d or e == b or e == c:
                    continue
                if not (clear2(a, e) and clear2(e, d)):
                    continue
                cand = out[:i] + [a, e, d] + out[i + 4:]
                # 必须减少拐点数，或拐点相等但线段更短
                old_len = (abs(a[0]-b[0])+abs(a[1]-b[1]) + abs(b[0]-c[0])+abs(b[1]-c[1])
                           + abs(c[0]-d[0])+abs(c[1]-d[1]))
                new_len = abs(a[0]-e[0])+abs(a[1]-e[1]) + abs(e[0]-d[0])+abs(e[1]-d[1])
                if bends(cand) < bends(out) or (bends(cand) == bends(out) and new_len < old_len):
                    out = cand
                    changed = True
                    break
            if changed:
                break
        if not changed:
            break
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
                # 兜底：粗网格扫描所有不重叠位置，按 cost 取最靠近亲密邻居的一处。
                # 旧实现取「页顶第一个空格」，会把被挤到兜底的元件（如新增晶振 Y1）
                # 丢到页面顶部 (y≈31)，离它的网络标签/配对芯片很远，造成断线和线头。
                # 改为全网格评估、按加权距离选最优，让兜底元件仍贴近相关网络。
                # 必须按**栅格索引**扫描：旧写法 `range(int(坐标), ..., int(2.54))`
                # 实际是以 2 毫米为步长，会把兜底元件放到 117/151 这类
                # 非 1.27 栅格坐标，符号引脚随之离网，ERC 报 endpoint_off_grid /
                # wire_dangling。用 _to_grid→_from_grid 保证兜底位置也严格落网。
                i0, j0 = _to_grid(PAGE_MX + hw, PAGE_MY + hh)
                i1, j1 = _to_grid(pw - PAGE_MX - hw, ph - PAGE_MY - hh)
                fallback = []
                for j in range(j0, j1 + 1, 2):
                    _gx, cy = _from_grid(0, j)
                    for i in range(i0, i1 + 1, 2):
                        cx, _gy = _from_grid(i, 0)
                        if _in_title_block(cx, cy, hw, hh, paper, whole_row=True):
                            continue
                        if not overlap(cx, cy):
                            fallback.append((cost(cx, cy), cx, cy))
                candidates.extend(fallback)
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

    # 元件 Reference/Value 文字的位置。过去各元件独立按自身 hh 推离中心，
    # 两个元件竖排靠近时，上元件的 Value 与下元件的 Reference 会落进同一空隙
    # 贴成一团（实测 R1.Value y=135.19 与 SW2.Reference y=136.59 仅差 1.4mm）。
    # 这里先按原始期望位置收集，再对「同列」文字做纵向避让，统一供占位与输出
    # 使用——生成器与 visual_check 用同一套 MIN_TEXT_PITCH，避免自检漏判。
    prop_y = {}          # (ref, kind) -> 避让后的绝对 y
    prop_texts = []      # {ref, kind, x, y, cy, text}
    for it in items:
        ref = it["ref"]
        x, y = positions[ref]
        _, pins = pin_cache[ref]
        hh = max((abs(p[1]) for p in pins.values()), default=5.0) + MARGIN
        for kind, text, ty in (("Reference", ref, y - hh - 2.54),
                               ("Value", it.get("value", ""), y + hh + 2.54)):
            if str(text).strip():
                prop_texts.append({"ref": ref, "kind": kind, "x": x,
                                   "y": ty, "cy": y, "text": str(text)})

    # 按「横向 bbox 是否相交」分组（并查集），而非只看完全相同的 x 网格。
    # 原因：宽 Value（如 "LM317"、"LM1117T-5.0"）半宽可达 3~7mm，会横向跨列
    # 压到相邻元件的 Reference/Value 上；仅按同列避让会漏掉这种跨列互压
    # （visual_check 用 bbox 判重叠，因此自检仍报错）。凡横向区间相交的文字
    # 并入同组，组内从下往上做「夹紧 + 拉开」：
    #   1) 任何文字不得越过标题栏上沿或页面下边距；
    #   2) 相邻两行间距 >= MIN_PROP_PITCH（2.5 格，> 自检的 2.54 报警线）。
    # 横向不相交的文字即使纵向接近也不会视觉冲突，无需约束。
    MIN_PROP_PITCH = 3.175
    CHAR_W = 1.27                           # 与 visual_check.text_bbox 一致
    pw, ph = PAPERS[paper]
    y_floor = PAGE_MY + 2.0                 # 上边界
    y_ceil = ph - TITLE_H - 2.0             # 标题栏上沿（留 2mm 缓冲）
    nprop = len(prop_texts)
    for p in prop_texts:
        p["hw"] = max(1, len(p["text"])) * CHAR_W / 2
    parent = list(range(nprop))

    def _find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    for i in range(nprop):
        for j in range(i + 1, nprop):
            if abs(prop_texts[i]["x"] - prop_texts[j]["x"]) < \
                    prop_texts[i]["hw"] + prop_texts[j]["hw"]:
                ri, rj = _find(i), _find(j)
                if ri != rj:
                    parent[rj] = ri
    cols = {}
    for i, p in enumerate(prop_texts):
        cols.setdefault(_find(i), []).append(p)
    for col in cols.values():
        col.sort(key=lambda p: p["y"])
        for i in range(len(col) - 1, -1, -1):
            col[i]["y"] = min(col[i]["y"], y_ceil)
            if i < len(col) - 1:
                col[i]["y"] = min(col[i]["y"],
                                  col[i + 1]["y"] - MIN_PROP_PITCH)
            col[i]["y"] = max(col[i]["y"], y_floor)

    property_cells = set()
    for p in prop_texts:
        prop_y[(p["ref"], p["kind"])] = p["y"]
        property_cells.update(_text_cells(_to_grid(p["x"], p["y"]), p["text"]))

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
                     mirror_y=False, text_y=None):
        """text_y: (Reference_y, Value_y) 避让后的绝对坐标（None 则按 hh 推算）。"""
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
        if text_y is None:
            text_y = (y - hh - 2.54, y + hh + 2.54)
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
        L.append(f"      (at {_fmt(x)} {_fmt(text_y[0])} 0)")
        L.append(f'      (effects (font (size 1.27 1.27)){hide})')
        L.append("    )")
        L.append(f'    (property "Value" "{value}"')
        L.append(f"      (at {_fmt(x)} {_fmt(text_y[1])} 0)")
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
                     mirror_y=(ref in mirrored),
                     text_y=(prop_y.get((ref, "Reference"), y - 20),
                             prop_y.get((ref, "Value"), y + 20)))

    wire_segments = []   # (x1, y1, x2, y2, net_key)，用于通用 T 型接点检测
    wire_buffer = []     # 先缓冲，最后统一裁掉悬空短线头再输出
    wire_seen = set()    # 去重：收口路径与引出线可能画出同一段（正反均算重复）

    def _wire(x1, y1, x2, y2, key=None):
        a, b = (round(x1, 3), round(y1, 3)), (round(x2, 3), round(y2, 3))
        # key 必须参与去重：过去异网同段会被静默丢掉，造成一条网络的线
        # 被另一条网络「搭桥」，是 multiple_net_names 的隐藏根因。
        uniq = (tuple(sorted((a, b))), key)
        if uniq in wire_seen:
            return
        wire_seen.add(uniq)
        wire_buffer.append((x1, y1, x2, y2, key))
        wire_segments.append((x1, y1, x2, y2, key))

    # 引脚引出线 + 网络标签 + 同网导线互连 + PWR_FLAG + 未连引脚 no_connect。
    # 方向要点：pin 的 angle 指向符号体，引出线走外侧（反向）；Y 轴要翻转。
    # 过去只有「短线 + 同名标签」，电气上连通但视觉上没连起来；现在额外用
    # 1.27mm 栅格上的正交走线把同一网络的所有端点真正连成一张网。
    pw, ph = PAPERS[paper]
    emitted = set()
    net_jobs = []
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

        needs_flag = _net_needs_flag(net)
        # 通用「长线标签化」判定（纯几何 + 电气类型，不看网名/位号）：
        #   恰好 2 个端点、非电源网、两端跨度超过阈值 → 用两端同名全局标签
        #   连接，跳过横穿长导线。电源主干与多端点网仍走实线，保持直观。
        xs = [e[0] for e in ends] + [e[2] for e in ends]
        ys = [e[1] for e in ends] + [e[3] for e in ends]
        span = max(max(xs) - min(xs), max(ys) - min(ys))
        label_route = (len(ends) == 2 and not needs_flag
                       and span >= LABEL_SPAN_MM)

        net_jobs.append({"key": len(net_jobs), "name": name, "ends": ends,
                         "flag": None, "needs_flag": needs_flag,
                         "touched": set(),
                         "label_route": label_route, "span": span,
                         "refs": {r for r, _k in ok}})

    # ② PWR_FLAG 位置：所有网络的引出线都收集完后统一排布。
    #    过去逐网放置时，VCC/GND 从相邻排针引出，两个旗标会被放到同一 x
    #    坐标、只隔 1 格（2.54mm），菱形尖角叠在一起。这里统一检查已放旗标
    #    和全部引脚引出线，给每个旗标留出独立空间。
    stub_cells = set()
    for _job in net_jobs:
        for _e in _job["ends"]:
            _a, _b = _to_grid(_e[0], _e[1]), _to_grid(_e[2], _e[3])
            if _a[0] == _b[0]:
                _step = 1 if _b[1] >= _a[1] else -1
                _cells = [(_a[0], _j) for _j in range(_a[1], _b[1] + _step, _step)]
            else:
                _step = 1 if _b[0] >= _a[0] else -1
                _cells = [(_i, _a[1]) for _i in range(_a[0], _b[0] + _step, _step)]
            stub_cells.update(_cells)

    flag_no = 0
    placed_flags = []   # 已放旗标的栅格端点 (gi, gj)
    for job in net_jobs:
        if not job["needs_flag"]:
            continue
        cands = []
        for ax, ay, ex, ey, odx, ody in job["ends"]:
            # 先试就近 1 格距离，再逐步外扩；每个引脚都试，避免相邻网络的
            # 旗标只能挤在同一个引出点附近。
            for dist in (12.7, 25.4, 38.1):
                for ox, oy in ((0.0, -dist), (0.0, dist),
                               (odx * dist, ody * dist),
                               (-odx * dist, -ody * dist),
                               (dist, 0.0), (-dist, 0.0)):
                    cands.append((ex + ox, ey + oy))
        fx = fy = None
        for tx, ty in cands:
            gi, gj = _to_grid(tx, ty)
            tx, ty = _from_grid(gi, gj)
            if not (3 <= gi <= int((pw - 3) / GRID)
                    and 3 <= gj <= int((ph - 3) / GRID)):
                continue
            # 菱形本体在引脚点上方约 2 格，连同引脚点一起做避让。
            body = [(gi, gj), (gi, gj - 1), (gi, gj - 2)]
            if any(c in stub_cells for c in body):
                continue
            if any(_hits_symbol(bx, by)
                   or _in_title_block(bx, by, 3, 3, paper)
                   for bx, by in (_from_grid(*c) for c in body)):
                continue
            # 与已有旗标至少留 1 格空隙：本体区间 [gj-2, gj] 各外扩 1 格。
            if any(abs(gi - pgi) <= 2
                   and not (gj + 1 < pgj - 3 or gj - 3 > pgj + 1)
                   for pgi, pgj in placed_flags):
                continue
            fx, fy = tx, ty
            placed_flags.append((gi, gj))
            break
        if fx is None:
            # 极端拥挤时的兜底：至少保证电气上能放下旗标（ERC 优先于观感）。
            ax, ay, ex, ey, odx, ody = job["ends"][0]
            fx, fy = _snap(ex + odx * 12.7), _snap(ey + ody * 12.7)
            placed_flags.append(_to_grid(fx, fy))
        flag_no += 1
        job["flag"] = (fx, fy, f"#FLG{flag_no:02d}")

    # ④ 障碍物：元件符号本体（引脚包围盒 + 2.54mm，比布局占位盒小，
    #    保证引出线端点仍在障碍外）+ 标题栏 + PWR_FLAG 菱形本体。
    #    这些是**通用**规则，对任意板卡都生效，不是针对某一张图的补丁。
    nx, ny = int(pw / GRID) + 1, int(ph / GRID) + 1
    tgi0, tgj0 = _to_grid(pw - TITLE_W, ph - TITLE_H)
    blocked = {(gi, gj)
               for gi in range(max(0, tgi0), nx)
               for gj in range(max(0, tgj0), ny)}
    for it in items:
        ref = it["ref"]
        _, pins = pin_cache[ref]
        if not pins:
            continue
        ix, iy = positions[ref]
        mir = ref in mirrored
        xs = [(-p[0] if mir else p[0]) for p in pins.values()]
        ys = [p[1] for p in pins.values()]
        # 通用：符号本体以局部原点 (0,0) 为中心，引脚线从本体伸向电气点。
        # 过去只用电气点算障碍盒，单侧引脚元件（排针/连接器，如 J1 5V 排针
        # 电气点局部 x=5.08、本体在 x=0~1.27）会漏掉本体那段，导致异网走线
        # 贴着本体右缘（引脚红线区）穿过，视觉上像交叉。把局部原点 0 纳入
        # 范围即可覆盖本体，对所有板卡统一生效。
        xs = xs + [0.0]
        ys = ys + [0.0]
        pad = 2.54
        x0, x1 = ix + min(xs) - pad, ix + max(xs) + pad
        y0, y1 = iy - max(ys) - pad, iy - min(ys) + pad
        i0, j0 = _to_grid(x0, y0)
        i1, j1 = _to_grid(x1, y1)
        for gi in range(max(0, i0), min(nx, i1 + 1)):
            for gj in range(max(0, j0), min(ny, j1 + 1)):
                blocked.add((gi, gj))

    endpoint_cells = set()
    for _job in net_jobs:
        for _e in _job["ends"]:
            endpoint_cells.add(_to_grid(_e[0], _e[1]))
            endpoint_cells.add(_to_grid(_e[2], _e[3]))
        if _job["flag"]:
            endpoint_cells.add(_to_grid(_job["flag"][0], _job["flag"][1]))
    # 菱形本体在引脚点上方 2 格、左右各 1 格（与 visual_check.flag_bbox
    # 同一几何）；全部登记为硬障碍，但真实端点格必须可达。
    # 过去只挡住脚点正上方一列，异网/本网导线可以沿菱形腰部横线擦过本体
    # （y = gj-1 那一行）再拐进脚点，视觉自检就会报“导线穿旗标”。
    flag_body_cells = set()
    for _job in net_jobs:
        if not _job["flag"]:
            continue
        _gi, _gj = _to_grid(_job["flag"][0], _job["flag"][1])
        for _di in (-1, 0, 1):
            for _dj in (1, 2):
                _c = (_gi + _di, _gj - _dj)
                if _c not in endpoint_cells:
                    flag_body_cells.add(_c)
                    blocked.add(_c)

    # ⑤ 通用标签避让：同一网络只挂一个标签，其余端点靠导线连接。
    #    标签锚点默认在 stub 末端；若与别的标签 / 元件文字 / 旗标 / 符号本体
    #    重叠，就沿本网 stub 往引脚方向退 1~3 格（仍在该网络的导线上，
    #    电气不变）。这是对**所有**网络统一执行的检查。
    label_plan = {}
    visual_taken = set(property_cells) | flag_body_cells
    for job in net_jobs:
        touched = sorted(job["touched"])
        primary = touched[0] if touched else 0
        for i, (ax, ay, ex, ey, odx, ody) in enumerate(job["ends"]):
            if i != primary and i in touched:
                continue
            angle = 180 if odx > 0 else 0
            ga, gb = _to_grid(ax, ay), _to_grid(ex, ey)
            sdx = (ga[0] > gb[0]) - (ga[0] < gb[0])
            sdy = (ga[1] > gb[1]) - (ga[1] < gb[1])
            max_step = min(3, abs(ga[0] - gb[0]) + abs(ga[1] - gb[1]))
            chosen = None
            for k in range(max_step + 1):
                cell = (gb[0] + sdx * k, gb[1] + sdy * k)
                cells = _text_cells(cell, job["name"], angle)
                if any(c in visual_taken or c in blocked for c in cells):
                    continue
                chosen = (cell, cells)
                break
            if chosen is None:
                chosen = (gb, _text_cells(gb, job["name"], angle))
            cell, cells = chosen
            lx, ly = _from_grid(*cell)
            label_plan[(job["key"], i)] = (lx, ly, angle, cells)
            visual_taken.update(cells)

    # ④ 预先登记各网络的引出线与端点。
    #    关键：引脚引出线（stub）整段都要登记！过去只登记端点 (ex,ey)，
    #    导致其它网络的走线可以横穿某引脚的整段 stub——这正是截图里芯片脚
    #    附近那一小簇“不必要交叉”的来源。
    # soft: {栅格点: {网络 key}} —— 已被哪些网络的走线占用（软障碍）。
    # 与旧的硬 owner 不同：完整线段的每一格都登记，穿越要付 CROSS_COST，
    # 但不是硬墙——密集区不会因为被封死而断线（旧硬墙方案标签 24→38）。
    soft = {}
    # hard 是「异网绝对不可落脚」的电气锚点集合。过去只把 stub 两端和
    # PWR_FLAG 脚放进 hard，而网络标签锚点和 stub_trim 收口点都落在 stub
    # **中段**：其它网络的走线可以穿过这些点，KiCad 会把异名标签接到同一
    # 条线上，触发 multiple_net_names（8051 上 NET1/NET6、NET17/NET19 就是
    # 这样串到一起的）。这里把整段 stub 都登记为异网硬障碍，覆盖所有可能
    # 的标签/收口锚点；本网自己仍可通行。
    hard = {}
    # edges/vertices 记录「已画导线」的独占关系：异网不能复用同一段，
    # 也不能在对方拐点/端点落脚（十字交叉仍允许，因为只共用中段格点）。
    edge_owner = {}     # 无向边 -> {key}
    vertex_owner = {}   # 拐点/端点格 -> {key}

    def _cells_between(a, b):
        """栅格 a→b 的水平/垂直线段包含的所有格点。"""
        if a[0] == b[0]:
            step = 1 if b[1] >= a[1] else -1
            return [(a[0], j) for j in range(a[1], b[1] + step, step)]
        step = 1 if b[0] >= a[0] else -1
        return [(i, a[1]) for i in range(a[0], b[0] + step, step)]

    def _occupy(key, a, b):
        """把栅格 a→b 的水平/垂直线段登记到 soft。"""
        for c in _cells_between(a, b):
            soft.setdefault(c, set()).add(key)

    # 引脚引出线（stub）全程登记：soft 用于少交叉；hard 用于阻止异网
    # 在 stub 中段落点（标签、收口点都在这里），从源头杜绝异名标签串网。
    for key, job in enumerate(net_jobs):
        for e in job["ends"]:
            ga, gb = _to_grid(e[0], e[1]), _to_grid(e[2], e[3])
            _occupy(key, ga, gb)
            for c in _cells_between(ga, gb):
                hard.setdefault(c, set()).add(key)
            edge_owner.setdefault(_edge_key(ga, gb), set()).add(key)
            vertex_owner.setdefault(ga, set()).add(key)
            vertex_owner.setdefault(gb, set()).add(key)
        if job["flag"]:
            fc = _to_grid(job["flag"][0], job["flag"][1])
            soft.setdefault(fc, set()).add(key)
            hard.setdefault(fc, set()).add(key)
            vertex_owner.setdefault(fc, set()).add(key)

    # 元件文字与网络标签统一作为“视觉软障碍”登记：走线能绕则绕，绕不开
    # 仍可穿越（保连通）。-1 是图形占位哨兵，不属于任何真实网络。
    for c in property_cells:
        soft.setdefault(c, set()).add(-1)
    for _plan in label_plan.values():
        for c in _plan[3]:
            soft.setdefault(c, set()).add(-1)

    def _commit(key, path):
        path = _shorten_path(path, blocked, soft, hard, key,
                             edge_owner, vertex_owner)
        for (x1, y1), (x2, y2) in _compress_segments(path):
            if (x1, y1) != (x2, y2):
                ga, gb = _to_grid(x1, y1), _to_grid(x2, y2)
                _occupy(key, ga, gb)
                _wire(x1, y1, x2, y2, key)
                edge_owner.setdefault(_edge_key(ga, gb), set()).add(key)
                vertex_owner.setdefault(ga, set()).add(key)
                vertex_owner.setdefault(gb, set()).add(key)

    stub_trim = {}   # (net_key, end_idx) -> 收口后的 stub 末端坐标

    def _mst_route(key, job, pts):
        end_cells = set(pts)
        if job["flag"]:
            end_cells.add(_to_grid(job["flag"][0], job["flag"][1]))

        # 第一步：先算出所有边，并找出「从引脚引出线中途接入」的收口点。
        # 必须两遍处理：同一个引出线末端可能连着多条 MST 边，只收口其中一条
        # 会让另一条悬空（曾导致 ERC wire_dangling）。
        edges = list(_mst_edges(pts))
        paths = {}
        candidates = {}
        for a, b in edges:
            path = _route_path(pts[a], pts[b], blocked, soft, hard, key, nx, ny,
                               end_cells, edge_owner, vertex_owner)
            if not path:
                continue                       # 走不通时仍保留网络标签兜底
            paths[(a, b)] = path
            for idx, at_start in ((a, True), (b, False)):
                if idx >= len(job["ends"]) or len(path) < 2:
                    continue
                endpoint = path[0] if at_start else path[-1]
                neighbor = path[1] if at_start else path[-2]
                if endpoint != pts[idx]:
                    continue
                ax, ay, ex, ey, _odx, _ody = job["ends"][idx]
                ga, gb = _to_grid(ax, ay), _to_grid(ex, ey)
                inside = ((ga[0] == gb[0] == neighbor[0]
                           and min(ga[1], gb[1]) < neighbor[1] < max(ga[1], gb[1]))
                          or (ga[1] == gb[1] == neighbor[1]
                              and min(ga[0], gb[0]) < neighbor[0] < max(ga[0], gb[0])))
                if inside:
                    candidates.setdefault(idx, []).append(neighbor)

        # 同一末端有多个接入点时，收口到离引脚最远的那个，保留最多连线。
        targets = {}
        for idx, cells in candidates.items():
            ga = _to_grid(job["ends"][idx][0], job["ends"][idx][1])
            target = max(cells, key=lambda c: abs(c[0] - ga[0]) + abs(c[1] - ga[1]))
            targets[idx] = target
            stub_trim[(key, idx)] = _from_grid(*target)

        # 第二步：把所有连到该末端的路径都改接到收口点。
        for (a, b), path in paths.items():
            job["touched"].update((a, b))
            for idx, at_start in ((a, True), (b, False)):
                if idx not in targets or len(path) < 2:
                    continue
                target = targets[idx]
                original = pts[idx]
                if at_start:
                    prev = path[1]
                    if prev == target:
                        path = path[1:]
                    elif abs(prev[0] - target[0]) + abs(prev[1] - target[1]) == 1:
                        path[0] = target
                    else:
                        extra = set(blocked) | {original}
                        rp = _route_path(prev, target, extra, soft, hard, key,
                                         nx, ny, end_cells | {target},
                                         edge_owner, vertex_owner)
                        if rp and len(rp) >= 2:
                            path = rp[::-1] + path[1:]
                else:
                    prev = path[-2]
                    if prev == target:
                        path = path[:-1]
                    elif abs(prev[0] - target[0]) + abs(prev[1] - target[1]) == 1:
                        path[-1] = target
                    else:
                        extra = set(blocked) | {original}
                        rp = _route_path(prev, target, extra, soft, hard, key,
                                         nx, ny, end_cells | {target},
                                         edge_owner, vertex_owner)
                        if rp and len(rp) >= 2:
                            path = path[:-2] + rp
            if len(path) >= 2:
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
        # 长距离点对点信号：两端各留短 stub + 同名全局标签即可，
        # 不画横穿全图的长导线（图面清爽、风格统一、不会绕不开而断线）。
        if job["label_route"]:
            continue
        pts = [_to_grid(e[2], e[3]) for e in job["ends"]]
        if job["flag"]:
            pts.append(_to_grid(job["flag"][0], job["flag"][1]))
        _mst_route(key, job, pts)

    # ⑥ 输出引出线、标签和 PWR_FLAG。
    # 已用导线连通的网络只挂一个标签；未接通的端点保留标签兜底。

    def _stub_early_tap(ax, ay, bx, by, key):
        """stub 段 ax→bx 上若有「同网导线的接入点」落在其严格内部，
        返回其中最靠近引脚(ax)的那个点。stub 应收口到这里：
        否则 junction 画在接入点，stub 却继续伸到原末端，形成一小段悬空线头
        （截图里 20 针排针每个引脚旁多出的那一截）。

        接入点 = 同网导线段的端点，且该端点严格位于 stub 两端之间。
        """
        EPS = 1e-6
        vert = abs(ax - bx) < EPS
        taps = []
        for x1, y1, x2, y2, k in wire_segments:
            if k != key:
                continue
            for px, py in ((x1, y1), (x2, y2)):
                if vert:
                    if abs(px - ax) < EPS and min(ay, by) + EPS < py < max(ay, by) - EPS:
                        taps.append((px, py))
                else:
                    if abs(py - ay) < EPS and min(ax, bx) + EPS < px < max(ax, bx) - EPS:
                        taps.append((px, py))
        if not taps:
            return None
        # 取最靠近引脚的接入点
        return min(taps, key=lambda p: abs(p[0] - ax) + abs(p[1] - ay))

    # ⑥b 最终标签文字方向再优化。label_plan 的避让发生在 stub_trim/early
    #     tap 之前，锚点后来挪到导线中段后原避让会失效（abecba76 的 VCC/V3
    #     只差 2.54mm、文字却要 3.175mm）。锚点**不能**沿导线挪：路由线是
    #     按原接入点算的，一挪就会留下悬空线头。所以只改文字方向，用全局
    #     局部搜索 minimization 总重叠数，锚点与走线都不动。
    label_items = []
    for job in net_jobs:
        touched = sorted(job["touched"])
        primary = touched[0] if touched else 0
        for i, (ax, ay, ex, ey, odx, ody) in enumerate(job["ends"]):
            if i != primary and i in touched:
                continue
            lx, ly, angle, _cells = label_plan[(job["key"], i)]
            if (job["key"], i) in stub_trim:
                lx, ly = stub_trim[(job["key"], i)]
            _tex, _tey = stub_trim.get((job["key"], i), (ex, ey))
            _tap = _stub_early_tap(ax, ay, _tex, _tey, job["key"])
            if _tap is not None:
                lx, ly = _tap
            orig = 90 if angle in (90, 270) else angle
            ga, gb = _to_grid(ax, ay), _to_grid(lx, ly)
            cells = list(_cells_between(ga, gb))
            cells.reverse()          # 从锚点往引脚方向，锚点始终在已画导线上
            # 全局标签给更多候选：既可调角度，也可沿引出线 stub 往引脚方向
            # 平移锚点（电气不变——仍在同一条 stub 上）。这样两个从相反
            # 方向引出、末端碰巧落在同一行的标签能各自退开、不再互压。
            lab_global = job["label_route"]
            if lab_global:
                # cells 已按「从锚点往引脚」排序；跳过第 0 格（原位只调角度）
                slide_cells = [gb] + cells[1:]
                cands = [(c, a) for c in slide_cells for a in (orig, 90)]
            else:
                cands = [(gb, a) for a in (orig, 90)]
            label_items.append({"key": (job["key"], i), "text": job["name"],
                                "x": lx, "y": ly, "cell": gb,
                                "orig": orig, "angle": orig, "cands": cands,
                                "pin_cell": ga, "stub_cells": cells, "stub_end": gb,
                                "pin": (ax, ay), "stub": (ex, ey),
                                "label_route": job["label_route"]})

    _CHAR_W, _TEXT_H = 1.27, 1.27
    fixed_boxes = []
    for p in prop_texts:
        w = max(1, len(p["text"])) * _CHAR_W
        fixed_boxes.append((p["x"] - w / 2, p["y"] - _TEXT_H / 2,
                            p["x"] + w / 2, p["y"] + _TEXT_H / 2))

    def _label_box(lab):
        w = max(1, len(lab["text"])) * _CHAR_W
        if lab["angle"] == 90:
            return (lab["x"] - _TEXT_H / 2, lab["y"] - w / 2,
                    lab["x"] + _TEXT_H / 2, lab["y"] + w / 2)
        return (lab["x"] - w / 2, lab["y"] - _TEXT_H / 2,
                lab["x"] + w / 2, lab["y"] + _TEXT_H / 2)

    def _overlap(a, b):
        tol = 0.01
        return not (a[2] <= b[0] + tol or b[2] <= a[0] + tol
                    or a[3] <= b[1] + tol or b[3] <= a[1] + tol)

    def _total_clash():
        boxes = [_label_box(lab) for lab in label_items]
        cost = sum(_overlap(b, f) for b in boxes for f in fixed_boxes)
        for i in range(len(boxes)):
            for j in range(i + 1, len(boxes)):
                if _overlap(boxes[i], boxes[j]):
                    cost += 1
        return cost

    def _seg_hits_rect(x1, y1, x2, y2, rect, inset=0.35, tol=0.01):
        """与 visual_check.seg_hits_rect 同口径：导线是否穿进矩形内部。"""
        ix0, iy0, ix1, iy1 = rect[0] + inset, rect[1] + inset, \
                             rect[2] - inset, rect[3] - inset
        if ix0 >= ix1 or iy0 >= iy1:
            ix0, iy0, ix1, iy1 = rect
        if ix0 < x1 < ix1 and iy0 < y1 < iy1:
            return True
        if ix0 < x2 < ix1 and iy0 < y2 < iy1:
            return True
        if abs(y1 - y2) < tol:
            return (iy0 < y1 < iy1
                    and max(x1, x2) > ix0 + tol and min(x1, x2) < ix1 - tol)
        if abs(x1 - x2) < tol:
            return (ix0 < x1 < ix1
                    and max(y1, y2) > iy0 + tol and min(y1, y2) < iy1 - tol)
        return False

    def _line_contact_cost():
        """标签文字被**异网**导线穿过的数量（视觉 info，非错误）。

        作为标签候选的次要排序目标：只在文字重叠数相同的候选之间挑更少
        邻线接触的位置，绝不为了消 info 牺牲 0 重叠。锚点仍受本网导线约束，
        不改电气、不串网。引脚引出线此时尚未进 wire_buffer，故只能减少、
        不保证完全归零。
        """
        cost = 0
        for lab in label_items:
            bb = _label_box(lab)
            key = lab["key"][0]
            for x1, y1, x2, y2, k in wire_buffer:
                if k == key:        # 本网导线是标签宿主，不算邻线
                    continue
                if _seg_hits_rect(x1, y1, x2, y2, bb):
                    cost += 1
                    break
        return cost

    def _score():
        return (_total_clash(), _line_contact_cost())

    def _apply(lab, cand):
        lab["cell"], lab["angle"] = cand
        lab["x"], lab["y"] = _from_grid(*cand[0])

    def _relax_passes():
        for _pass in range(6):
            changed = False
            for lab in label_items:
                before = _score()
                best = ((lab["cell"], lab["angle"]), before)
                for cand in lab["cands"]:
                    _apply(lab, cand)
                    cost = _score()
                    if cost < best[1]:
                        best = (cand, cost)
                _apply(lab, best[0])
                if best[1] < before:
                    changed = True
            if not changed:
                break

    def _anchor_wire_cells(key, x, y) -> set:
        """锚点所在同网导线上的全部栅格点（标签只能在这些点上滑动）。"""
        out = set()
        cx, cy = _to_grid(x, y)
        for x1, y1, x2, y2, k in wire_buffer:
            if k != key:
                continue
            if abs(y1 - y2) < 0.01 and abs(y1 - y) < 0.01 \
                    and min(x1, x2) - 0.01 <= x <= max(x1, x2) + 0.01:
                a, b = _to_grid(min(x1, x2), y1)[0], _to_grid(max(x1, x2), y1)[0]
                out.update((i, cy) for i in range(a, b + 1))
            if abs(x1 - x2) < 0.01 and abs(x1 - x) < 0.01 \
                    and min(y1, y2) - 0.01 <= y <= max(y1, y2) + 0.01:
                a, b = _to_grid(x1, min(y1, y2))[1], _to_grid(x1, max(y1, y2))[1]
                out.update((cx, j) for j in range(a, b + 1))
        return out

    _relax_passes()
    # 已有候选（沿 stub 往引脚滑 + 改竖排）仍消不掉重叠时，给标签更多候选：
    #   - 锚点不在导线上（长距离同名标，线头已被裁掉）→ 可小幅自由平移；
    #   - 锚点确实落在导线上 → 只沿 stub 向外延伸几格，并同步延长引出线。
    # 两种情况都不改变网络的电气连接，锚点仍在本网线上或仍靠同名连通。
    if _total_clash():
        for lab in label_items:
            ga, gb = lab["pin_cell"], lab["stub_end"]
            lx, ly = lab["x"], lab["y"]
            wire_cells = _anchor_wire_cells(lab["key"][0], lx, ly)
            # 引脚引出线要等优化之后才真正 _wire()，此时还没进 wire_buffer；
            # 但它同样是标签锚点所在的导线，必须一并作为可滑动范围。
            wire_cells.update(lab["stub_cells"])
            # 锚点在长距离同名标上且没有导线：可小幅自由平移；锚点在导线上：
            # 只能沿该网导线滑动，绝不能挪到别的网名标签附近（ERC 会串网）。
            free = not wire_cells
            extra_cells = set()
            if free:
                bx, by = _to_grid(lx, ly)
                extra_cells.update((bx + dx, by + dy)
                                   for dx in range(-4, 5) for dy in range(-2, 3))
            else:
                extra_cells.update(wire_cells - {(gb[0], gb[1])})
                step = ((gb[0] > ga[0]) - (gb[0] < ga[0]),
                        (gb[1] > ga[1]) - (gb[1] < ga[1]))
                if step != (0, 0):
                    extra_cells.update((gb[0] + step[0] * k, gb[1] + step[1] * k)
                                       for k in range(1, 5))
            for cell in extra_cells:
                x, y = _from_grid(*cell)
                hw = max(1, len(lab["text"])) * _CHAR_W / 2
                if not (2 <= x <= pw - 2 and 2 <= y <= ph - 2) \
                        or _in_title_block(x, y, hw, _TEXT_H / 2, paper):
                    continue
                for a in (lab["orig"], 90):
                    cand = (cell, a)
                    if cand not in lab["cands"]:
                        lab["cands"].append(cand)
        _relax_passes()

    final_label_plan = {}
    for lab in label_items:
        final_label_plan[lab["key"]] = (
            lab["x"], lab["y"], lab["angle"],
            _text_cells(_to_grid(lab["x"], lab["y"]), lab["text"], lab["angle"]))

    junc_pts = set()   # 引脚电气连接点：加实心圆点，明确「线接在这里」
    emitted_labels = set()   # 真正写出的标签锚点（stub_trim 后可能已挪位）
    for job in net_jobs:
        name = job["name"]
        key = job["key"]
        touched = sorted(job["touched"])
        primary = touched[0] if touched else 0
        for i, (ax, ay, ex, ey, odx, ody) in enumerate(job["ends"]):
            # 通用线头消除：如果这条引出线没有被任何走线接入，而本网的
            # 标签又挂在别的端点上，这截短线就是悬空「线头」——不画。
            if touched and i not in touched:
                continue
            tex, tey = stub_trim.get((key, i), (ex, ey))
            # 标签避让可能把锚点移到 stub 外侧：把引出线同步延长到新锚点，
            # 否则文字画在线头之外，锚点悬空且 ERC 会报 label_dangling。
            if job["label_route"] and (key, i) in final_label_plan:
                lxp, lyp = final_label_plan[(key, i)][:2]
                ax0, ay0 = job["ends"][i][0], job["ends"][i][1]
                far_x = abs(lxp - ax0) > abs(tex - ax0) + 0.01
                far_y = abs(lyp - ay0) > abs(tey - ay0) + 0.01
                if (far_x and abs(ay0 - tex) < 0.01 and abs(ay0 - lyp) < 0.01) \
                        or (far_y and abs(ax0 - tey) < 0.01 and abs(ax0 - lxp) < 0.01):
                    tex, tey = lxp, lyp
            # stub 中途若有同网导线接入，收口到最靠引脚的接入点
            tap = _stub_early_tap(ax, ay, tex, tey, key)
            if tap is not None:
                tex, tey = tap
            _wire(ax, ay, tex, tey, key)
            # 不在这里给引脚端点加圆点：单根线接到引脚是正常连接，画圆点只会
            # 让「圆点 + stub 横线 + 引脚红线」看起来像多伸出一截的线头。
            # 是否画圆点统一交给下面的 T 型汇聚检测（≥3 线或端点顶在中段）。
        for i, (ax, ay, ex, ey, odx, ody) in enumerate(job["ends"]):
            if i != primary and i in touched:
                continue
            # 锚点与角度已由 ⑥b 按最终 stub/early-tap 位置统一排过重叠
            lx, ly, langle, _cells = final_label_plan[(key, i)]
            # 长距离同名连接一律用**无框** label：同页同名即连通（KiCad 规则），
            # ERC/网表与全局标签完全一致（已用 kicad-cli 导出网表逐网比对）。
            # 旧版用带框 global_label，但 KiCad 8 的 passive 框固定从锚点向右
            # 展开（与 angle/justify/shape 无关，实测），当导线也从右侧进入
            # 锚点（芯片左脚向左引出）时，框会压住 2~11mm 绿色导线，视觉上
            # 像把线截断。无框同名标保留「一眼看出是有意同名连接」的好处，
            # 又不再遮挡导线。分类名由 label_route 决定，只影响画法不影响电气。
            L.append(f'  (label "{name}"')
            L.append(f"    (at {_fmt(lx)} {_fmt(ly)} {langle})")
            L.append('    (effects (font (size 1.27 1.27)))')
            L.append(f'    (uuid "{_u()}")')
            L.append("  )")
            # 登记真实标签锚点：线头裁剪要用它保护「导线恰好终止于标签」的
            # 端点。过去这个集合定义了却从不 add，等于标签端点毫无保护。
            emitted_labels.add((round(lx, 3), round(ly, 3)))
        if job["flag"]:
            fx, fy, fref = job["flag"]
            _emit_symbol(fref, "power:PWR_FLAG", fx, fy,
                         {1: (0.0, 0.0, 90.0)}, "PWR_FLAG", in_bom="no",
                         hide_text=True)

    # 通用「线头」清理：所有导线画完后，若某段的末端既不是引脚、标签、
    # junction，也没有别的导线接上，而这段又很短（≤3 格），它就是截图里
    # 那种多伸出来的线头——直接裁掉。级联处理，直到没有新的短线头出现。
    from collections import Counter as _Counter
    pin_pts = {(round(e[0], 3), round(e[1], 3))
               for _j in net_jobs for e in _j["ends"]}
    # PWR_FLAG 的短引出线绝不能裁：裁掉后 ERC 会报 power_pin_not_driven。
    pin_pts |= {(round(_j["flag"][0], 3), round(_j["flag"][1], 3))
                for _j in net_jobs if _j["flag"]}
    label_pts = emitted_labels

    # 引脚引出线若「穿过」本网标签锚点又继续往外伸，标签外侧那一格就是
    # 视觉线头（ERC 仍通过，因为标签在导线中段已连通）。把这样的 stub 在
    # 标签锚点处劈成两段：标签侧正常接线，外侧段随后被下面的短段裁剪移除。
    # 只劈「一端是引脚、锚点严格落在其内部且共线」的段，绝不碰走线中段。
    def _split_at_label():
        EPS = 1e-6
        changed = False
        out = []
        for x1, y1, x2, y2, k in wire_segments:
            a = (round(x1, 3), round(y1, 3))
            b = (round(x2, 3), round(y2, 3))
            # 只要标签锚点严格落在这段导线内部，就该在此劈开——不论端点
            # 是不是引脚。旧版只劈「一端是引脚」的段，导致「走线端点→走线
            # 端点」的段中间挂了标签时不被劈，标签外侧那半段成了视觉线头
            # （整段超过短段裁剪阈值，于是永远留着）。劈开后外侧半段会
            # 被下面的短段裁剪正确移除，电气不变（标签仍在导线上）。
            horiz = abs(y1 - y2) < EPS
            vert = abs(x1 - x2) < EPS
            if not (horiz or vert):
                out.append((x1, y1, x2, y2, k))
                continue
            # 落在该段严格内部、且共线的标签锚点
            cuts = []
            for lp in emitted_labels:
                if horiz:
                    if abs(lp[1] - y1) < EPS and min(x1, x2) + EPS < lp[0] < max(x1, x2) - EPS:
                        cuts.append(lp[0])
                else:
                    if abs(lp[0] - x1) < EPS and min(y1, y2) + EPS < lp[1] < max(y1, y2) - EPS:
                        cuts.append(lp[1])
            if not cuts:
                out.append((x1, y1, x2, y2, k))
                continue
            cuts.sort(reverse=(x1 > x2 or y1 > y2))
            pts = [(x1, y1)] + [((c, y1) if horiz else (x1, c)) for c in cuts] + [(x2, y2)]
            for u, v in zip(pts, pts[1:]):
                out.append((u[0], u[1], v[0], v[1], k))
            changed = True
        if changed:
            wire_segments[:] = out
        return changed

    _split_at_label()
    for _ in range(len(wire_segments) + 1):
        deg = _Counter()
        for x1, y1, x2, y2, _k in wire_segments:
            deg[(round(x1, 3), round(y1, 3))] += 1
            deg[(round(x2, 3), round(y2, 3))] += 1
        valid = (pin_pts | label_pts
                 | {(round(x, 3), round(y, 3)) for x, y in junc_pts}
                 | {p for p, n in deg.items() if n >= 2})
        drop = []
        for seg in wire_segments:
            x1, y1, x2, y2, _k = seg
            # 旧版只裁 ≤3 格的短线头，导致「标签锚点被 stub_trim 挪走后
            # 残留的长 stub」（末端 deg=1、既非引脚也非标签也非接点）因
            # 太长而永远保留，成为截图里的悬空线头。悬空端点本身就是
            # 作废的充分条件：合法 stub 的末端必落在引脚/标签/接点上。
            # 去掉长度限制，但要求该段至少一端 deg=1 且完全悬空。
            for p in ((round(x1, 3), round(y1, 3)), (round(x2, 3), round(y2, 3))):
                if deg[p] == 1 and p not in valid:
                    drop.append(seg)
                    break
        if not drop:
            break
        for seg in drop:
            if seg in wire_segments:
                wire_segments.remove(seg)
    # 直接输出裁剪后的 wire_segments：它已是去重全集，且含标签锚点劈出的
    # 子段；绕 wire_buffer 反而会漏掉子段、留下被裁的整段。
    for x1, y1, x2, y2, key in wire_segments:
        L.append("  (wire")
        L.append("    (pts")
        L.append(f"      (xy {_fmt(x1)} {_fmt(y1)}) (xy {_fmt(x2)} {_fmt(y2)})")
        L.append("    )")
        L.append('    (stroke (width 0) (type solid))')
        L.append(f'    (uuid "{_u()}")')
        L.append("  )")

    # 通用 T 型接点检测：一段导线末端顶在另一段**同网**导线中段，或三段以上
    # 同网导线汇聚时，KiCad 电气上已连通，但缺少 junction 圆点会看成“线头”
    # （截图里 PWR_FLAG 接到主干、GND 接到稳压器引脚引出线处都属此类）。
    # 普通转角/两段直通不加圆点，避免图纸上到处是点。
    EPS = 1e-6

    def _is_endpoint(p, seg):
        x, y = p
        x1, y1, x2, y2, _k = seg
        return ((abs(x - x1) < EPS and abs(y - y1) < EPS)
                or (abs(x - x2) < EPS and abs(y - y2) < EPS))

    def _is_strict_inside(p, seg):
        x, y = p
        x1, y1, x2, y2, _k = seg
        if abs(x1 - x2) < EPS:
            return (abs(x - x1) < EPS
                    and min(y1, y2) + EPS < y < max(y1, y2) - EPS)
        if abs(y1 - y2) < EPS:
            return (abs(y - y1) < EPS
                    and min(x1, x2) + EPS < x < max(x1, x2) - EPS)
        return False

    def _is_redundant_seg(seg):
        """段 seg 是否被另一条「同网、共线」段完全覆盖（冗余重画）。

        同一网络多条 MST 边各自寻路时会共用主干，画出互相覆盖的水平/竖直线
        段。被覆盖段的端点落在长段中段，会被 T 接检测误判成 junction（右下
        角多出的圆点、密脚区线头都源于此）。这类段识别出来后，其端点不作为
        T 接证据——真正的 T 接（垂直线接入主干）不会被判为冗余。
        """
        x1, y1, x2, y2, key = seg
        if key is None:
            return False
        horiz = abs(y1 - y2) < EPS
        vert = abs(x1 - x2) < EPS
        if not (horiz or vert):
            return False
        lo, hi = (min(x1, x2), max(x1, x2)) if horiz else (min(y1, y2), max(y1, y2))
        for X1, Y1, X2, Y2, k2 in wire_segments:
            if k2 != key or (X1, Y1, X2, Y2) == (x1, y1, x2, y2):
                continue
            if horiz:
                if abs(Y1 - Y2) < EPS and abs(Y1 - y1) < EPS:
                    if min(X1, X2) <= lo + EPS and max(X1, X2) >= hi - EPS \
                            and (max(X1, X2) - min(X1, X2)) > (hi - lo) + EPS:
                        return True
            else:
                if abs(X1 - X2) < EPS and abs(X1 - x1) < EPS:
                    if min(Y1, Y2) <= lo + EPS and max(Y1, Y2) >= hi - EPS \
                            and (max(Y1, Y2) - min(Y1, Y2)) > (hi - lo) + EPS:
                        return True
        return False

    probe_pts = set()
    for x1, y1, x2, y2, _k in wire_segments:
        probe_pts.add((x1, y1))
        probe_pts.add((x2, y2))
    for p in probe_pts:
        touching = [seg for seg in wire_segments
                    if seg[4] is not None
                    and (_is_endpoint(p, seg) or _is_strict_inside(p, seg))]
        if len(touching) < 2:
            continue
        # 关键：只有汇聚导线**全部属于同一网络**才是真接点。不同网络的导线
        # 几何交叉（如 VOUT 竖线穿过 GND 水平线）绝不能画 junction 圆点，
        # 否则视觉上像把两个网络短接，误导阅读（电气上 KiCad 并未相连）。
        # 注意还要检查「异网线段的中段正好穿过本网接点」：KiCad 的 junction
        # 会把穿过它的所有线都连起来，画点就等于主动把异网短接。
        keys = {seg[4] for seg in touching}
        if len(keys) != 1:
            continue
        foreign = [seg for seg in wire_segments
                   if seg[4] is not None and seg[4] != next(iter(keys))
                   and (_is_endpoint(p, seg) or _is_strict_inside(p, seg))]
        if foreign:
            continue
        # 被同网长段完全覆盖的冗余段：其端点不作为 T 接证据（伪接点）。
        effective = [seg for seg in touching
                     if not (_is_endpoint(p, seg) and _is_redundant_seg(seg))]
        has_endpoint = any(_is_endpoint(p, seg) for seg in effective)
        has_inside = any(_is_strict_inside(p, seg) for seg in effective)
        if len(effective) >= 3 or (has_endpoint and has_inside):
            junc_pts.add((round(p[0], 3), round(p[1], 3)))

    # 引脚电气连接点与 T 型接点画实心圆点（junction）：纯视觉标记，让「导线
    # 确实接到了这里」一眼可见，消除线头观感。不改电气连接，ERC 不受影响。
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
