"""Agent 编排：串起 ②③④⑤ 步，并维护交互修正状态。"""
from __future__ import annotations
import re
from pathlib import Path
from .bom_generator import generate_bom
from .netlist_generator import generate_netlist
from .config import kicad_symbols_dir
from .symbol_lib import SymbolLib
from .safety import check_electrical

# 元件类型词 → 候选 symbol 前缀/名（用于「把电容换成…」这类按类型批量修改）
_TYPE_HINTS = {
    "电容": ("Device:C",), "电容器": ("Device:C",), "去耦电容": ("Device:C",),
    "电阻": ("Device:R",), "电阻器": ("Device:R",), "上拉电阻": ("Device:R",),
    "发光二极管": ("Device:LED",), "led": ("Device:LED",), "指示灯": ("Device:LED",),
    "二极管": ("Device:D",), "稳压二极管": ("Device:D_Zener",),
    "按键": ("Switch:SW_Push",), "开关": ("Switch:SW_Push",), "按钮": ("Switch:SW_Push",),
    "稳压器": ("Regulator_Linear:",), "稳压芯片": ("Regulator_Linear:",), "ldo": ("Regulator_Linear:",),
    "晶振": ("Device:Crystal",), "晶体": ("Device:Crystal",),
    "排针": ("Connector:Conn_",), "连接器": ("Connector:Conn_",), "接口": ("Connector:Conn_",),
}

# 数值+单位（10k、0.1uF、22µF、330Ω…）——这类新内容应改 value 而非 symbol
_VALUE_RE = re.compile(
    r"^\s*\d+(?:\.\d+)?\s*(?:ohms?|Ω|nF|pF|uF|µF|μF|mF|Hz|k|m|g|r|V|A)\s*\.?\s*$", re.I)


def _sanitize_value(content: str) -> str:
    """清洗新内容，防止把整句指令误写进 value（历史脏数据防御）。

    旧版解析器曾把「把C1换成22uF，把C2换成10uF」的后半句塞进 C1 的 value，
    得到 `22uF，把C2换成22uF` 这类脏值。这里把混入的指令尾巴截掉，只保留
    第一段有效值。
    """
    s = (content or "").strip()
    # 按分隔符切开，若后续片段含换值动词或「把/将 X」结构，视为混入的指令，截断
    parts = re.split(r"[,，、;；]", s)
    kept = [parts[0]]
    for p in parts[1:]:
        if _VERB_RE.search(p) or re.match(r"^\s*(?:把|将)\s", p):
            break
        kept.append(p)
    return re.sub(r"\s+", "", ",".join(kept)).strip(",")


def _parse_revision(text: str):
    """解析「把 X 换成 Y」指令，返回 (目标列表, 新内容)。

    支持的目标：具体位号（U1、C2）或类型词（电容、电阻、稳压器…）。
    支持多个目标（如「R1 R2 改成 10k」）。解析不出返回 (None, None)。
    """
    s = (text or "").strip()
    m = re.search(r"(?:换成|改为|替换成|改成|变更为|改为|换成的是|改成是|换成)[:：]?\s*(.+)$", s)
    if not m:
        m = re.search(r"(?:把|将)\s*(.+?)\s*(?:换成|改为|改成|替换为|变更成)\s*(.+)$", s)
        if not m:
            return None, None
        target_part, new_part = m.group(1).strip(), m.group(2).strip()
    else:
        new_part = m.group(1).strip()
        head = s[:m.start()]
        target_part = re.sub(r"^(?:把|将|请|帮(?:我)?)\s*", "", head).strip()
    if not target_part or not new_part:
        return None, None
    # 目标可能是多个，用逗号/空格/顿号分隔；也可能带"的"后缀（"电容的"）
    parts = [p.strip(" 的") for p in re.split(r"[,，、\s]+", target_part) if p.strip(" 的")]
    return parts, new_part


# 子句切分符（逗号/分号/顿号）——注意顿号既是「多目标分隔符」也可能是「子句分隔符」，
# 靠「片段是否含换值动词」来区分：不含动词的片段并入后面含动词的片段作为目标。
_CLAUSE_SPLIT_RE = re.compile(r"[,，、;；]")
_VERB_RE = re.compile(r"换成|改为|替换成|改成|变更为|替换为|设置为|设成|调成")


