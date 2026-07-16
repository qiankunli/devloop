"""rules —— 具体 Rule 子类的集合（外置于 core，可积累的"判子"面）。

`REGISTRY` 是引擎消费的全部规则；hook 把它传给 `engine.evaluate`，引擎按 target 类型路由
（Bash hook → COMMAND/CHANGE 规则；Edit hook → FILE_CHANGE 规则）。新增规则 = 写一个 `Rule`
子类、登记进 REGISTRY，不碰 core。
"""
from hooks.rules.code.layer_deps import LayerDepsRule
from hooks.rules.command.add_all import AddAllRule
from hooks.rules.command.checkout_owner import CheckoutOwnerGuardRule
from hooks.rules.command.pip_install import PipInstallRule
from hooks.rules.command.precommit_gate import PrecommitGateRule
from hooks.rules.command.protect_branch import ProtectBranchRule
from hooks.rules.command.pytest_naked import PytestNakedRule
from hooks.rules.command.workspace_cwd import WorkspaceCwdRule
from hooks.rules.command.worktree_add import WorktreeAddRule
from hooks.rules.edit.branch_merged import BranchMergedGuardRule
from hooks.rules.edit.edit_owner import EditOwnerGuardRule
from hooks.rules.edit.requirements_edit import RequirementsEditRule

REGISTRY = [
    # 命令侧（Bash）
    ProtectBranchRule(),
    CheckoutOwnerGuardRule(),
    WorktreeAddRule(),
    AddAllRule(),
    WorkspaceCwdRule(),
    PytestNakedRule(),
    PipInstallRule(),
    PrecommitGateRule(),
    # 编辑侧（Edit/Write/…）
    EditOwnerGuardRule(),
    BranchMergedGuardRule(),
    RequirementsEditRule(),
    # 代码 lint
    LayerDepsRule(),
]
