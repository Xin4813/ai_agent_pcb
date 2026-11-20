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
import tempfile
import threading
import uuid
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


def export_svg(sch_path: Path, cli) -> tuple[str | None, str]:
    """导出原理图为 SVG 文本（用于浏览器内预览）。

    返回 (svg_text, error)：成功时 error 为空，失败时 svg_text 为 None 且
    error 含真实原因（便于前端排查）。

    注意：KiCad 8 的 `sch export svg` 把 `--output` 当作**输出目录**（不同于 ERC
    的 `--output` 是报告文件路径），导出的文件名固定为「原理图名 + .svg」，
    因此这里传目录并在目录里取对应文件名，而非把文件路径当 --output。
    """
    if not cli:
        return None, "kicad-cli 不可用"
    # 用隔离临时目录作为 --output，而不是原理图所在目录。
    # 旧版本曾把「xxx.svg」文件路径直接传给 kicad-cli，KiCad 会把它建成同名目录；
    # 若仍把原理图目录传给 --output，expected 会命中这个残留目录，读取时报
    # Permission denied。临时目录还能避免同名 SVG 被覆盖或误读旧文件。
    with tempfile.TemporaryDirectory(prefix=".kicad_svg_", dir=str(sch_path.parent)) as tmp:
        out_dir = Path(tmp)
        res = _run([cli, "sch", "export", "svg", "--no-background-color",
                    "--output", str(out_dir), str(sch_path)])
        if not res["ok"]:
            return None, "kicad-cli 执行失败：" + (res.get("err") or "未知错误")
        expected = out_dir / (sch_path.stem + ".svg")
        svg = expected if expected.is_file() else None
        if svg is None:
            # 兜底：不同 KiCad 版本可能给导出文件加后缀，按修改时间取最新文件。
            cands = [p for p in out_dir.rglob("*.svg") if p.is_file()]
            svg = max(cands, key=lambda p: p.stat().st_mtime, default=None)
        if svg is None:
            return None, f"未生成 SVG（输出目录：{out_dir}；kicad 报错：{(res.get('err') or '')[:200]}）"
        try:
            return svg.read_text(encoding="utf-8", errors="ignore"), ""
        except Exception as e:  # noqa: BLE001
            return None, f"读取 SVG 失败：{e}"


# 会话表：session_id -> ctx（保存 Agent 与渲染所需的全部状态），带锁保护。
# 网页「追问/更换元件」需要复用同一 Agent（含 BOM/网表/历史），否则每轮都从零开始。
_SESSIONS: dict[str, dict] = {}
_SESSIONS_LOCK = threading.Lock()


def _new_out_dir() -> Path:
    out_dir = Path(os.environ.get("PCB_WEB_OUT") or (ROOT / "examples" / "output"))
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir


def _sym_dir(cfg) -> str:
    if cfg:
        try:
            d = kicad_symbols_dir(cfg)
            if d.exists():
                return str(d)
        except Exception:
            pass
    return r"C:/Users/Gray/AppData/Local/Programs/KiCad/8.0/share/kicad/symbols"