def _parse_revisions(text: str):
    """把一条指令解析成多个 (目标列表, 新内容) 子句。

    同时支持两种写法（也可混用）：
      • 多目标共享一个值：「把 C1、C2 换成 20uF」
      • 多条独立子句：「把 C1 换成 22uF，把 C2 换成 10uF」
        （分隔符可用 逗号/顿号/分号，第二句可省略「把」）
    全部解析不出返回 None。
    """
    s = (text or "").strip()
    if not s:
        return None
    pieces = [p.strip() for p in _CLAUSE_SPLIT_RE.split(s) if p.strip()]
    clauses, buf = [], ""
    for p in pieces:
        buf = (buf + "，" + p) if buf else p
        if _VERB_RE.search(p):
            clauses.append(buf)
            buf = ""
    if buf:
        clauses.append(buf)
    out = []
    for cl in clauses:
        targets, new = _parse_revision(cl)
        if targets and new:
            out.append((targets, new))
    return out or None


class Agent:
    def __init__(self, cfg: dict | None = None, mock: bool = False):
        self.cfg = cfg or {}
        self.mock = mock
        self.llm = None
        self.symbol_lib = None
        if not mock and cfg:
            try:
                from .llm_client import LLMClient
                self.llm = LLMClient(cfg)
            except Exception:
                self.llm = None
        # 符号库是本地 KiCad 资源，与 LLM/密钥无关；mock 模式也要加载，
        # 这样离线演示也能「更换型号」（find_exact_symbol 需要它）。
        for d in filter(None, [
            (kicad_symbols_dir(cfg) if cfg else None),
            Path(r"C:/Users/Gray/AppData/Local/Programs/KiCad/8.0/share/kicad/symbols"),
        ]):
            try:
                if Path(d).exists():
                    self.symbol_lib = SymbolLib(d)
                    break
            except Exception:
                self.symbol_lib = None
        self.state = {"bom": None, "netlist": None, "history": [], "warnings": []}

    def design(self, prompt: str):
        """步骤②③④：需求 → BOM → 网表。返回 (bom, netlist, used_llm)。"""
        bom, used = generate_bom(prompt, self.llm, mock=self.mock, symbol_lib=self.symbol_lib)
        netlist, _ = generate_netlist(prompt, bom, self.llm, mock=self.mock, symbol_lib=self.symbol_lib)
        # 初次设计：修正 ERC 检查不到的功能性拓扑错误（5V 直灌/LED 无限流/
        # USB 反接/晶振悬空/BOOT0 未拉）。只修检测到的错误，并保留修正说明。
        from .circuit_fix import fix_topology
        topology_fixes = fix_topology(bom, netlist, self.symbol_lib)
        self.state["topology_fixes"] = topology_fixes
        self.state["bom"] = bom
        self.state["netlist"] = netlist
        self.state["prompt"] = prompt
        self.state["history"].append(prompt)
        self._refresh_warnings()
        return bom, netlist, used

    def _refresh_warnings(self, extra: list[str] | None = None):
        """重算电气安全警告（ERC 不做电气参数检查，这里补一层启发式）。"""
        w = check_electrical(self.state.get("bom"), self.state.get("netlist"),
                             self.symbol_lib)
        if extra:
            w = extra + w
        self.state["warnings"] = w

    def revise(self, instruction: str) -> bool:
        """步骤⑤：按自然语言指令更换元件（改标称值或型号），并重算网表。

        支持的指令（中英文混排均可）：
          • 按位号：  「把 C1 换成 22uF」「R3 改成 1k」「U2 换成 AMS1117-5.0」
          • 按类型：  「把电容换成 100nF」「稳压器换成 AMS1117-5.0」「LED 改成绿色」
          • 多目标：  「C1 C2 换成 0.1uF」

        处理逻辑：
          1) 解析目标与新内容；目标可为具体位号或元件类型词。
          2) 新内容若是本机符号库真实存在的型号 → 改 symbol + value（换型号）；
             若是数值+单位或普通文字 → 只改 value（换标称值/颜色）。
          3) 换型号后引脚号可能变化：用 _self_heal_netlist 丢弃新符号里不存在的
             引脚，保证网表仍合法、ERC 仍可为 0。
          4) 记录本轮改动说明到 state["last_revision"]，供网页展示。

        返回 True 表示成功修改。保持 bool 返回以兼容 CLI 的 `if agent.revise(cmd)`。
        """
        if not self.state.get("bom"):
            self.state["last_revision"] = "还没有设计结果，无法修改"
            return False
        revisions = _parse_revisions(instruction)
        if not revisions:
            self.state["last_revision"] = "没识别出来，试试「把 C1 换成 22uF」或「稳压器换成 AMS1117-5.0」"
            return False

        items = self.state["bom"]["items"]
        by_ref = {it["ref"]: it for it in items}

        changes = []
        any_symbol_change = False
        unmatched = []
        for targets, new_content in revisions:
            # 目标 → 具体位号集合
            hit_refs = []
            for t in targets:
                if t in by_ref:                   # 精确位号
                    hit_refs.append(t)
                    continue
                tl = t.lower()
                hints = None
                for kw, prefixes in _TYPE_HINTS.items():
                    if kw.lower() in tl or tl in kw.lower():
                        hints = prefixes
                        break
                if hints:
                    for it in items:
                        sym = it.get("symbol", "")
                        if any(sym.startswith(p) for p in hints) and it["ref"] not in hit_refs:
                            hit_refs.append(it["ref"])
            if not hit_refs:
                unmatched.extend(targets)
                continue

            # 新内容：真实型号 → 换 symbol；否则只改 value
            new_symbol = None
            if self.symbol_lib is not None and not _VALUE_RE.match(new_content):
                new_symbol = self.symbol_lib.find_exact_symbol(new_content)
            clean_value = _sanitize_value(new_content)

            for ref in hit_refs:
                it = by_ref[ref]
                old = f'{it.get("value", "")}'
                it["value"] = clean_value
                if new_symbol:
                    any_symbol_change = True
                    old_sym = it.get("symbol", "")
                    it["symbol"] = new_symbol
                    changes.append(f"{ref}: {old_sym} → {new_symbol}，{old} → {clean_value}")
                else:
                    changes.append(f"{ref}: {old} → {clean_value}")

        if not changes:
            self.state["last_revision"] = (
                f"找不到元件「{', '.join(unmatched)}」，当前位号：{', '.join(by_ref)}"
                if unmatched else "没识别出来，试试「把 C1 换成 22uF」"
            )
            return False

        # 换型号后过滤掉新符号不存在的引脚，保持网表合法
        dropped: list[str] = []
        nl = self.state.get("netlist")
        if any_symbol_change and nl is not None and self.symbol_lib is not None:
            from .netlist_generator import _self_heal_netlist
            before = {p for n in nl.get("nets", []) for p in n.get("pins", [])}
            healed = _self_heal_netlist(nl, self.state["bom"], set(by_ref), self.symbol_lib)
            after = {p for n in healed.get("nets", []) for p in n.get("pins", [])}
            dropped = sorted(before - after)
            self.state["netlist"] = healed

        notes = "；".join(changes)
        if unmatched:
            notes += f"（未找到：{', '.join(unmatched)}）"
        self.state["history"].append(instruction)
        self.state["last_revision"] = notes

        # 电气安全提示：引脚被丢弃 + 电压/极性启发式检查
        extra = []
        if dropped:
            extra.append(
                f"⚠️ 换型号后已丢弃 {len(dropped)} 条连线（新符号无对应引脚：{', '.join(dropped)}），"
                f"原电路连接可能已被改变，请人工核对。"
            )
            notes += extra[0]
            self.state["last_revision"] = notes
        self._refresh_warnings(extra)
        return True

    def revise_erc(self, erc_text: str):
        """规则检查闭环（步骤⑤进阶）：把 ERC 违规报告回灌给 LLM，让它在不增删
        元件的前提下只调整引脚连接关系来消除违规，并重生成原理图所需的网表。

        仅真实 LLM 模式可用（mock 模式依赖内置板卡、无法按任意反馈重算）。
        返回 (netlist, 是否成功)；失败返回 (None, False)。
        """
        if self.mock or self.llm is None or not self.state.get("bom"):
            return None, False
        try:
            prompt = self.state.get("prompt", "")
            nl, _ = generate_netlist(
                prompt, self.state["bom"], self.llm, mock=False,
                symbol_lib=self.symbol_lib, feedback=erc_text,
            )
            self.state["netlist"] = nl
            self.state["history"].append("[ERC闭环] 依据违规报告修订网表")
            return nl, True
        except Exception:
            return None, False
