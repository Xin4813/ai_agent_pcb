"""步骤②：自然语言 → BOM 元器件清单。支持 LLM 模式与离线知识库回退。"""
from __future__ import annotations
import json
import re
from .knowledge import lookup_board
from .validator import validate_bom, ValidationError

# 常用元件 → 候选 lib_id（按优先级，运行时只保留本机真实存在的）。
# 这样即使模型没见过 KiCad 库，也能拿到一份“保底可用”的符号清单。
_CATALOG_BASE = [
    "Device:C", "Device:R", "Device:D", "Device:D_Zener", "Device:LED",
    "Device:Crystal", "Device:Battery_Cell", "Device:Fuse", "Device:Fuse_Small",
    "Switch:SW_Push", "Switch:SW_SPDT",
    "Regulator_Linear:LM1117T-5.0", "Regulator_Linear:LM1117T-3.3",
    "Regulator_Linear:LM7805T", "Regulator_Linear:LM7905T",
    "MCU_Microchip_ATmega:ATmega328P-P", "MCU_Microchip_ATmega:ATmega328P-AU",
    "MCU_Microchip_ATmega:ATmega328P-M", "MCU_Microchip_ATmega:ATmega328P-MM",
]
# 排针/排座：动态拼接 01x0N / 02xNN 并只保留存在的。
_CATALOG_HEADERS = [
    *(f"Connector:Conn_01x0{n}_Pin" for n in (1, 2, 3, 4, 5, 6, 7, 8)),
    *(f"Connector:Conn_01x{n}_Pin" for n in (10, 12, 14, 16, 20)),
    *(f"Connector:Conn_02x{c}_Pin" for c in (2, 3, 4, 5, 6, 7, 8, 10, 13, 15, 20)),
    *(f"Connector:Conn_02x{c}_Socket" for c in (3, 5, 7, 10, 13, 15, 20)),
]
# 需求关键词（中文/英文）→ 在符号库里召回的子串，用于扩充目录，使提示词随需求自适应。
_KEYWORD_RECALL = {
    "电阻": "Device:R", "电容": "Device:C", "二极管": "Device:D",
    "稳压": "Regulator", "电源": "Regulator", "降压": "Regulator", "升压": "Regulator",
    "发光": "LED", "灯": "LED", "晶振": "Crystal", "振荡": "Crystal", "时钟": "Crystal",
    "按键": "SW_Push", "按钮": "SW_Push", "开关": "SW_", "拨码": "SW_",
    "电池": "Battery", "保险": "Fuse", "熔断": "Fuse",
    "单片机": "ATmega", "微控制": "MCU", "MCU": "MCU", "处理器": "MCU",
    "接插件": "Conn_", "排针": "Conn_", "插座": "Conn_", "端子": "Terminal",
    "传感器": "Sensor", "电机": "Motor", "继电器": "Relay", "晶振": "Oscillator",
    "三极管": "Transistor", "晶体管": "Transistor", "场效应": "FET", "MOS": "FET",
    "运放": "Opamp", "放大": "Opamp", "比较器": "Comparator",
}

SYS_BOM = (
    "你是电子工程师助手。根据用户自然语言需求，列出电路板所需的元器件清单。"
    "只输出 JSON，不要任何解释文字。格式："
    '{"project":"项目名","items":[{"ref":"位号如U1","name":"名称","value":"标称值",'
    '"qty":数量,"symbol":"KiCad 符号库ID 如 Device:C","footprint":"封装(可空)","desc":"说明(可空)"}]}'
    "\n【硬性要求】symbol 必须是下面“可用符号目录”里真实存在的 lib_id 之一"
    "（必须严格一致，包括库前缀与符号名，不要自己编造或用近似名）："
    "\n\n【位号规则】每个 ref 只能对应一个元件；若有多个同型号元件，必须写成独立条目"
    "（如 C1、C2 两条），绝不要把多个位号塞进同一个 ref 字符串（例如写成 \"C1,C2\"）。"
)


def _build_catalog(symbol_lib) -> list:
    """构建一份本机真实存在的、常用且随需求自适应的符号目录，注入提示词。"""
    if symbol_lib is None:
        return []
    present = set(symbol_lib.list_symbols())
    out = []
    for c in _CATALOG_BASE + _CATALOG_HEADERS:
        if c in present and c not in out:
            out.append(c)
    return out


