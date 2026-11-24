"""在「元件清单不变」的前提下，用修复后的生成链路重生成某个会话的网表。

用途：当某个历史会话的网表出现 ERC/视觉查不出、但功能上漏接的问题
（如整组 IO 丢失、单脚悬空）时，固定其 BOM 不动，只让 LLM 重新给接线，
并通过 io_coverage_issues 回灌闭环，直到覆盖问题清零或重试耗尽。

用法：
  python tests/regen_netlist_with_coverage.py <sid> [--mock]

真实 LLM 模式会调用网络；--mock 需要该会话能命中内置板卡。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.config import find_kicad_symbols_dir, load_config  # noqa: E402
from src.llm_client import LLMClient  # noqa: E402
from src.netlist_generator import generate_netlist, io_coverage_issues  # noqa: E402
from src.symbol_lib import SymbolLib  # noqa: E402

SESS = ROOT / "examples" / "output" / "web_sessions"


def main(argv: list[str]) -> int:
    if not argv:
        print("用法: python tests/regen_netlist_with_coverage.py <sid> [--mock]")
        return 2
    sid = argv[0]
    use_mock = "--mock" in argv[1:]
    d = SESS / sid
    bom_path, nl_path = d / "bom.json", d / "netlist.json"
    bom = json.loads(bom_path.read_text(encoding="utf-8"))

    symdir = find_kicad_symbols_dir()
    lib = SymbolLib(str(symdir)) if symdir else None

    llm = None
    if not use_mock:
        llm = LLMClient(load_config())

    # prompt 从 BOM 反推：元件清单已固定，这里只给足够的设计意图上下文。
    project = bom.get("project", "")
    desc = "、".join(
        f"{it['ref']}={it.get('desc') or it.get('value', '')}"
        for it in bom.get("items", [])
    )
    prompt = (
        f"{project}，ATmega328P 最小系统板。要求：把全部数字IO(D0-D13/PD0-PD7/PB0-PB5)、"
        f"全部模拟输入(A0-A5/PC0-PC5)、ICSP(MISO/MOSI/SCK/RESET/VCC/GND)、"
        f"电源与地都接到清单中对应的排针；每个网络至少两个引脚，不允许单脚悬空。"
        f"元件及用途：{desc}"
    )

    nl, used = generate_netlist(prompt, bom, llm=llm, mock=use_mock, symbol_lib=lib)
    issues = io_coverage_issues(bom, nl, lib)
    print(f"会话 {sid}: used_llm={used} 网络数={len(nl['nets'])} 覆盖问题={len(issues)}")
    for x in issues:
        print("  !", x)

    if issues:
        print("覆盖自检未清零，未写回。可重试或检查 prompt/LLM。")
        return 1

    nl_path.write_text(json.dumps(nl, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已写回 {nl_path}")
    print("--- 新网表 ---")
    for n in nl["nets"]:
        print(f"  {n['name']:14s}", " ".join(n["pins"]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
