"""原理图视觉重叠/排版自检（不改电气，纯几何近似）。

把过去只能靠人眼截图发现的问题变成可自动检测：
  • 文字互压：元件 Reference/Value 与网络标签两两包围盒重叠
  • 导线穿文字：异网导线从文字中间穿过
  • 导线穿符号本体 / PWR_FLAG 菱形
  • 旗标互压、标题栏侵入
  • 导线十字交叉（异网交叉属标准画法，仅作 info 提示；同网无点才是真问题）

几何采用与 kicad_writer 一致的 1.27mm 栅格与字符宽估算，因此检测的是
「生成器能控制的那部分排版」。ERC 仍由 kicad-cli 独立负责，两者互补。
"""
from __future__ import annotations

import re
from pathlib import Path

GRID = 1.27
CHAR_W = 1.27
TEXT_H = 1.27
# 两行属性文字（上元件的 Value / 下元件的 Reference）中心纵向最小间距。
# 1.27mm 字高时 2.0mm 已明显贴靠，取 2.54mm（2 格）作通用下限。
MIN_TEXT_PITCH = 2.54
TITLE_W, TITLE_H = 122.0, 42.0
PAPERS = {"A4": (297.0, 210.0), "A3": (420.0, 297.0)}


def parse_schematic(path) -> dict:
    s = Path(path).read_text(encoding="utf-8", errors="replace")
    m = re.search(r'\(paper "([^"]+)"', s)
    paper = m.group(1) if m else "A4"
    pw, ph = PAPERS.get(paper, PAPERS["A4"])

    li = s.find("(lib_symbols")
    head, tail = (s[:li], s[li:]) if li >= 0 else (s, "")

    # KiCad 8 的 lib_symbols 里每个 symbol 定义常被序列化成单行，旧的
    # 依赖换行的正则会匹配 0 个，导致「导线穿符号本体」检测长期空跑。
    # 这里在 lib_symbols 子串上按括号深度切出每个顶层 symbol 定义再解析。
    lib_pins = {}
    li_tail = tail[tail.find("(lib_symbols"):] if "(lib_symbols" in tail else tail
    for sm in re.finditer(r'\(symbol "([^"]+)"', li_tail):
        name = sm.group(1)
        start = sm.start()
        depth, end = 0, None
        for k in range(start, len(li_tail)):
            c = li_tail[k]
            if c == '(':
                depth += 1
            elif c == ')':
                depth -= 1
                if depth == 0:
                    end = k + 1
                    break
        body = li_tail[start:end] if end else ""
        pts = [(float(a), float(b)) for a, b in
               re.findall(r'\(pin \w+ \w+\s*\(at ([-\d.]+) ([-\d.]+)', body)]
        if pts:
            lib_pins[name] = pts

    insts, texts = [], []
    for sm in re.finditer(r'\n  \(symbol\s*\n(.*?)\n  \)', tail, re.S):
        b = sm.group(1)
        lid = re.search(r'\(lib_id "([^"]+)"', b)
        at = re.search(r'\(at ([-\d.]+) ([-\d.]+)', b)
        if not (lid and at):
            continue
        ref = re.search(r'\(property "Reference" "([^"]*)"', b)
        val = re.search(r'\(property "Value" "([^"]*)"', b)
        insts.append({"lib_id": lid.group(1),
                      "x": float(at.group(1)), "y": float(at.group(2)),
                      "ref": ref.group(1) if ref else "?",
                      "val": val.group(1) if val else "",
                      "mirror": "(mirror y)" in b})
        for pname in ("Reference", "Value"):
            pm = re.search(r'\(property "' + pname + r'" "([^"]*)"[\s\S]{0,160}?\(at ([-\d.]+) ([-\d.]+)', b)
            if not (pm and pm.group(1).strip()):
                continue
            # 隐藏文字（如 PWR_FLAG 的 Value="PWR_FLAG"）不显示，不该算重叠。
            # 必须只看「这一个 property 自己的块」：固定取 220 字符会把后面
            # 隐藏的 Footprint/Datasheet 也算进来，误把可见的 Value 判成隐藏，
            # 于是 Value 从未参与重叠检测——这是 R1.Value 与 SW2.Reference
            # 贴在一起却漏检的根因。块边界＝下一个 (property 或实例结束。
            tail_b = b[pm.start():]
            nxt = tail_b.find('(property ', 12)
            seg = tail_b[:nxt] if nxt > 0 else tail_b[:400]
            if re.search(r'\(hide\s+yes\)', seg):
                continue
            texts.append({"kind": pname, "text": pm.group(1),
                          "x": float(pm.group(2)), "y": float(pm.group(3)),
                          "angle": 0, "center": True})

    flat = re.sub(r'\s+', ' ', s)   # KiCad 会把 wire/label 的 xy 放在同一行

    # 普通 label 与带方框的 global_label 都算网络标签：后者是长距离点对点
    # 信号主动采用的连接形式，若不识别会把它的锚点误判成悬空线头。
    # global_label 也可能带 (shape ...) / (justify ...) 等字段，用宽松匹配。
    for lm in re.finditer(
            r'\(global_label "([^"]*)"', flat):
        seg = flat[lm.start():lm.start() + 220]
        at = re.search(r'\(at ([-\d.]+) ([-\d.]+) ([-\d.]+)\)', seg)
        if at:
            texts.append({"kind": "GlobalLabel", "text": lm.group(1),
                          "x": float(at.group(1)), "y": float(at.group(2)),
                          "angle": int(float(at.group(3))) % 360,
                          "center": False, "is_label": True})
    for lm in re.finditer(r'\(label "([^"]*)" \(at ([-\d.]+) ([-\d.]+) ([-\d.]+)\)', flat):
        texts.append({"kind": "Label", "text": lm.group(1),
                      "x": float(lm.group(2)), "y": float(lm.group(3)),
                      "angle": int(float(lm.group(4))) % 360, "center": False,
                      "is_label": True})

    wires = [tuple(float(x) for x in m.groups()) for m in re.finditer(
        r'\(wire \(pts \(xy ([-\d.]+) ([-\d.]+)\) \(xy ([-\d.]+) ([-\d.]+)\)', flat)]
    juncs = [(float(a), float(b)) for a, b in re.findall(
        r'\(junction \(at ([-\d.]+) ([-\d.]+)\)', flat)]

    return {"path": path, "paper": paper, "pw": pw, "ph": ph,
            "lib_pins": lib_pins, "insts": insts, "texts": texts,
            "wires": wires, "juncs": juncs}