def _render_and_package(ctx: dict, autofix: bool, max_fix: int = 2) -> dict:
    """把 ctx 里当前的 BOM/网表 → 原理图 → ERC(可选闭环) → SVG → 结果 JSON。

    首次生成与「追问修订」共用，保证两者输出结构完全一致。
    """
    agent = ctx["agent"]
    bom, netlist, cli = agent.state["bom"], agent.state["netlist"], ctx["cli"]
    out_dir, sym_dir, name = ctx["out_dir"], ctx["sym_dir"], ctx["name"]

    (out_dir / "bom.json").write_text(json.dumps(bom, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "netlist.json").write_text(json.dumps(netlist, ensure_ascii=False, indent=2), encoding="utf-8")

    sch = write_schematic(netlist, bom, out_dir / "schematic" / f"{name}.kicad_sch", sym_dir)

    loop = []
    last_erc = None
    iters = (max_fix + 1) if (autofix and not ctx.get("mock")) else 1
    for i in range(iters):
        erc = run_erc(sch, cli)
        last_erc = erc
        loop.append({"iter": i, "erc": erc})
        if not autofix or ctx.get("mock") or i >= max_fix:
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

    svg, svg_err = export_svg(sch, cli)
    return {
        "ok": True,
        "source": "LLM" if ctx.get("used") else "离线知识库",
        "prompt": ctx["prompt"],
        "bom": bom,
        "netlist": agent.state["netlist"],
        "sch": str(sch),
        "svg": svg,
        "svg_error": svg_err,
        "erc": last_erc,
        "loop": loop,
        "kicad_cli": cli or "",
        "session_id": ctx["sid"],
        "last_revision": agent.state.get("last_revision", ""),
        "history": list(agent.state.get("history", [])),
        "warnings": list(agent.state.get("warnings", [])),
    }


def run_design(prompt: str, mock: bool, autofix: bool, max_fix: int = 2) -> dict:
    cfg = None if mock else load_config()
    cli = _kicad_cli(cfg)
    agent = Agent(cfg=cfg, mock=mock)
    bom, netlist, used = agent.design(prompt)

    out_dir = _new_out_dir()
    name = "".join(c if c.isalnum() else "_" for c in (bom.get("project") or prompt))[:40] or "design"
    sid = uuid.uuid4().hex
    ctx = {"sid": sid, "agent": agent, "cfg": cfg, "cli": cli, "out_dir": out_dir,
           "sym_dir": _sym_dir(cfg), "name": name, "prompt": prompt, "mock": mock, "used": used}
    with _SESSIONS_LOCK:
        _SESSIONS[sid] = ctx
    return _render_and_package(ctx, autofix, max_fix)


def revise_design(session_id: str, instruction: str, autofix: bool, max_fix: int = 2) -> dict:
    """在已有会话上按自然语言指令更换元件，重画并重新检查。"""
    with _SESSIONS_LOCK:
        ctx = _SESSIONS.get(session_id)
    if ctx is None:
        return {"ok": False, "error": "会话已失效，请重新生成原理图后再修改"}
    agent = ctx["agent"]
    ok = agent.revise(instruction)
    if not ok:
        return {"ok": False, "error": agent.state.get("last_revision", "无法识别该修改指令")}
    result = _render_and_package(ctx, autofix, max_fix)
    result["revision"] = agent.state.get("last_revision", "")
    result["instruction"] = instruction
    return result


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

    @staticmethod
    def _run_async(fn) -> dict:
        """在子线程里跑耗时任务，用事件把结果交回（避免阻塞服务器）。"""
        result = {}
        ev = threading.Event()

        def worker():
            try:
                result["data"] = fn()
            except Exception as e:  # noqa: BLE001
                result["data"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            ev.set()

        threading.Thread(target=worker, daemon=True).start()
        ev.wait(timeout=600)
        return result.get("data", {"ok": False, "error": "处理超时（>600s）"})

    def do_POST(self):
        path = urlparse(self.path).path
        if path not in ("/api/design", "/api/revise"):
            self._send(404, json.dumps({"ok": False, "error": "unknown endpoint"}).encode("utf-8"))
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            data = json.loads(raw.decode("utf-8") or "{}")
        except Exception as e:  # noqa: BLE001
            self._send(400, json.dumps({"ok": False, "error": f"请求解析失败: {e}"}).encode("utf-8"))
            return

        autofix = bool(data.get("autofix", True))
        if path == "/api/design":
            prompt = (data.get("prompt") or "").strip()
            mock = bool(data.get("mock", False))
            if not prompt:
                self._send(400, json.dumps({"ok": False, "error": "需求不能为空"}).encode("utf-8"))
                return
            out = self._run_async(lambda: run_design(prompt, mock, autofix))
        else:  # /api/revise
            sid = (data.get("session_id") or "").strip()
            instruction = (data.get("instruction") or "").strip()
            if not sid or not instruction:
                self._send(400, json.dumps({"ok": False, "error": "缺少 session_id 或修改指令"}).encode("utf-8"))
                return
            out = self._run_async(lambda: revise_design(sid, instruction, autofix))
        self._send(200, json.dumps(out, ensure_ascii=False).encode("utf-8"))


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
