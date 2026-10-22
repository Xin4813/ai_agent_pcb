"""步骤③：BOM + 需求 → 引脚级网表 JSON。LLM 生成 + 校验-重试闭环。"""
from __future__ import annotations
import json
import re
from .knowledge import lookup_board
from .validator import validate_netlist, ValidationError

SYS_NET = (
    "你是电路设计助手。已知元器件清单(含 KiCad 符号)和用户需求，请给出引脚级连接关系。"
    "只输出 JSON，不要任何解释文字。格式："
    '{"nets":[{"name":"网络名(电源统一用 VCC/GND)","pins":["位号:引脚号"]}]}。'
    "硬性要求：\n"
    "1) 每个引脚号必须真实存在于对应元器件的 KiCad 符号中（下面已给出每个元件的可用引脚）；\n"
    "2) 同一引脚只能出现在一个网络里，绝不能把同一个引脚连到两个网络；\n"
    "3) 电容/电阻只有两个引脚(1 和 2)，分别代表两端，不要重复连接同一引脚；\n"
    "4) 不要输出 JSON 以外的任何内容；\n"
    "5) 网表里的位号(REF)必须严格来自上面给出的 BOM 位号列表，绝不允许新增、改名或臆造"
    "任何位号（例如 BOM 里没有 C4 就绝对不要写 C4，因为 C4 对应的元件根本不存在，写了也无法渲染）。"
)


def _extract_json(text: str):
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        text = m.group(1)
    s, e = text.find("{"), text.rfind("}")
    if s >= 0 and e >= 0:
        text = text[s:e + 1]
    return json.loads(text)


def _build_pin_reference(bom: dict, symbol_lib) -> str:
    """把每个元件「真实可用的引脚号=引脚名」拼成给 LLM 的事实依据文本。"""
    if symbol_lib is None:
        return ""
    lines = []
    for it in bom["items"]:
        ref, sym = it["ref"], it["symbol"]
        try:
            names = symbol_lib.pin_names(sym)
        except Exception:
            names = {}
        if not names:
            continue
        parts = [f"{n}={names[n]}" if names[n] else str(n) for n in sorted(names)]
        lines.append(f"- {ref} [{sym}]: " + ", ".join(parts))
    if not lines:
        return ""
    return "各元件可用引脚（引脚号=引脚名）：\n" + "\n".join(lines) + "\n"


def _check_pins_exist(nl: dict, bom: dict, symbol_lib) -> None:
    """校验网表里引用的引脚是否真实存在于对应符号（真实 LLM 模式的自纠错依据）。"""
    pin_sets = {}
    for it in bom["items"]:
        try:
            pin_sets[it["ref"]] = set(symbol_lib.get_pins(it["symbol"]).keys())
        except Exception:
            pin_sets[it["ref"]] = set()
    for net in nl.get("nets", []):
        for pin in net.get("pins", []):
            ref, _, num = pin.partition(":")
            if ref not in pin_sets:
                continue
            try:
                n = int(num)
            except ValueError:
                continue
            avail = pin_sets[ref]
            if n not in avail:
                sym = next((it["symbol"] for it in bom["items"] if it["ref"] == ref), "?")
                raise ValidationError(
                    f"引脚 {pin} 不存在于符号 {sym}（可用引脚：{sorted(avail)}）"
                )


def _is_power_net(name: str) -> bool:
    """粗略判断一个网络名是否为电源/地网络（用于冲突引脚的归属裁决）。"""
    u = (name or "").upper()
    keys = ("VCC", "GND", "VDD", "VSS", "AVCC", "AREF", "+5V", "+3.3V",
            "+3V3", "3V3", "V+", "V-", "+5", "+3.3")
    return any(k in u for k in keys)


