"""步骤③：BOM + 需求 → 引脚级网表 JSON。LLM 生成 + 校验-重试闭环。"""
from __future__ import annotations
import copy
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
    "4) 每个网络必须至少包含 2 个引脚，绝不允许只含单个引脚的『孤立网络』；"
    "尤其是排针/接口(如 ICSP、USB、编程口)的每一根信号脚，都必须连到芯片或元件的具体引脚，"
    "不能只写排针自己那一端——例如 ICSP 的 MISO/MOSI/SCK/RESET 应分别接到主控芯片对应的 SPI/复位脚，"
    "ICSP 的 VCC/GND 接到电源/地网络；如果某个功能脚与另一个网络复用同一个芯片脚，"
    "就把排针脚直接加入那个已有网络(用该网络名)，而不是新建一个只有排针的单脚网络；\n"
    "5) 网表里的位号(REF)必须严格来自上面给出的 BOM 位号列表，绝不允许新增、改名或臆造"
    "任何位号（例如 BOM 里没有 C4 就绝对不要写 C4，因为 C4 对应的元件根本不存在，写了也无法渲染）；\n"
    "6) 不要输出 JSON 以外的任何内容。"
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


def _complete_interface_nets(nl: dict, board: dict | None) -> dict:
    """补全「单脚接口网络」：LLM 常把 ICSP/编程口只写排针一端（芯片端漏写），
    导致该网络只有一个引脚，原理图上就是一排没连的针。

    若命中内置板卡知识里的 interface_aliases（如 ICSP_MISO→D12，硬件上二者
    复用同一主控脚），把单脚网络的引脚并入已存在的目标网络，并删除该单脚
    网络。只在目标网络确实存在、且来源网络确实单脚时动作，绝不臆造连接。
    """
    aliases = (board or {}).get("interface_aliases") or {}
    if not aliases:
        return nl
    nets = nl.get("nets", [])
    by_name = {n.get("name"): n for n in nets}
    merged = []
    for n in nets:
        name = n.get("name")
        pins = n.get("pins", [])
        target = aliases.get(name)
        if len(pins) == 1 and target in by_name and by_name[target] is not n:
            dst = by_name[target]
            for p in pins:
                if p not in dst["pins"]:
                    dst["pins"].append(p)
            continue                      # 丢弃这个单脚网络
        merged.append(n)
    nl["nets"] = merged
    return nl


# 端口组引脚名：PC0/PD5/PB3/PA12 这类「端口字母+序号」。
# 用于发现「一整组 IO 都没接」这一 ERC/视觉都查不出的功能漏接。
_PORT_PIN_RE = re.compile(r"^([A-Za-z]{1,2})(\d+)$")


