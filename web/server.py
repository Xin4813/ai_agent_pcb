"""PCB Agent 网页后端（零依赖，仅用 Python 标准库）。

启动：  python web/server.py            （默认 http://127.0.0.1:8000）
        python web/server.py 9000       （自定义端口）

它复用与命令行完全一致的流水线：Agent.design() → write_schematic() 生成原理图，
再调用本机 kicad-cli 跑 ERC（电气规则检查）+ 导出 SVG 预览；可选「规则检查闭环」
把 ERC 违规报告回灌给 LLM 自动修订并重生成，直到 0 违规或达到上限。

注意：后端进程需要能访问 config/config.toml 里的 LLM 端点与 KiCad，因此请在本机
（即装有 Python 3.11 + KiCad 8 的那台 Windows 机器）运行，浏览器打开 localhost 即可。
"""
from __future__ import annotations
import sys
import os
import json
import subprocess
import re
import threading
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config import load_config, kicad_symbols_dir          # noqa: E402
from src.agent import Agent                                    # noqa: E402
from src.kicad_writer import write_schematic                    # noqa: E402

INDEX = Path(__file__).resolve().parent / "index.html"


def _kicad_cli(cfg) -> str | None:
    """推导 kicad-cli 可执行文件路径；找不到返回 None（调用方优雅降级）。"""
    if cfg:
        inst = cfg.get("kicad", {}).get("install_dir", "")
        if inst:
            cand = Path(inst) / "bin" / "kicad-cli.exe"
            if cand.exists():
                return str(cand)
            cand = Path(inst) / "bin" / "kicad-cli"
            if cand.exists():
                return str(cand)
    for name in ("kicad-cli", "kicad-cli.exe"):
        if os.system(f"where {name} >nul 2>nul") == 0 or os.system(f"command -v {name} >/dev/null 2>&1") == 0:
            return name
    return None


def _run(cmd) -> dict:
    try:
        # 显式用 UTF-8 读取管道输出：中文 Windows 默认 locale 编码是 GBK，
        # 而 kicad-cli 输出为 UTF-8，二者不一致会在 capture_output 的后台读线程里
        # 抛 UnicodeDecodeError（'gbk' codec can't decode ...）。errors="replace"
        # 兜底，避免个别非法字节导致整条流水线崩溃。
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=180)
        return {"ok": True, "rc": r.returncode, "out": r.stdout, "err": r.stderr}
    except FileNotFoundError:
        return {"ok": False, "rc": -1, "out": "", "err": "kicad-cli 未找到"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "rc": -2, "out": "", "err": "kicad-cli 执行超时"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "rc": -3, "out": "", "err": str(e)}


def run_erc(sch_path: Path, cli) -> dict:
    """跑 ERC，返回 {ok, violations, report, error}。"""
    if not cli:
        return {"ok": False, "violations": None, "report": "", "error": "kicad-cli 不可用"}
    rep = sch_path.with_suffix(".erc.txt")
    res = _run([cli, "sch", "erc", str(sch_path), "--output", str(rep)])
    if not res["ok"]:
        return {"ok": False, "violations": None, "report": "", "error": res["err"]}
    report = ""
    if rep.exists():
        try:
            report = rep.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            report = ""
    # 违规数：优先从 stdout 提取「发现 N 条违规项 / Found N violations」，
    # 否则统计报告里的 ErrType 行数。
    m = re.search(r"(?:发现|Found)\s*(\d+)\s*(?:条违规项|violations)", res["out"] + report)
    if m:
        violations = int(m.group(1))
    else:
        violations = report.count("ErrType(")
    return {"ok": True, "violations": violations, "report": report, "error": ""}


def export_svg(sch_path: Path, cli) -> str | None:
    """导出原理图为 SVG 文本（用于浏览器内预览）；失败返回 None。"""
    if not cli:
        return None
    svg = sch_path.with_suffix(".svg")
    res = _run([cli, "sch", "export", "svg", "--output", str(svg), str(sch_path)])
    if not res["ok"] or not svg.exists():
        return None
    try:
        return svg.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return None


