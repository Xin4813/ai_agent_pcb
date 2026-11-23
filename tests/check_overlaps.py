"""原理图视觉重叠检测命令行工具。

用法：
  python tests/check_overlaps.py <sch 文件> [<sch 文件> ...]
  python tests/check_overlaps.py --all      # 扫 examples/output/schematic 下所有 .kicad_sch

核心检测逻辑在 src/visual_check.py（与网页服务端共用）。
退出码：有 error 级重叠返回 1，全部干净返回 0。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.visual_check import check  # noqa: E402


def main(argv):
    args = argv[1:]
    if not args:
        print("用法: python tests/check_overlaps.py <sch...> | --all")
        return 2
    if args == ["--all"]:
        files = sorted(Path("examples/output/schematic").glob("*.kicad_sch"))
    else:
        files = [Path(a) for a in args]

    total_err = 0
    for f in files:
        if not f.exists():
            print(f"[跳过] 不存在: {f}")
            continue
        r = check(f)
        total_err += r["error_count"]
        if r["issues"]:
            print(f"\n{f.name}: {r['error_count']} 处重叠 / {r['info_count']} 处交叉提示")
            for it in r["issues"]:
                tag = "!" if it["severity"] == "error" else "i"
                print(f"  [{tag}] [{it['type']}] {it['detail']}")
        else:
            print(f"\n{f.name}: 干净")
    print(f"\n总计 {total_err} 处重叠")
    return 1 if total_err else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