def io_coverage_issues(bom: dict, netlist: dict, symbol_lib=None) -> list[str]:
    """网表功能覆盖自检——专抓 ERC 看不出的「少接一组 IO / 单脚悬空」。

    两条通用规则，不绑定任何具体芯片或项目：

    1. 任一网络只有 1 个引脚 → 必然是一根没连出去的线头（ICSP 只写排针端、
       AREF 悬空等）。它 ERC 为 0、视觉上有名字有标签，但功能上不成立。
    2. 有源芯片某端口组（PC0..PC5 等）**全部**未连接，而同芯片其它端口组
       已经引出 → 几乎必定是生成时漏接了一整组 IO（Arduino 的 A0-A5 就是
       这种情况）。只在「已有别的组引出」时才报，避免对只用到少量 IO 的
       芯片误报。

    注意：芯片未使用的 GPIO 单独成网（如 AREF、某个没引出的 PB 脚）
    属正常设计，不判为错误；只报排针/接口的单脚与整组 IO 漏接。

    返回问题描述列表，供生成阶段回灌 LLM 与 functional_check 共用。
    """
    issues: list[str] = []
    items = bom.get("items", [])
    nets = netlist.get("nets", [])
    if not items or not nets:
        return issues

    by_ref = {it.get("ref", ""): it for it in items}

    def _is_connector(ref: str) -> bool:
        sym = str(by_ref.get(ref, {}).get("symbol", "")).lower()
        return sym.startswith(("connector:", "connector_", "terminalblock"))

    def _is_critical_io(ref: str) -> bool:
        """关键接口排针：ICSP/编程/电源/USB 这类必须逐脚接出的接口。

        普通数字/模拟 IO 扩展排针上偶有未用针脚（如 D11-D13 与 SPI 复用、
        或单纯没引出）属正常设计，不算错误；只有关键接口的单脚悬空
        （如 ICSP 的 MISO/MOSI/SCK 只写排针一端）才是功能错误。
        """
        it = by_ref.get(ref, {})
        txt = " ".join(str(it.get(k, "")) for k in ("desc", "name", "value")).lower()
        return bool(re.search(r"icsp|isp|编程|program|usb|电源|power|调试|swd|jtag", txt))

    # 1) 单脚网络——只报「关键接口单脚」，即 ICSP/电源/USB 的一根信号脚
    #    没接到芯片，这在功能上一定是错的（一排没连出去的针）。
    #    芯片自己未使用的 GPIO（如 AREF、某个 PB 脚单独成网）以及普通 IO
    #    排针上未引出的针脚都属于正常设计，不报——否则会把合法网表误判为
    #    错误、还逼 LLM 反复臆造连接。
    for n in nets:
        pins = n.get("pins", [])
        if len(pins) != 1:
            continue
        ref = pins[0].partition(":")[0]
        if _is_connector(ref) and _is_critical_io(ref):
            issues.append(
                f"网络 {n.get('name')} 只有 {pins[0]} 一个引脚，"
                f"关键接口（{ref}）引脚未接到芯片或其它器件"
            )

    if symbol_lib is None:
        return issues

    connected = {p for n in nets for p in n.get("pins", [])}

    # 2) 芯片整组 IO 漏接
    for it in items:
        sym = it.get("symbol", "")
        sl = sym.lower()
        if not ("mcu_" in sl or "rf_module" in sl or "soc_" in sl):
            continue
        ref = it.get("ref", "")
        try:
            names = symbol_lib.pin_names(sym)
        except Exception:
            continue
        groups: dict[str, list[int]] = {}
        for pin, nm in names.items():
            m = _PORT_PIN_RE.match(str(nm).strip())
            if m:
                groups.setdefault(m.group(1).upper(), []).append(int(pin))
        if len(groups) < 2:
            continue
        wired = {p for pins in groups.values() for p in pins
                 if f"{ref}:{p}" in connected}
        if not wired:
            continue
        for prefix, pins in sorted(groups.items()):
            if any(p in wired for p in pins):
                continue
            issues.append(
                f"{ref}（{it.get('value', '')}）的 {prefix} 端口"
                f"（{len(pins)} 个 IO，引脚 "
                f"{', '.join(str(p) for p in sorted(pins))}）全部未连接，"
                f"而同芯片其它 IO 端口已引出——疑似漏接一整组 IO"
            )
    return issues


def _coverage_feedback(issues: list[str]) -> str:
    """把覆盖自检结果变成给 LLM 的明确修正指令。"""
    lines = "\n".join(f"  - {x}" for x in issues)
    return (
        "\n\n[网表功能覆盖自检发现问题，请修正后重新输出完整网表 JSON，"
        "不要只解释]：\n" + lines
        + "\n修正要求：排针/接口的每根信号脚都必须接到芯片或其它器件，"
        "绝不允许只有排针一端的单脚网络；"
        "芯片按端口成组引出的 IO 必须接到 BOM 中对应功能的排针"
        "（模拟输入 A0-A5/PC0-PC5→模拟IO排针，数字IO→数字IO排针，SPI→编程接口）；"
        "芯片未使用的 GPIO 可以悬空，不要为了凑数臆造连接；"
        "不得新增 BOM 之外的位号，也不得臆造符号里不存在的引脚号。"
    )


