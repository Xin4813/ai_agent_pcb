"""命令行入口：python -m src.cli "帮我设计一块 Arduino 最小系统板" [--mock]"""
from __future__ import annotations
import argparse
import json
from pathlib import Path

from .config import load_config, kicad_symbols_dir
from .agent import Agent
from .kicad_writer import write_schematic


def _safe_name(s: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in s)[:40] or "design"


def main():
    ap = argparse.ArgumentParser(description="AI Agent 电路板设计")
    ap.add_argument("prompt", help="自然语言需求")
    ap.add_argument("--mock", action="store_true", help="离线模式（用内置设计，无需密钥）")
    ap.add_argument("--out", default=None, help="输出目录")
    ap.add_argument("--symbols-dir", default=None, help="KiCad 符号库目录（覆盖配置）")
    ap.add_argument("-i", "--interactive", action="store_true", help="生成后进入交互修正")
    args = ap.parse_args()

    cfg = None
    if not args.mock:
        cfg = load_config()

    agent = Agent(cfg=cfg, mock=args.mock)
    bom, netlist, used = agent.design(args.prompt)

    out_dir = Path(args.out) if args.out else (
        Path(kicad_symbols_dir(cfg)) if False else Path("examples/output")
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    (out_dir / "bom.json").write_text(json.dumps(bom, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "netlist.json").write_text(json.dumps(netlist, ensure_ascii=False, indent=2), encoding="utf-8")

    sym_dir = args.symbols_dir
    if sym_dir is None and cfg:
        sym_dir = kicad_symbols_dir(cfg)
    if sym_dir is None:
        sym_dir = "C:/Users/Gray/AppData/Local/Programs/KiCad/8.0/share/kicad/symbols"

    sch = write_schematic(netlist, bom, out_dir / "schematic" / f"{_safe_name(bom.get('project','design'))}.kicad_sch", sym_dir)

    print(f"[OK] 数据来源: {'离线知识库' if not used else 'LLM'}")
    print(f"[OK] BOM   -> {out_dir / 'bom.json'}")
    print(f"[OK] 网表  -> {out_dir / 'netlist.json'}")
    print(f"[OK] 原理图-> {sch}")

    if args.interactive:
        print("\n进入交互修正（输入如 '把电容换成 100nF'，输入 exit 退出）:")
        while True:
            try:
                cmd = input("> ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if cmd in ("exit", "quit", ""):
                break
            if agent.revise(cmd):
                (out_dir / "bom.json").write_text(json.dumps(agent.state["bom"], ensure_ascii=False, indent=2), encoding="utf-8")
                (out_dir / "netlist.json").write_text(json.dumps(agent.state["netlist"], ensure_ascii=False, indent=2), encoding="utf-8")
                write_schematic(agent.state["netlist"], agent.state["bom"], sch, sym_dir)
                print(f"[OK] 已按「{cmd}」重新生成")
            else:
                print("[提示] 暂未识别该指令（Demo 仅支持「把电容换成 XX」类指令）")


if __name__ == "__main__":
    main()
