"""校验 BOM 与网表 JSON 的结构合法性（步骤③的反馈闭环里用它判断是否要重试）。"""
from __future__ import annotations


class ValidationError(Exception):
    pass


def validate_bom(bom: dict) -> None:
    if not isinstance(bom, dict):
        raise ValidationError("BOM 必须是对象")
    if "items" not in bom or not isinstance(bom["items"], list) or not bom["items"]:
        raise ValidationError("BOM.items 必须是非空数组")
    refs = set()
    for it in bom["items"]:
        if "ref" not in it or "symbol" not in it:
            raise ValidationError(f"元器件缺少 ref 或 symbol: {it}")
        if it["ref"] in refs:
            raise ValidationError(f"重复的 ref: {it['ref']}")
        refs.add(it["ref"])


def validate_netlist(netlist: dict, bom: dict) -> None:
    if not isinstance(netlist, dict) or "nets" not in netlist:
        raise ValidationError("网表必须包含 nets")
    refs = {it["ref"] for it in bom["items"]}
    seen_pins = set()
    for net in netlist["nets"]:
        if "name" not in net or "pins" not in net:
            raise ValidationError(f"网络缺少 name 或 pins: {net}")
        for pin in net["pins"]:
            if ":" not in pin:
                raise ValidationError(f"引脚格式应为 REF:引脚号，得到: {pin}")
            ref, _, num = pin.partition(":")
            if ref not in refs:
                raise ValidationError(f"网表引用了 BOM 中不存在的元件: {ref}")
            if pin in seen_pins:
                raise ValidationError(f"引脚出现在多个网络（冲突）: {pin}")
            try:
                int(num)
            except ValueError:
                raise ValidationError(f"引脚号必须是整数: {pin}")
            seen_pins.add(pin)