def _self_heal_netlist(nl: dict, bom: dict, valid_ref_set, symbol_lib) -> dict:
    """把校验不过的网表就地修复为可被 KiCad 解析的合法网表（真实 LLM 模式的鲁棒兜底）：
       1) 丢弃 BOM 中不存在的位号（模型臆造元件）；
       2) 若提供符号库，进一步丢弃「位号存在但引脚号不存在」的引脚（模型臆造引脚）；
       3) 同一引脚出现在多个网络时，优先把它保留在电源/地网络，否则保留在首次出现的网络。
    """
    # 1) BOM 不存在的位号直接丢弃（整条连接无效）
    for net in nl.get("nets", []):
        net["pins"] = [p for p in net.get("pins", [])
                       if p.partition(":")[0] in valid_ref_set]
    nl["nets"] = [n for n in nl["nets"] if n.get("pins")]
    # 2) 符号里不存在的引脚号丢弃
    if symbol_lib is not None:
        sym_of = {it["ref"]: it["symbol"] for it in bom["items"]}
        for net in nl["nets"]:
            kept = []
            for p in net["pins"]:
                ref, _, num = p.partition(":")
                sym = sym_of.get(ref)
                if sym is None:
                    continue
                try:
                    n = int(num)
                    avail = symbol_lib.get_pins(sym).keys()
                except Exception:
                    continue
                if n in avail:
                    kept.append(p)
            net["pins"] = kept
        nl["nets"] = [n for n in nl["nets"] if n.get("pins")]
    # 3) 解决「同一引脚出现在多个网络」的冲突
    sym_of = {it["ref"]: it["symbol"] for it in bom["items"]}

    def _pin_name(p):
        if symbol_lib is None:
            return ""
        ref, _, num = p.partition(":")
        sym = sym_of.get(ref)
        if sym is None:
            return ""
        try:
            return symbol_lib.pin_names(sym).get(int(num), "")
        except Exception:
            return ""

    def _is_power_pin(p):
        n = _pin_name(p).upper()
        return any(k in n for k in ("VCC", "GND", "VDD", "VSS", "AVCC", "AREF", "+", "-"))

    pin_occ = {}  # pin -> [(net_index, net_name), ...]
    for i, net in enumerate(nl["nets"]):
        for p in net.get("pins", []):
            pin_occ.setdefault(p, []).append((i, net.get("name", "")))
    conflicts = {p: occ for p, occ in pin_occ.items() if len(occ) > 1}
    if conflicts:
        keep_net = {}
        for p, occ in conflicts.items():
            if _is_power_pin(p):
                # 电源引脚：优先保留在电源/地网络
                chosen = next((idx for idx, name in occ if _is_power_net(name)), occ[0][0])
            else:
                # 信号引脚：优先保留在「非电源」网络，避免被错接到 VCC/GND 造成短路；
                # 若所有冲突网络都是电源网（模型把信号脚同时塞进 VCC/GND），保留首次出现。
                chosen = next((idx for idx, name in occ if not _is_power_net(name)), occ[0][0])
            keep_net[p] = chosen
        new_nets = []
        for i, net in enumerate(nl["nets"]):
            local_seen = set()
            kept = []
            for p in net.get("pins", []):
                if p in conflicts and keep_net.get(p) != i:
                    continue  # 该引脚应保留在别的网络
                if p in local_seen:
                    continue  # 同网内重复，去重
                local_seen.add(p)
                kept.append(p)
            if kept:
                net = dict(net)
                net["pins"] = kept
                new_nets.append(net)
        nl["nets"] = new_nets
    return nl


def generate_netlist(prompt: str, bom: dict, llm=None, mock: bool = False, max_retries: int = 3,
                     symbol_lib=None):
    board = lookup_board(prompt)
    if mock or llm is None:
        if board is None:
            raise RuntimeError("离线模式且未匹配到内置板卡。")
        nl = board["netlist"]
        validate_netlist(nl, bom)   # 内置板卡数据同样要过校验（防数据本身有误）
        return nl, False

    valid_refs = [it["ref"] for it in bom["items"]]
    valid_ref_set = set(valid_refs)
    pin_ref = _build_pin_reference(bom, symbol_lib)
    base_user = f"需求: {prompt}\n元器件清单:\n{json.dumps(bom, ensure_ascii=False)}\n"
    base_user += (f"本设计所有元件位号（网表只能使用以下位号，禁止新增/改名/臆造）："
                  f"{', '.join(valid_refs)}\n")
    if pin_ref:
        base_user += "\n" + pin_ref
    base_user += "\n请给出引脚级连接网表。"

    last = None
    for _ in range(max_retries):
        user = base_user
        if last is not None:
            user += f"\n\n[上一次出错，请修正后重试] {last}"
        nl = None
        try:
            raw = llm.complete(SYS_NET, user)
            nl = _extract_json(raw)
            validate_netlist(nl, bom)
            if symbol_lib is not None:
                _check_pins_exist(nl, bom, symbol_lib)
            return nl, True
        except ValidationError as e:
            # 自纠错回落：校验不通过的网表（无论是因为臆造位号/引脚，还是同一引脚
            # 出现在多个网络等冲突）都先尝试本地修复，修复成功就直接返回，避免整条
            # 流水线崩溃。修复仍失败才把错误回灌给模型重试。
            if nl is not None:
                try:
                    nl = _self_heal_netlist(nl, bom, valid_ref_set, symbol_lib)
                    validate_netlist(nl, bom)
                    if symbol_lib is not None:
                        _check_pins_exist(nl, bom, symbol_lib)
                    return nl, True
                except Exception:
                    last = e
                    continue
            last = e
        except Exception as e:
            last = e
    raise RuntimeError(f"网表生成失败: {last}")
