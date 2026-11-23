"""加载 config.toml（LLM / Agent / KiCad 配置），并自动探测本机 KiCad 资源。

KiCad 的符号库目录与 kicad-cli 位置因操作系统、安装方式（安装包 / 便携版 /
包管理器）而异。本项目提供跨平台自动探测，让助教程序按 README 即可跑通，
无需手写任何本机绝对路径。探测优先级：

    config.toml 的 kicad.install_dir  >  环境变量  >  PATH  >  各平台默认安装路径

环境变量（可选，用于非标准安装）：
    KICAD_SYMBOL_DIR   —— 直接指向 symbols 目录
    KICAD_INSTALL_DIR  —— 指向 KiCad 安装根目录（其下 bin/ 与 share/kicad/）
"""
from __future__ import annotations
import os
import shutil
import sys
import tomllib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "config.toml"


def load_config(path=None) -> dict:
    p = Path(path) if path else DEFAULT_CONFIG
    if not p.exists():
        raise FileNotFoundError(
            f"未找到配置文件 {p}\n请先执行: cp config/config.example.toml config/config.toml 并填入密钥"
        )
    with open(p, "rb") as f:
        return tomllib.load(f)


def _is_symbols_dir(d: Path) -> bool:
    """目录存在且确实含有 .kicad_sym 文件，才认为是有效的符号库目录。"""
    try:
        return d.is_dir() and any(d.glob("*.kicad_sym"))
    except OSError:
        return False


def _symbols_candidates(cfg: dict | None) -> list[Path]:
    """按优先级生成符号库目录候选路径（去重、保序）。"""
    out: list[Path] = []

    def add(*paths):
        for p in paths:
            if p and p not in out:
                out.append(Path(p))

    # 1) config.toml 显式指定
    if cfg:
        install = cfg.get("kicad", {}).get("install_dir", "")
        if install:
            base = Path(install)
            add(base / "share" / "kicad" / "symbols",  # KiCad 8+
                base / "share" / "kicad" / "library")  # KiCad 6/7

    # 2) 环境变量
    for env in ("KICAD_SYMBOL_DIR", "KICAD8_SYMBOL_DIR", "KICAD9_SYMBOL_DIR"):
        if os.environ.get(env):
            add(os.environ[env])
    if os.environ.get("KICAD_INSTALL_DIR"):
        base = Path(os.environ["KICAD_INSTALL_DIR"])
        add(base / "share" / "kicad" / "symbols", base / "share" / "kicad" / "library")

    # 3) 各平台默认安装路径
    if sys.platform == "win32":
        pf = [Path(os.environ.get("ProgramFiles", "C:/Program Files")),
              Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)"))]
        local = os.environ.get("LOCALAPPDATA")
        if local:
            pf.append(Path(local) / "Programs")
        roots = []
        for base in pf:
            for major in ("9.0", "8.0", "7.0", "6.0"):
                roots.append(base / "KiCad" / major)
        add(*[r / "share" / "kicad" / "symbols" for r in roots],
            *[r / "share" / "kicad" / "library" for r in roots])
    elif sys.platform == "darwin":  # macOS
        add("/Applications/KiCad/KiCad.app/Contents/SharedSupport/symbols",
            "/Applications/KiCad/KiCad.app/Contents/SharedSupport/library")
    else:  # Linux / BSD
        add("/usr/share/kicad/symbols", "/usr/local/share/kicad/symbols",
            "/usr/share/kicad/library")
    return out


def find_kicad_symbols_dir(cfg: dict | None = None) -> Path | None:
    """自动探测 KiCad 符号库目录；找不到返回 None（调用方优雅降级）。"""
    for d in _symbols_candidates(cfg):
        if _is_symbols_dir(d):
            return d
    return None


def kicad_symbols_dir(cfg: dict) -> Path:
    """兼容旧接口：优先自动探测，失败时退回按配置推导的路径。"""
    found = find_kicad_symbols_dir(cfg)
    if found is not None:
        return found
    install = cfg.get("kicad", {}).get("install_dir", "")
    return Path(install) / "share" / "kicad" / "symbols"


def find_kicad_cli(cfg: dict | None = None) -> str | None:
    """自动探测 kicad-cli 可执行文件；找不到返回 None（调用方优雅降级）。"""
    # 1) config.toml 显式指定
    if cfg:
        install = cfg.get("kicad", {}).get("install_dir", "")
        if install:
            for rel in ("bin/kicad-cli.exe", "bin/kicad-cli"):
                cand = Path(install) / rel
                if cand.is_file():
                    return str(cand)
    # 2) 环境变量
    if os.environ.get("KICAD_INSTALL_DIR"):
        base = Path(os.environ["KICAD_INSTALL_DIR"])
        for rel in ("bin/kicad-cli.exe", "bin/kicad-cli"):
            cand = base / rel
            if cand.is_file():
                return str(cand)
    # 3) PATH（跨平台，shutil.which 内部处理 .exe 等后缀）
    for name in ("kicad-cli", "kicad-cli.exe"):
        found = shutil.which(name)
        if found:
            return found
    # 4) 从已探测到的符号库目录反推安装根目录：<root>/share/kicad/symbols
    sym = find_kicad_symbols_dir(cfg)
    if sym is not None:
        for parent in (sym.parent.parent.parent, sym.parent.parent):
            for rel in ("bin/kicad-cli.exe", "bin/kicad-cli"):
                cand = parent / rel
                if cand.is_file():
                    return str(cand)
    # 5) 各平台默认路径
    if sys.platform == "win32":
        roots = [Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "KiCad" / v
                 for v in ("9.0", "8.0", "7.0", "6.0")]
        local = os.environ.get("LOCALAPPDATA")
        if local:
            roots += [Path(local) / "Programs" / "KiCad" / v
                      for v in ("9.0", "8.0", "7.0", "6.0")]
        for r in roots:
            for rel in ("bin/kicad-cli.exe", "bin/kicad-cli"):
                cand = r / rel
                if cand.is_file():
                    return str(cand)
    elif sys.platform == "darwin":
        for rel in ("Contents/SharedSupport/bin/kicad-cli", "Contents/MacOS/kicad-cli"):
            cand = Path("/Applications/KiCad/KiCad.app") / rel
            if cand.is_file():
                return str(cand)
    else:
        for cand in (Path("/usr/bin/kicad-cli"), Path("/usr/local/bin/kicad-cli")):
            if cand.is_file():
                return str(cand)
    return None
