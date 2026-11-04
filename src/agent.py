"""Agent 编排：串起 ②③④⑤ 步，并维护交互修正状态。"""
from __future__ import annotations
import re
from .bom_generator import generate_bom
from .netlist_generator import generate_netlist
from .config import kicad_symbols_dir
from .symbol_lib import SymbolLib


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
            try:
                d = kicad_symbols_dir(cfg)
                if d.exists():
                    self.symbol_lib = SymbolLib(d)
            except Exception:
                self.symbol_lib = None
        self.state = {"bom": None, "netlist": None, "history": []}

    def design(self, prompt: str):
        """步骤②③④：需求 → BOM → 网表。返回 (bom, netlist, used_llm)。"""
        bom, used = generate_bom(prompt, self.llm, mock=self.mock, symbol_lib=self.symbol_lib)
        netlist, _ = generate_netlist(prompt, bom, self.llm, mock=self.mock, symbol_lib=self.symbol_lib)
        self.state["bom"] = bom
        self.state["netlist"] = netlist
        self.state["prompt"] = prompt
        self.state["history"].append(prompt)
        return bom, netlist, used

    def revise(self, instruction: str) -> bool:
        """步骤⑤（演示版）：解析“把电容换成 XX”这类指令，改 BOM 标称值后重新生成网表。

        注意：改电容标称值不改变引脚连接关系（网表结构不变），因此这里只更新
        BOM 中的 value；若需要按原始需求重算网表，则复用最初的需求 prompt，
        而不是用修订指令去查板卡（否则离线模式会因匹配不到板卡而报错）。
        """
        m = re.search(r"(\d+(?:\.\d+)?\s?(?:nF|pF|uF|µF|μF))", instruction, re.I)
        if not m or not self.state["bom"]:
            return False
        val = m.group(1).replace(" ", "")
        for it in self.state["bom"]["items"]:
            if it["symbol"] == "Device:C":
                it["value"] = val
        # 网表结构不变，仍以原始需求重算以保持一致（离线模式也能命中内置板卡）
        prompt = self.state.get("prompt", instruction)
        self.state["netlist"], _ = generate_netlist(
            prompt, self.state["bom"], self.llm, mock=self.mock, symbol_lib=self.symbol_lib
        )
        self.state["history"].append(instruction)
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