def generate_netlist(prompt: str, bom: dict, llm=None, mock: bool = False, max_retries: int = 3,
                     symbol_lib=None, feedback: str = ""):
    board = lookup_board(prompt)
    if board is not None and board.get("pinned"):
        nl = copy.deepcopy(board["netlist"])
        validate_netlist(nl, bom)   # 内置板卡数据同样要过校验（防数据本身有误）
        return nl, False
    if mock or llm is None:
        if board is None:
            raise RuntimeError("离线模式且未匹配到内置板卡。")
        nl = copy.deepcopy(board["netlist"])
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
    if feedback:
        # 规则检查闭环：把上一轮 ERC/DRC 的违规报告作为修正依据回灌给模型，
        # 让它在不改变元件清单的前提下只调整引脚连接关系来消除违规。
        base_user += (
            "\n\n[上一轮规则检查(ERC)报告，请据此只修改引脚连接关系以"
            "消除这些违规，不要增删元件、不要臆造位号/引脚]：\n" + feedback
        )
    base_user += "\n请给出引脚级连接网表。"

    last = None
    cov_feedback = ""     # 上一轮功能覆盖自检发现的问题，回灌给 LLM 修
    best = None           # 覆盖问题最少的一版，重试耗尽时兜底返回
    best_count = None
    for _ in range(max_retries):
        user = base_user
        if cov_feedback:
            user += cov_feedback
        if last is not None:
            user += f"\n\n[上一次出错，请修正后重试] {last}"
        nl = None
        try:
            raw = llm.complete(SYS_NET, user)
            nl = _extract_json(raw)
            validate_netlist(nl, bom)
            # 补全只写排针一端的接口网络（ICSP 等），并入其复用的主网络
            nl = _complete_interface_nets(nl, board)
            validate_netlist(nl, bom)
            if symbol_lib is not None:
                _check_pins_exist(nl, bom, symbol_lib)
            cov = io_coverage_issues(bom, nl, symbol_lib)
            if cov:
                # 结构合法但功能有漏接：记住最好的一版，把问题回灌重试，
                # 而不是让 ERC/视觉全绿、功能却缺一组 IO 的网表直接交付。
                if best_count is None or len(cov) < best_count:
                    best, best_count = nl, len(cov)
                last = None
                cov_feedback = _coverage_feedback(cov)
                continue
            return nl, True
        except ValidationError as e:
            # 自纠错回落：校验不通过的网表（无论是因为臆造位号/引脚，还是同一引脚
            # 出现在多个网络等冲突）都先尝试本地修复，修复成功就直接返回，避免整条
            # 流水线崩溃。修复仍失败才把错误回灌给模型重试。
            if nl is not None:
                try:
                    nl = _self_heal_netlist(nl, bom, valid_ref_set, symbol_lib)
                    validate_netlist(nl, bom)
                    nl = _complete_interface_nets(nl, board)
                    validate_netlist(nl, bom)
                    if symbol_lib is not None:
                        _check_pins_exist(nl, bom, symbol_lib)
                    cov = io_coverage_issues(bom, nl, symbol_lib)
                    if cov:
                        if best_count is None or len(cov) < best_count:
                            best, best_count = nl, len(cov)
                        last = None
                        cov_feedback = _coverage_feedback(cov)
                        continue
                    return nl, True
                except Exception:
                    last = e
                    continue
            last = e
        except Exception as e:
            last = e
    if best is not None:
        # 重试耗尽也不能把整个设计弄崩：返回覆盖问题最少的一版，
        # 由后续 fix_topology / functional_check 继续兜底并在界面上提示。
        return best, True
    raise RuntimeError(f"网表生成失败: {last}")
