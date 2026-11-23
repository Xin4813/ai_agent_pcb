"""换名验证：新增规则在真实数据改位号后仍按结构工作（幂等、无假提示）。"""
import copy
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.circuit_fix import fix_topology
from src.symbol_lib import SymbolLib
from src.safety import check_electrical

lib = SymbolLib(r"C:/Users/Gray/AppData/Local/Programs/KiCad/8.0/share/kicad/symbols")
d = ROOT / "examples/output/web_sessions/1e980b1d"
bom = json.loads((d / "bom.json").read_text(encoding="utf-8"))
nl = json.loads((d / "netlist.json").read_text(encoding="utf-8"))

# 所有位号换名 + 网名换名，验证规则不依赖具体命名
refs = {it["ref"]: f"{it['ref'][0]}9{it['ref'][1:]}" for it in bom["items"]}
# LED2_GND -> LED9_GND, LED_A_LED2 -> LED_A_LED9 等
netmap = {}
for net in nl["nets"]:
    if "led2" in net["name"].lower():
        netmap[net["name"]] = net["name"].lower().replace("led2", "led9")
    elif "j1" in net["name"].lower():
        netmap[net["name"]] = net["name"].replace("J1", "J9")

b2, n2 = copy.deepcopy(bom), copy.deepcopy(nl)
for it in b2["items"]:
    it["ref"] = refs.get(it["ref"], it["ref"])
for net in n2["nets"]:
    net["pins"] = [f"{refs.get(p.split(':')[0], p.split(':')[0])}:{p.split(':')[1]}"
                   if ":" in p else p for p in net["pins"]]
    net["name"] = netmap.get(net["name"], net["name"])

fixes = fix_topology(b2, n2, lib)
warnings = check_electrical(b2, n2, lib)
print("换名后幂等 fixes:", fixes)
print("换名后安全告警:", len(warnings))
assert not fixes, "换名后不应再产生修复（原会话已修好）"
assert not warnings, "换名后不应有安全告警"

# 验证关键结构在换名后仍成立：GPIO→R→LED 与 UART GND
def netof(p):
    return next((x["name"] for x in n2["nets"] if p in x["pins"]), None)

new_u = refs["U1"]
assert netof(f"{new_u}:2") == netof(refs["R3"] + ":1"), "GPIO 未接到 LED 限流电阻"
assert netof(refs["J1"] + ":1") == "GND", "UART GND 未接地"
print("换名验证通过：规则按结构工作，不依赖具体位号/网名")
