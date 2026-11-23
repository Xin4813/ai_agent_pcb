"""用当前代码重生成指定 web_session 的 BOM/网表/原理图/ERC。

用法：python tests/regen_session.py <sid> [<sid> ...]

与网页端 _render_and_package 的顺序一致：
  修拓扑（有改动则回写 BOM/网表）→ 重画 → ERC → 视觉 → 安全。
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.circuit_fix import fix_topology  # noqa: E402
from src.kicad_writer import write_schematic  # noqa: E402
from src.safety import check_electrical  # noqa: E402
from src.symbol_lib import SymbolLib  # noqa: E402
from src.visual_check import check as visual_check  # noqa: E402

CLI = Path(r"C:/Users/Gray/AppData/Local/Programs/KiCad/8.0/bin/kicad-cli.exe")
SYMDIR = Path(r"C:/Users/Gray/AppData/Local/Programs/KiCad/8.0/share/kicad/symbols")
SESS = ROOT / "examples" / "output" / "web_sessions"


def run_erc(sch: Path, report: Path) -> int:
    r = subprocess.run(
        [str(CLI), "sch", "erc", str(sch), "--output", str(report)],
        capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=180,
    )
    text = (r.stdout or "") + (r.stderr or "")
    if report.exists():
        text += report.read_text(encoding="utf-8", errors="ignore")
    m = re.search(r"(?:发现|Found)\s*(\d+)\s*(?:条违规项|violations)", text)
    return int(m.group(1)) if m else text.count("ErrType(")


def regen(sid: str, lib: SymbolLib) -> None:
    d = SESS / sid
    bom_p, nl_p = d / "bom.json", d / "netlist.json"
    bom = json.loads(bom_p.read_text(encoding="utf-8"))
    nl = json.loads(nl_p.read_text(encoding="utf-8"))

    fixes = fix_topology(bom, nl, lib)
    if fixes:
        bom_p.write_text(json.dumps(bom, ensure_ascii=False, indent=2), encoding="utf-8")
        nl_p.write_text(json.dumps(nl, ensure_ascii=False, indent=2), encoding="utf-8")

    sch_dir = d / "schematic"
    schs = sorted(sch_dir.glob("*.kicad_sch"))
    if not schs:
        print(f"{sid}: 无原理图，跳过")
        return
    sch = schs[0]
    write_schematic(nl, bom, sch, str(SYMDIR))
    erc = run_erc(sch, sch.with_suffix(".erc.txt"))
    vis = visual_check(sch)
    warnings = check_electrical(bom, nl, lib)
    again = fix_topology(bom, nl, lib)
    print(f"{sid}: fixes={len(fixes)} ERC={erc} 视觉={vis['error_count']} "
          f"安全={len(warnings)} 幂等={len(again)}")
    for f in fixes:
        print(f"    [fix] {f}")
    for it in vis["issues"]:
        if it["severity"] == "error":
            print(f"    [视觉] {it['type']}: {it['detail']}")
    for w in warnings:
        print(f"    [安全] {w}")


def main(argv: list[str]) -> int:
    lib = SymbolLib(str(SYMDIR)) if SYMDIR.exists() else None
    for sid in argv[1:]:
        regen(sid, lib)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