def text_bbox(t):
    w = max(1, len(t["text"])) * CHAR_W
    h = TEXT_H
    if t["center"] or not t.get("is_label"):
        # 元件 Reference/Value：锚点居中。
        return (t["x"] - w / 2, t["y"] - h / 2, t["x"] + w / 2, t["y"] + h / 2)
    # KiCad 8 SVG 实证：label 恒为 text-anchor="middle"，文字以锚点居中。
    # 0/180 都是横排（只改变小箭头朝向），90/270 才是竖排。与 writer 的
    # _text_cells 用同一套定义，避免「生成器以为躲开了、自检却漏检」。
    if t["angle"] in (90, 270):
        return (t["x"] - h / 2, t["y"] - w / 2, t["x"] + h / 2, t["y"] + w / 2)
    return (t["x"] - w / 2, t["y"] - h / 2, t["x"] + w / 2, t["y"] + h / 2)


def symbol_bbox(inst, lib_pins):
    pins = lib_pins.get(inst["lib_id"])
    if not pins:
        return None
    xs = [(-px if inst["mirror"] else px) for px, _ in pins]
    ys = [py for _, py in pins]
    # 符号本体以局部原点为中心，引脚线从本体伸到电气点。单侧引脚元件（排针/
    # 连接器）若只按电气点取范围会漏掉本体那段，异网走线贴着引脚红线穿过时
    # 就检不出来（如 J1 5V 排针）。把局部原点 0 纳入，覆盖本体+引脚线全段。
    xs = xs + [0.0]
    ys = ys + [0.0]
    return (inst["x"] + min(xs), inst["y"] - max(ys),
            inst["x"] + max(xs), inst["y"] - min(ys))


def flag_bbox(inst):
    x, y = inst["x"], inst["y"]
    return (x - GRID, y - 2 * GRID, x + GRID, y + 0.2)


def rect_overlap(a, b, tol=0.01):
    return not (a[2] <= b[0] + tol or b[2] <= a[0] + tol
                or a[3] <= b[1] + tol or b[3] <= a[1] + tol)


