"""rules —— 具体 Rule 子类的集合（外置于 core，可积累的"判子"面）。

`REGISTRY` 是引擎消费的全部规则；hook 把它传给 `engine.evaluate`，引擎按 target 类型路由
（Bash hook → COMMAND/CHANGE 规则；Edit hook → FILE_CHANGE 规则）。新增规则 = 写一个 `Rule`
子类、登记进 REGISTRY，不碰 core。
"""
from lib.rules.code.layer_deps import LayerDepsRule
from lib.rules.command.add_all import AddAllRule
from lib.rules.command.checkout_owner import CheckoutOwnerGuardRule
from lib.rules.command.pip_install import PipInstallRule
from lib.rules.command.precommit_gate import PrecommitGateRule
from lib.rules.command.protect_branch import ProtectBranchRule
from lib.rules.command.pytest_naked import PytestNakedRule
from lib.rules.command.workspace_cwd import WorkspaceCwdRule
from lib.rules.edit.branch_merged import BranchMergedGuardRule
from lib.rules.edit.edit_owner import EditOwnerGuardRule
from lib.rules.edit.requirements_edit import RequirementsEditRule

REGISTRY = [
    # 命令侧（Bash）
    ProtectBranchRule(),
    CheckoutOwnerGuardRule(),
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
