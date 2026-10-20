"""Agent 编排：串起 ②③④⑤ 步，并维护交互修正状态。"""
from __future__ import annotations
import re
from .bom_generator import generate_bom
from .netlist_generator import generate_netlist


class Agent:
    def __init__(self, cfg: dict | None = None, mock: bool = False):
        self.cfg = cfg or {}
        self.mock = mock
        self.llm = None
        if not mock and cfg:
            try:
                from .llm_client import LLMClient
                self.llm = LLMClient(cfg)
            except Exception:
                self.llm = None
        self.state = {"bom": None, "netlist": None, "history": []}

    def design(self, prompt: str):
        """步骤②③④：需求 → BOM → 网表。返回 (bom, netlist, used_llm)。"""
        bom, used = generate_bom(prompt, self.llm, mock=self.mock)
        netlist, _ = generate_netlist(prompt, bom, self.llm, mock=self.mock)
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
            prompt, self.state["bom"], self.llm, mock=self.mock
        )
        self.state["history"].append(instruction)
        return True