def seg_hits_rect(seg, rect, tol=0.01, inset=0.35):
    """判断线段是否真正穿过矩形内部。

    inset 默认内缩 0.35mm：本网导线终止于电气点（恰在 bbox 边界）属正常
    接入，不应算穿越；只有导线深入矩形内部才算。这样既抓得到异网导线贴着
    元件引脚红线穿过的真交叉，又不会把正常接线误报成重叠。
    """
    x1, y1, x2, y2 = seg
    rx0, ry0, rx1, ry1 = rect
    ix0, iy0, ix1, iy1 = rx0 + inset, ry0 + inset, rx1 - inset, ry1 - inset
    if ix0 >= ix1 or iy0 >= iy1:            # 矩形太小（如文字行高）时退回原范围
        ix0, iy0, ix1, iy1 = rx0, ry0, rx1, ry1
    if ix0 < x1 < ix1 and iy0 < y1 < iy1:
        return True
    if ix0 < x2 < ix1 and iy0 < y2 < iy1:
        return True
    if abs(y1 - y2) < tol:                  # 水平线
        return (iy0 < y1 < iy1
                and max(x1, x2) > ix0 + tol and min(x1, x2) < ix1 - tol)
    if abs(x1 - x2) < tol:                  # 竖线
        return (ix0 < x1 < ix1
                and max(y1, y2) > iy0 + tol and min(y1, y2) < iy1 - tol)
    return False


def seg_hits_rect_edge(seg, rect, tol=0.02):
    """含边界的线段-矩形相交判定（用于符号本体/引脚红线：贴边也算穿越）。

    seg_hits_rect 用严格内部，导线正好压在本体边缘（等于引脚红线起点）时
    检不出来；这里边界也算命中，避免“贴着引脚红线走”的交叉漏检。
    """
    x1, y1, x2, y2 = seg
    rx0, ry0, rx1, ry1 = rect
    if abs(y1 - y2) < tol:                      # 水平线
        return (ry0 - tol <= y1 <= ry1 + tol
                and max(x1, x2) >= rx0 - tol and min(x1, x2) <= rx1 + tol)
    if abs(x1 - x2) < tol:                      # 竖线
        return (rx0 - tol <= x1 <= rx1 + tol
                and max(y1, y2) >= ry0 - tol and min(y1, y2) <= ry1 + tol)
    return seg_hits_rect(seg, rect, tol)


def seg_is_label_host(seg, t):
    """标签锚点落在这条导线上（端点或中段）属正常，不算穿字。

    KiCad 里标签必须画在导线上才表示同一网络，因此锚点必在导线上；
    文字以锚点居中，必然与宿主导线接触——这是标准画法不是重叠。
    """
    if t["center"] or not t.get("is_label"):
        return False
    x1, y1, x2, y2 = seg
    ax, ay = t["x"], t["y"]
    tol = 0.02
    if abs(y1 - y2) < 0.01 and abs(ay - y1) < tol and min(x1, x2) - tol <= ax <= max(x1, x2) + tol:
        return True
    if abs(x1 - x2) < 0.01 and abs(ax - x1) < tol and min(y1, y2) - tol <= ay <= max(y1, y2) + tol:
        return True
    return False


def dangling_endpoints(d):
    """找出悬空线头：导线端点只接一段线，且不是引脚/标签/junction/旗标。

    与生成器里的裁剪规则互为独立校验：生成器按同一几何定义裁掉短线头，
    这里则从产物反查，任何图都能查，不依赖某张图的坐标或某个特定网络名。
    """
    from collections import Counter

    def key(p):
        return (round(p[0], 2), round(p[1], 2))

    deg = Counter()
    for x1, y1, x2, y2 in d["wires"]:
        deg[key((x1, y1))] += 1
        deg[key((x2, y2))] += 1

    valid = set()
    for inst in d["insts"]:
        for px, py in d["lib_pins"].get(inst["lib_id"], []):
            x = inst["x"] + (-px if inst.get("mirror") else px)
            y = inst["y"] - py
            valid.add(key((x, y)))
        # PWR_FLAG 的引脚常是隐藏电源脚，实例锚点本身就是电气点
        if inst["lib_id"] == "power:PWR_FLAG":
            valid.add(key((inst["x"], inst["y"])))
    for t in d["texts"]:
        if t.get("is_label"):
            valid.add(key((t["x"], t["y"])))
    valid |= {key(j) for j in d["juncs"]}
    valid |= {p for p, n in deg.items() if n >= 2}

    return sorted(p for p, n in deg.items() if n == 1 and p not in valid)


