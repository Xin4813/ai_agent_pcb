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
        self._all_symbols_cache = None  # lib_id 全量缓存

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

    def list_symbols(self) -> list:
        """返回该目录所有 .kicad_sym 中对外可用的 lib_id（形如 'Device:C'）。

        仅取 .kicad_sym 顶层 (symbol ...) 节点——它们是公开 lib_id；
        嵌套的子单元符号（如 C_0_1）不计（不是独立 lib_id）。
        结果按 lib_id 字典序排序，用于把「真实可用符号目录」喂给 LLM，
        避免模型编造不存在的 lib_id。
        """
        if self._all_symbols_cache is not None:
            return self._all_symbols_cache
        out = []
        for f in sorted(self.dir.glob("*.kicad_sym")):
            lib_name = f.stem
            try:
                root = self._load(lib_name)
            except Exception:
                continue
            for child in root:
                if (isinstance(child, list) and child and child[0] == "symbol"
                        and len(child) > 1 and isinstance(child[1], str)):
                    name = child[1].strip('"')
                    if name:
                        out.append(f"{lib_name}:{name}")
        out.sort()
        self._all_symbols_cache = out
        return out

    def suggest_lib_id(self, bad: str) -> str | None:
        """为一个不存在的 lib_id 推荐最接近的、真实存在的 lib_id。

        匹配优先级：① 符号名完全一致(库也对) > ② 符号名完全一致(库可不同) >
        ③ 符号名包含查询 > ④ 查询是符号名的子序列(容忍封装后缀/字母差异) >
        ⑤ 仅库名一致。用于 BOM 校验报错时给出可改用的正确符号。
        同分时优先符号名更短、库名一致者，避免推荐过长的变体。
        """
        bad = (bad or "").strip()
        if not bad:
            return None
        lib, _, name = bad.partition(":")
        name_l = name.lower()
        lib_l = lib.lower()
        qnorm = re.sub(r"[^a-z0-9]", "", name_l)

        def is_subseq(a: str, b: str) -> bool:
            it = iter(b)
            return all(ch in it for ch in a)

        best = None
        best_key = (-1, -1 << 30, 1, "")
        for s in self.list_symbols():
            sl, _, sn = s.partition(":")
            sn_l = sn.lower()
            snn = re.sub(r"[^a-z0-9]", "", sn_l)
            score = 0
            if sn_l == name_l and (not lib_l or sl.lower() == lib_l):
                score = 100
            elif sn_l == name_l:
                score = 90
            elif name_l and name_l in sn_l:
                score = 80
            elif qnorm and snn and is_subseq(qnorm, snn):
                score = 60
            elif qnorm and snn and (snn.startswith(qnorm[:6]) or qnorm.startswith(snn[:6])):
                # 家族前缀一致（如 atmega328 系列），容忍封装/尾缀差异
                score = 55
            elif lib_l and sl.lower() == lib_l:
                score = 40
            if score <= 0:
                continue
            # 同分：符号名更短优先；再库名一致优先；再字典序
            same_lib = 1 if (lib_l and sl.lower() == lib_l) else 0
            key = (score, -len(sn), -same_lib, s)
            if key > best_key:
                best_key = key
                best = s
        return best

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

    def pin_names(self, lib_id: str) -> dict:
        """返回 {引脚号(int): 引脚名(str)}。引脚名来自 pin 节点内的 (name "...") 子节点。

        供真实 LLM 模式把「每个元件可用的真实引脚号+引脚名」作为事实依据注入提示词，
        避免模型瞎猜引脚导致网表非法。
        """
        names = {}
        for node in self._resolve_chain(lib_id):
            for pin_node in _collect_pins(node):
                num = _find_sub(pin_node, "number")
                nm = _find_sub(pin_node, "name")
                if num is None:
                    continue
                try:
                    n = int(num[1].strip('"'))
                except (ValueError, IndexError):
                    continue
                name = nm[1].strip('"') if (nm and len(nm) > 1) else ""
                names[n] = name
        return names

    def get_symbol_definitions(self, lib_id: str, qualified: bool = False) -> list:
        """返回该符号用于内嵌到 .kicad_sch 的 (lib_symbols ...) 的 S-expression 文本。

        qualified=True 时，内嵌符号顶层（及嵌套子单元）名改为带库前缀的全名
        （如 "Device:C"）——这是 KiCad 标准原理图的写法，实例的 lib_id 与内嵌
        定义同名，ERC 不再报「当前配置不包含库 ''」。

        关键：KiCad 的原理图解析器**无法在 lib_symbols 内解析 extends 继承链**，
        直接内嵌带 extends 的符号会导致整个原理图「加载失败」。因此这里把继承链
        「扁平化」为单一、自包含、无 extends 的符号定义（以最底层祖先的完整几何/引脚
        为基础，重命名为目标符号名，并应用派生层的属性覆盖）。
        """
        chain = self._resolve_chain(lib_id)
        if not chain:
            return []
        base = copy.deepcopy(chain[0])
        if qualified:
            # KiCad 标准写法：顶层符号名带库前缀（如 "Device:C"），
            # 但嵌套单元符号（C_0_1 / C_1_1）只含裸符号名、不带前缀。
            _, _, base_sym = lib_id.partition(":")
            _rename_symbol(base, chain[0][1], f'"{base_sym}"')
            base[1] = f'"{lib_id}"'
        else:
            # 把顶层与所有嵌套子符号名统一重命名为目标符号名
            _rename_symbol(base, chain[0][1], chain[-1][1])
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
