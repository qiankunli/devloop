"""codemodel —— 代码投影层（与 cmdtree 平级）：把一个待落盘的文件改动投影成
Rule 可读的事实（imports / decls / layer / module）。按语言换 analyzer（Python 走 ast，
Go 待补）；后端可换，同 cmdtree 的 parser。
"""
from .analyze import enrich  # noqa: F401
