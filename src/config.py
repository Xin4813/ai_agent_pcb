"""加载 config.toml（LLM / Agent / KiCad 配置）。"""
from __future__ import annotations
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


def kicad_symbols_dir(cfg: dict) -> Path:
    install = cfg.get("kicad", {}).get("install_dir", "")
    d = Path(install) / "share" / "kicad" / "symbols"
    if not d.exists():
        # 兜底：常见路径
        d = Path(install) / "share" / "kicad" / "library"
    return d
