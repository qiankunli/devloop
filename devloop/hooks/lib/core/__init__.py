"""core —— 变更策略引擎的核心：概念(domain) + 契约(protocol) + 门面(context) + 引擎(engine)。

具体规则子类不在这里，在 `lib/rules/`（外置，可积累）。设计见
工作区 `docs/loop-architecture-v2.md`。
"""
from .domain import (  # noqa: F401
    Change,
    Command,
    Decision,
    Decl,
    FileChange,
    Finding,
    Import,
    Severity,
    Target,
    TargetKind,
)
from .protocol import FailurePolicy, Rule  # noqa: F401
