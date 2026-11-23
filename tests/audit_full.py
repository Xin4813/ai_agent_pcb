"""全量回归审计：对 web_sessions 下每个会话检查四个维度。

用法：
  python tests/audit_full.py                 # 全部会话
  python tests/audit_full.py 171cbf92 ...    # 只查指定会话

检查项：
  1. fix_topology 幂等（已落盘会话再跑应返回 0 条修正）
  2. safety.check_electrical 安全告警数
  3. visual_check 视觉 error 数
  4. kicad-cli sch erc 违规数
退出码：任一维度非 0 则为 1。
"""
from __future__ import annotations

import copy
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.circuit_fix import fix_topology  # noqa: E402
from src.safety import check_electrical  # noqa: E402
from src.symbol_lib import SymbolLib  # noqa: E402
from src.visual_check import check as visual_check  # noqa: E402

CLI = Path(r"C:/Users/Gray/AppData/Local/Programs/KiCad/8.0/bin/kicad-cli.exe")
SYMDIR = Path(r"C:/Users/Gray/AppData/Local/Programs/KiCad/8.0/share/kicad/symbols")
SESS = ROOT / "examples" / "output" / "web_sessions"


def erc_count(sch: Path, report: Path) -> int:
    if not CLI.exists():
        return -1
    try:
        r = subprocess.run(
            [str(CLI), "sch", "erc", str(sch), "--output", str(report)],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=180,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"    ERC 执行失败: {exc}")
        return -1
    text = (r.stdout or "") + (r.stderr or "")
    if report.exists():
        text += report.read_text(encoding="utf-8", errors="ignore")
    m = re.search(r"(?:发现|Found)\s*(\d+)\s*(?:条违规项|violations)", text)
    return int(m.group(1)) if m else text.count("ErrType(")


def main(argv: list[str]) -> int:
    lib = SymbolLib(str(SYMDIR)) if SYMDIR.exists() else None
    only = set(argv[1:])
    print(f"符号库: {'已加载' if lib else '未加载'}  KiCad CLI: {'可用' if CLI.exists() else '不可用'}")
    print(f"{'会话':<12} {'fix':>4} {'安全':>4} {'视觉':>4} {'ERC':>4}")
    print("-" * 36)
    bad = 0
    sessions = [d for d in sorted(SESS.iterdir()) if d.is_dir()]
    for d in sessions:
        if only and d.name not in only:
            continue
        bom_p, nl_p = d / "bom.json", d / "netlist.json"
        if not (bom_p.exists() and nl_p.exists()):
            continue
        bom = json.loads(bom_p.read_text(encoding="utf-8"))
        nl = json.loads(nl_p.read_text(encoding="utf-8"))

        fixes = fix_topology(copy.deepcopy(bom), copy.deepcopy(nl), lib)
        warns = check_electrical(bom, nl, lib)

        sch_dir = d / "schematic"
        schs = sorted(sch_dir.glob("*.kicad_sch")) if sch_dir.exists() else []
        verr = sum(visual_check(s)["error_count"] for s in schs)
        erc = erc_count(schs[0], d / "_audit.erc.txt") if schs else -1

        flag = "" if (not fixes and not warns and not verr and erc == 0) else "  <== 问题"
        print(f"{d.name:<12} {len(fixes):>4} {len(warns):>4} {verr:>4} {erc:>4}{flag}")
        if fixes:
            for f in fixes:
                print(f"    [fix] {f}")
        for w in warns:
            print(f"    [安全] {w}")
        if verr or erc not in (0,):
            for s in schs:
                r = visual_check(s)
                for it in r["issues"]:
                    if it["severity"] == "error":
                        print(f"    [视觉] {it['type']}: {it['detail']}")
        if fixes or warns or verr or erc != 0:
            bad += 1
    print("-" * 36)
    print(f"合计 {bad} 个会话存在问题")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
