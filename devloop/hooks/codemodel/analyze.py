"""把 `FileChange` 投影成 Rule 可读的事实：text / lang / layer / imports / decls。

`enrich(fc, ctx)` 是入口（引擎在 content-aware 规则命中时调用）：
1. 求"改后全文"（Write=content；Edit/MultiEdit=读盘+套用替换）——import 在文件顶部，
   只看 diff 片段会漏，必须基于改后全文解析。
2. 从路径+arch 配置定 layer；按扩展名定 lang。
3. 源码类型用对应 analyzer 解析 imports/decls（当前 Python via ast；Go 待补，先返回空）。

非目标语言/解析失败 → imports/decls 置空，规则自然 no-op（fail-open 在引擎侧兜底）。
"""
from __future__ import annotations

import ast
from pathlib import Path

from hooks.core.domain import Decl, FileChange, Import

_LANG_BY_EXT = {".py": "python", ".go": "go", ".ts": "typescript", ".tsx": "typescript", ".js": "javascript"}


def enrich(fc: FileChange, ctx) -> None:
    """就地填充 fc 的 text/lang/layer/imports/decls。"""
    arch = getattr(ctx, "arch", None) or {}
    fc.lang = _detect_lang(fc.path)
    fc.layer = _layer_of(fc.path, arch.get("layers") or {})
    fc.text = _resulting_text(fc.tool_input or {}, fc.path)

    if fc.lang == "python":
        layer_names = arch.get("order") or list((arch.get("layers") or {}).values())
        fc.imports = _py_imports(fc.text, layer_names)
        fc.decls = _py_decls(fc.text)
    else:
        # 其它语言 analyzer 待补（Go/TS）；先置空，content 规则 no-op。
        fc.imports = []
        fc.decls = []


# ── 路径/语言 ───────────────────────────────────────────────────────────────
def _detect_lang(path: str) -> str | None:
    return _LANG_BY_EXT.get(Path(path).suffix.lower())


def _layer_of(path: str, layers_map: dict) -> str | None:
    """路径 → 层：layers_map 是 {路径片段: 层名}，命中第一个即返回。语言无关。"""
    for frag, name in layers_map.items():
        if frag in path:
            return name
    return None


def _import_layer(module: str, layer_names) -> str | None:
    """import 的模块路径 → 它属于哪一层：模块点分段里若有一段等于某层名即归该层（启发式，先够用）。"""
    segs = set(module.split("."))
    for name in layer_names:
        if name in segs:
            return name
    return None


# ── Python analyzer（stdlib ast）─────────────────────────────────────────────
def _py_imports(text: str, layer_names) -> list[Import]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []  # 语法错 → 不产 import 事实（fail-open）
    out: list[Import] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.append(Import(raw=alias.name, target_layer=_import_layer(alias.name, layer_names)))
        elif isinstance(node, ast.ImportFrom):
            if node.module:  # `from . import x`（module=None）无法定层，跳过
                out.append(Import(raw=node.module, target_layer=_import_layer(node.module, layer_names)))
    return out


def _py_decls(text: str) -> list[Decl]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    out: list[Decl] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            span = (getattr(node, "end_lineno", node.lineno) or node.lineno) - node.lineno + 1
            out.append(Decl(kind="func", name=node.name, span=span))
        elif isinstance(node, ast.ClassDef):
            span = (getattr(node, "end_lineno", node.lineno) or node.lineno) - node.lineno + 1
            out.append(Decl(kind="class", name=node.name, span=span))
    return out


# ── 改后全文 ─────────────────────────────────────────────────────────────────
def _resulting_text(tool_input: dict, path: str) -> str:
    """工具层得到的"改后全文"。Write=整篇 content；Edit/MultiEdit=读盘后套用替换。"""
    if "content" in tool_input:  # Write
        return tool_input.get("content") or ""
    try:
        cur = Path(path).read_text(encoding="utf-8")
    except OSError:
        cur = ""
    if "edits" in tool_input and isinstance(tool_input["edits"], list):  # MultiEdit
        for e in tool_input["edits"]:
            cur = cur.replace(e.get("old_string", ""), e.get("new_string", "") or "", 1)
        return cur
    old = tool_input.get("old_string")  # Edit
    if old is not None:
        new = tool_input.get("new_string") or ""
        return cur.replace(old, new) if tool_input.get("replace_all") else cur.replace(old, new, 1)
    return cur