def run_design(prompt: str, mock: bool, autofix: bool, max_fix: int = 2) -> dict:
    cfg = None if mock else load_config()
    cli = _kicad_cli(cfg)

    agent = Agent(cfg=cfg, mock=mock)
    bom, netlist, used = agent.design(prompt)

    out_dir = Path(os.environ.get("PCB_WEB_OUT") or (ROOT / "examples" / "output"))
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "bom.json").write_text(json.dumps(bom, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "netlist.json").write_text(json.dumps(netlist, ensure_ascii=False, indent=2), encoding="utf-8")

    if cfg and kicad_symbols_dir(cfg).exists():
        sym_dir = kicad_symbols_dir(cfg)
    else:
        sym_dir = r"C:/Users/Gray/AppData/Local/Programs/KiCad/8.0/share/kicad/symbols"

    name = "".join(c if c.isalnum() else "_" for c in (bom.get("project") or prompt))[:40] or "design"
    sch = write_schematic(netlist, bom, out_dir / "schematic" / f"{name}.kicad_sch", sym_dir)

    loop = []
    last_erc = None
    iters = (max_fix + 1) if (autofix and not mock) else 1
    for i in range(iters):
        erc = run_erc(sch, cli)
        last_erc = erc
        loop.append({"iter": i, "erc": erc})
        if not autofix or mock or i >= max_fix:
            break
        if not erc["ok"] or erc["violations"] is None:
            loop.append({"iter": i, "note": "ERC 不可用，停止闭环"})
            break
        if erc["violations"] == 0:
            break
        nl, ok = agent.revise_erc(erc["report"])
        if not ok or nl is None:
            loop.append({"iter": i, "note": "LLM 修订失败，停止闭环"})
            break
        (out_dir / "netlist.json").write_text(json.dumps(nl, ensure_ascii=False, indent=2), encoding="utf-8")
        sch = write_schematic(nl, bom, sch, sym_dir)

    svg = export_svg(sch, cli)
    return {
        "ok": True,
        "source": "LLM" if used else "离线知识库",
        "prompt": prompt,
        "bom": bom,
        "netlist": netlist,
        "sch": str(sch),
        "svg": svg,
        "erc": last_erc,
        "loop": loop,
        "kicad_cli": cli or "",
    }


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body: bytes, ctype="application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self):
        p = urlparse(self.path).path
        if p in ("/", "/index.html"):
            if INDEX.exists():
                self._send(200, INDEX.read_bytes(), "text/html; charset=utf-8")
            else:
                self._send(404, b"index.html not found", "text/plain; charset=utf-8")
            return
        if p == "/favicon.ico":
            self._send(204, b"")
            return
        self._send(404, b"not found", "text/plain; charset=utf-8")

    def do_POST(self):
        if urlparse(self.path).path != "/api/design":
            self._send(404, json.dumps({"ok": False, "error": "unknown endpoint"}).encode("utf-8"))
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            data = json.loads(raw.decode("utf-8") or "{}")
        except Exception as e:  # noqa: BLE001
            self._send(400, json.dumps({"ok": False, "error": f"请求解析失败: {e}"}).encode("utf-8"))
            return

        prompt = (data.get("prompt") or "").strip()
        mock = bool(data.get("mock", False))
        autofix = bool(data.get("autofix", True))
        if not prompt:
            self._send(400, json.dumps({"ok": False, "error": "需求不能为空"}).encode("utf-8"))
            return

        # 在子线程里跑（避免阻塞服务器），用事件把结果交回。
        result = {}
        ev = threading.Event()

        def worker():
            try:
                result["data"] = run_design(prompt, mock, autofix)
            except Exception as e:  # noqa: BLE001
                result["data"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            ev.set()

        threading.Thread(target=worker, daemon=True).start()
        ev.wait(timeout=600)
        if "data" not in result:
            result["data"] = {"ok": False, "error": "处理超时（>600s）"}
        self._send(200, json.dumps(result["data"], ensure_ascii=False).encode("utf-8"))


def main():
    port = 8000
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except ValueError:
            pass
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"PCB Agent 网页已启动： http://127.0.0.1:{port}")
    print("按 Ctrl+C 停止。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")


if __name__ == "__main__":
    main()