def _recall_by_keyword(prompt: str, symbol_lib) -> list:
    """按需求里的关键词，从符号库召回相关 lib_id，扩充目录。"""
    if symbol_lib is None or not prompt:
        return []
    present = set(symbol_lib.list_symbols())
    out = []
    lower = prompt.lower()
    for kw, sub in _KEYWORD_RECALL.items():
        if kw.lower() in lower:
            # 取子串匹配、且属于常见库的若干候选
            hits = [s for s in sorted(present) if sub.lower() in s.lower()]
            # 过滤掉明显不相关的大库（如 Battery_Management 芯片），优先通用库
            pref = [s for s in hits if s.split(":")[0] in
                    ("Device", "Switch", "Connector", "Regulator_Linear",
                     "MCU_Microchip_ATmega", "LED", "Opto", "Transistor_FET",
                     "Transistor_BJT", "Opamp", "Relay", "Sensor")]
            for s in (pref or hits)[:6]:
                if s not in out:
                    out.append(s)
    return out


def _extract_json(text: str):
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        text = m.group(1)
    s, e = text.find("{"), text.rfind("}")
    if s >= 0 and e >= 0:
        text = text[s:e + 1]
    return json.loads(text)


def _expand_bom_refs(bom: dict) -> dict:
    """把 BOM 中合并写的位号（如 ref="C1,C2"、"C1 C2"、"C1，C2"）拆成独立条目。

    LLM 常把多个同型号元件的位号塞进一个 ref 字符串，而下游的网表校验
    (validator.validate_netlist) 与原理图渲染 (kicad_writer) 都只认“一个 ref 对应一个
    元件”，必须从源头展平，否则网表引用 C1 时在校验里找不到（只有 "C1,C2" 这个字符串）。
    """
    new_items = []
    for it in bom.get("items", []):
        ref = str(it.get("ref", "")).strip()
        parts = re.split(r"[,，;；\s]+", ref)
        parts = [p for p in parts if p]
        if len(parts) <= 1:
            new_items.append(it)
            continue
        try:
            base_qty = int(it.get("qty", len(parts)))
        except (ValueError, TypeError):
            base_qty = len(parts)
        per = max(1, base_qty // len(parts)) if base_qty >= len(parts) else 1
        for p in parts:
            ni = dict(it)               # 浅拷贝其余字段（symbol/value/footprint/desc）
            ni["ref"] = p
            ni["qty"] = per
            new_items.append(ni)
    bom["items"] = new_items
    return bom


def _check_symbols(bom: dict, symbol_lib) -> list:
    """校验 BOM 中每个 symbol 是否在 KiCad 符号库里真实存在。

    若存在「高度可信」的纠正项（库名一致或符号名一致/子序列匹配），
    则直接把错误的 symbol 改写为真实符号（自纠错），并返回改写说明；
    否则抛出 ValidationError，把可用目录与建议一并回灌给模型重试。
    """
    notes = []
    for it in bom["items"]:
        sym = it.get("symbol", "")
        try:
            defs = symbol_lib.get_symbol_definitions(sym)
        except Exception:
            defs = []
        if defs:
            continue
        sug = symbol_lib.suggest_lib_id(sym)
        if sug:
            it["symbol"] = sug
            notes.append(f"{sym} 不存在，已自动改用真实符号 {sug}")
        else:
            raise ValidationError(
                f"symbol 在 KiCad 符号库中找不到：{sym}。"
                f"请只使用“可用符号目录”里列出的 lib_id。"
            )
    return notes


def generate_bom(prompt: str, llm=None, mock: bool = False, max_retries: int = 3,
                 symbol_lib=None):
    board = lookup_board(prompt)
    if mock or llm is None:
        if board is None:
            raise RuntimeError("离线模式且未匹配到内置板卡；请提供 LLM 密钥以生成。")
        return _expand_bom_refs(board["bom"]), False

    # 可用符号目录（本机校验过的真实 lib_id）
    catalog = _build_catalog(symbol_lib)
    recalled = _recall_by_keyword(prompt, symbol_lib)
    catalog_block = "\n可用符号目录（从中选择，必须严格一致）：\n- " + "\n- ".join(catalog)
    if recalled:
        catalog_block += "\n（按需求召回的更多可用符号）\n- " + "\n- ".join(recalled)
    sys_prompt = SYS_BOM + catalog_block + "\n"

    last = None
    for _ in range(max_retries):
        user = prompt
        if last is not None:
            user += f"\n\n[上一次出错，请修正后重试] {last}"
        try:
            raw = llm.complete(sys_prompt, user)
            bom = _extract_json(raw)
            bom = _expand_bom_refs(bom)          # 展平合并位号（如 "C1,C2" → C1、C2）
            validate_bom(bom)
            if symbol_lib is not None:
                notes = _check_symbols(bom, symbol_lib)
                if notes:
                    # 已自动把不存在的 symbol 改写为真实符号，BOM 合法，直接接受返回
                    return bom, True
            return bom, True
        except Exception as e:
            last = e
    raise RuntimeError(f"BOM 生成失败: {last}")