def check(path) -> dict:
    """返回 {'ok': bool, 'issues': [{type, detail, severity}]}。"""
    d = parse_schematic(path)
    issues = []

    def add(sev, typ, detail):
        issues.append({"type": typ, "detail": detail, "severity": sev})

    boxes = [(t, text_bbox(t)) for t in d["texts"]]
    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            t1, b1 = boxes[i]
            t2, b2 = boxes[j]
            if rect_overlap(b1, b2):
                add("error", "文字互压",
                    f'{t1["kind"]}="{t1["text"]}" 与 {t2["kind"]}="{t2["text"]}"')
                continue
            # 同列（x 接近）的两行属性文字虽未真正重叠，但间距不足 2 格时
            # 视觉上贴成一团（典型：上元件 Value 与下元件 Reference）。
            # 只查 Reference/Value，不碰网络标签（标签贴线是标准画法）。
            if (not t1.get("is_label") and not t2.get("is_label")
                    and abs(t1["x"] - t2["x"]) < 0.6
                    and abs(t1["y"] - t2["y"]) < MIN_TEXT_PITCH):
                add("error", "文字贴靠",
                    f'{t1["kind"]}="{t1["text"]}" 与 {t2["kind"]}="{t2["text"]}" '
                    f'纵向仅差 {abs(t1["y"] - t2["y"]):.2f}mm')

    for t, bb in boxes:
        for w in d["wires"]:
            if seg_is_label_host(w, t):
                continue
            if seg_hits_rect(w, bb):
                if t.get("is_label"):
                    # 网络标签必须贴在自身导线上；密集线束里文字靠近邻线是
                    # KiCad 常见画法，仅提示，不阻断。
                    add("info", "标签邻线接触",
                        f'标签="{t["text"]}" 附近有邻线经过（电气不连）')
                else:
                    add("error", "导线穿文字", f'{t["kind"]}="{t["text"]}" 被导线穿过')
                break

    flag_insts = [i for i in d["insts"] if i["lib_id"] == "power:PWR_FLAG"]
    sym_boxes = []
    for inst in d["insts"]:
        if inst["lib_id"] == "power:PWR_FLAG":
            continue
        sb = symbol_bbox(inst, d["lib_pins"])
        if sb:
            sym_boxes.append((inst["ref"], sb))
    flag_boxes = [(i["ref"], flag_bbox(i)) for i in flag_insts]

    for ref, sb in sym_boxes:
        for w in d["wires"]:
            # 用严格内部判定：本网导线终止于电气点（bbox 边界）不算穿越；
            # 异网导线从 bbox 内部穿过（如贴着 J1 引脚红线走）才算。
            if seg_hits_rect(w, sb):
                add("error", "导线穿符号本体", f"导线穿过元件 {ref} 本体/引脚红线区")
                break
    for ref, fb in flag_boxes:
        for w in d["wires"]:
            if seg_hits_rect(w, fb):
                add("error", "导线穿旗标本体", f"导线穿过 PWR_FLAG {ref} 菱形")
                break
    for k in range(len(flag_boxes)):
        for l in range(k + 1, len(flag_boxes)):
            if rect_overlap(flag_boxes[k][1], flag_boxes[l][1]):
                add("error", "旗标互压",
                    f"{flag_boxes[k][0]} 与 {flag_boxes[l][0]} 菱形重叠")

    jset = set(d["juncs"])
    for i, w1 in enumerate(d["wires"]):
        for w2 in d["wires"][i + 1:]:
            x1, y1, x2, y2 = w1
            u1, v1, u2, v2 = w2
            h1, h2 = abs(y1 - y2) < 0.01, abs(v1 - v2) < 0.01
            if h1 == h2:
                continue
            hx0, hx1, hy = (min(x1, x2), max(x1, x2), y1) if h1 else (min(u1, u2), max(u1, u2), v1)
            vx, vy0, vy1 = (u1, min(v1, v2), max(v1, v2)) if h1 else (x1, min(y1, y2), max(y1, y2))
            if not (hx0 + 0.01 < vx < hx1 - 0.01 and vy0 + 0.01 < hy < vy1 - 0.01):
                continue
            if (vx, hy) in jset or any(abs(vx - jx) < 0.01 and abs(hy - jy) < 0.01 for jx, jy in jset):
                continue
            add("info", "导线十字交叉", f"竖线 x={vx} 与横线 y={hy} 交叉（无圆点，电气不连）")

    tx0, ty0 = d["pw"] - TITLE_W, d["ph"] - TITLE_H
    for t, bb in boxes:
        if bb[2] > tx0 and bb[3] > ty0:
            add("error", "标题栏侵入", f'{t["kind"]}="{t["text"]}" 进入标题栏')
    for w in d["wires"]:
        if max(w[0], w[2]) > tx0 and max(w[1], w[3]) > ty0:
            add("error", "标题栏侵入", f"导线进入标题栏")
            break

    # 悬空线头（通用：从产物几何反查，不针对任何特定图/网络）
    for p in dangling_endpoints(d):
        add("error", "悬空线头",
            f"导线端点 ({p[0]:.2f}, {p[1]:.2f}) 未接引脚/标签/接点/另一段导线")

    errors = [x for x in issues if x["severity"] == "error"]
    return {"ok": not errors, "issues": issues,
            "error_count": len(errors),
            "info_count": len(issues) - len(errors)}
