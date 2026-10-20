"""读取 KiCad 的 .kicad_sym 符号库，解析出引脚坐标（供生成原理图时连网使用）。

只做“读取”，不改写任何库文件。支持符号的 extends（继承）链解析。
"""
from __future__ import annotations
import copy
import re
from pathlib import Path


def _tokenize(s: str):
    tokens = []
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c in " \t\r\n":
            i += 1
            continue
        if c == "(":
            tokens.append("("); i += 1; continue
        if c == ")":
            tokens.append(")"); i += 1; continue
        if c == '"':
            j = i + 1; buf = ['"']
            while j < n:
                if s[j] == "\\" and j + 1 < n:
                    buf.append(s[j:j + 2]); j += 2; continue
                buf.append(s[j])
                if s[j] == '"':
                    j += 1; break
                j += 1
            tokens.append("".join(buf)); i = j; continue
        j = i
        while j < n and s[j] not in " \t\r\n()":
            j += 1
        tokens.append(s[i:j]); i = j
    return tokens


def _parse(tokens):
    idx = 0
    n = len(tokens)

    def parse_expr():
        nonlocal idx
        tok = tokens[idx]; idx += 1
        if tok == "(":
            lst = []
            while tokens[idx] != ")":
                lst.append(parse_expr())
            idx += 1
            return lst
        return tok

    return parse_expr()


def _find_sub(node, key):
    if not isinstance(node, list):
        return None
    for child in node:
        if isinstance(child, list) and child and child[0] == key:
            return child
    return None


def _find_symbol_in(root, name_quoted):
    for child in root:
        if isinstance(child, list) and child and child[0] == "symbol" and len(child) > 1 and child[1] == name_quoted:
            return child
    return None


class SymbolLib:
    def __init__(self, symbols_dir: str | Path):
        self.dir = Path(symbols_dir)
        self._cache = {}  # lib_file -> parsed root

    def _load(self, lib_name: str):
        if lib_name in self._cache:
            return self._cache[lib_name]
        path = self.dir / f"{lib_name}.kicad_sym"
        if not path.exists():
            raise FileNotFoundError(f"找不到符号库: {path}")
        text = path.read_text(encoding="utf-8", errors="ignore")
        root = _parse(_tokenize(text))
        self._cache[lib_name] = root
        return root

    def _symbol_node(self, lib_id: str):
        lib_name, _, sym_name = lib_id.partition(":")
        root = self._load(lib_name)
        node = _find_symbol_in(root, f'"{lib_id}"')
        if node is None:
            node = _find_symbol_in(root, f'"{sym_name}"')
        return node  # 可能为 None（继承自不存在的父符号时）

    def _resolve_chain(self, lib_id: str):
        """返回从祖先到自身的符号节点链（自身在最后，可覆盖父级）。"""
        chain = []
        cur_id = lib_id
        seen = set()
        while cur_id and cur_id not in seen:
            seen.add(cur_id)
            node = self._symbol_node(cur_id)
            if node is None:
                break
            chain.append(node)
            ext = _find_sub(node, "extends")
            if ext and len(ext) > 1:
                parent = ext[1].strip('"')
                cur_id = parent if ":" in parent else f"{lib_id.split(':')[0]}:{parent}"
            else:
                cur_id = None
        chain.reverse()  # 祖先在前，自身在后
        return chain

    def get_pins(self, lib_id: str) -> dict:
        """返回 {引脚号(int): (x, y, angle)}，坐标为符号局部坐标。

        KiCad 8 的引脚定义在嵌套的子单元符号（如 C_0_1 / C_1_1）内，
        因此这里递归收集整棵符号树中的所有 pin 节点再解析。
        """
        pins = {}
        for node in self._resolve_chain(lib_id):
            for pin_node in _collect_pins(node):
                at = _find_sub(pin_node, "at")
                num = _find_sub(pin_node, "number")
                if at is None or num is None:
                    continue
                try:
                    x = float(at[1]); y = float(at[2])
                    angle = float(at[3]) if len(at) > 3 else 0.0
                    n = int(num[1].strip('"'))
                except (ValueError, IndexError):
                    continue
                pins[n] = (x, y, angle)
        return pins

    def pin_numbers(self, lib_id: str):
        return sorted(self.get_pins(lib_id).keys())

    def get_symbol_definitions(self, lib_id: str) -> list:
        """返回该符号用于内嵌到 .kicad_sch 的 (lib_symbols ...) 的 S-expression 文本。

        关键：KiCad 的原理图解析器**无法在 lib_symbols 内解析 extends 继承链**，
        直接内嵌带 extends 的符号会导致整个原理图「加载失败」。因此这里把继承链
        「扁平化」为单一、自包含、无 extends 的符号定义（以最底层祖先的完整几何/引脚
        为基础，重命名为目标符号名，并应用派生层的属性覆盖）。
        """
        chain = self._resolve_chain(lib_id)
        if not chain:
            return []
        self_name = chain[-1][1]
        base = copy.deepcopy(chain[0])
        # 把顶层与所有嵌套子符号名统一重命名为目标符号名
        _rename_symbol(base, chain[0][1], self_name)
        # 移除 extends（已内联）
        base = [c for c in base if not (isinstance(c, list) and c and c[0] == "extends")]
        # 自底向上应用派生层的覆盖（属性、子符号等）
        for node in chain[1:]:
            _merge_overrides(base, node)
        return [_serialize(base)]


def _serialize(node):
    """将解析后的节点树还原为 KiCad S-expression 文本。"""
    if isinstance(node, list):
        return "(" + " ".join(_serialize(c) for c in node) + ")"
    return node


def _rename_symbol(node, old: str, new: str):
    """递归把名为 old（及其带 _X_Y 后缀的嵌套子符号）的 (symbol ...) 重命名为 new。

    old/new 须为带引号的名称（如 '"ATmega48PV-10P"'）。匹配时使用去引号后的
    名称做前缀比较（避免尾引号干扰 _X_Y 后缀判定）。
    """
    if not isinstance(node, list):
        return
    old_u, new_u = old.strip('"'), new.strip('"')
    if node and node[0] == "symbol" and len(node) > 1 and isinstance(node[1], str):
        cur = node[1].strip('"')
        if cur == old_u or cur.startswith(old_u + "_"):
            node[1] = '"' + new_u + cur[len(old_u):] + '"'
    for c in node:
        if isinstance(c, list):
            _rename_symbol(c, old, new)


def _merge_overrides(base, child):
    """把 child 的**属性**覆盖到 base 上（child 为更派生的符号）。

    注意：只合并 property，不合并嵌套子符号。否则会把 child 里
    `(symbol "X_1_1" (extends "Y_1_1") ...)` 这类仍带 extends 的子符号
    拽进来，导致扁平化后产生悬空继承、原理图加载失败。
    """
    for elem in child:
        if not (isinstance(elem, list) and elem):
            continue
        if elem[0] == "property" and len(elem) > 1:
            pname = elem[1]
            replaced = False
            for bi, be in enumerate(base):
                if isinstance(be, list) and be and be[0] == "property" and len(be) > 1 and be[1] == pname:
                    base[bi] = elem
                    replaced = True
                    break
            if not replaced:
                base.append(elem)


def _collect_pins(node):
    """递归收集节点树中所有的 pin 节点。"""
    found = []
    if not isinstance(node, list):
        return found
    if node and node[0] == "pin":
        found.append(node)
    for child in node:
        if isinstance(child, list):
            found.extend(_collect_pins(child))
    return found
