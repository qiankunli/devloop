"""变更策略引擎的核心领域：`Change → Target → (Rule) → Decision`。

纯数据 + 零依赖——每条 Rule 读的稳定骨架、引擎聚合的对象。
房子风格对齐 loop-harness/middleware（`Decision` 带工厂构造），但用 stdlib dataclass：
PreToolUse 路径每次工具调用都跑，必须 import 轻量，不引 pydantic。

中心句：**对一个待生效的 `Change`（携带若干 `Target`），跑匹配该 Target 的 `Rule`，聚合出 `Decision`。**
- `Change`   一次被 hook 拦截、尚未落地的工具调用——引擎的输入。
- `Target`   这次调用作用的主体（`Command` | `FileChange`）——Rule 评判的对象。
- `Decision` allow/warn/deny——对一次 Change 的聚合产出（驱动架构图里"硬拦截"盒子）。

刻意没有顶层 `operation` 轴：动词长在 target 内部（`Command.subcommand` / `FileChange.mode`），
否则 `operation=run` 作用在本身就是动作的 `Command` 上会"动词套动词"，反而confusing。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Literal


class Severity(str, Enum):
    WARN = "warn"  # 软提示：不拦，建议性 advisory（≠ 状态总线的 turn 注入软提醒）
    DENY = "deny"  # 硬拦：阻断本次工具调用


class TargetKind(str, Enum):
    COMMAND = "command"
    FILE_CHANGE = "file_change"
    CHANGE = "change"  # mutation 级规则：不看具体 target，只看 Change/Context


@dataclass
class Finding:
    """一条 Rule 对一个 target 的判定。deny 时 `message` 原样回灌给 agent。"""

    rule: str
    severity: Severity
    message: str
    locator: str = ""  # 命中位置：命令串 / 文件路径，便于定位


@dataclass
class Decision:
    """对一次 Change 的聚合结果。工厂构造（房子风格），不直接 new。"""

    action: Literal["allow", "warn", "deny"]
    findings: list[Finding] = field(default_factory=list)

    @classmethod
    def allow(cls) -> Decision:
        return cls(action="allow")

    @classmethod
    def of(cls, findings: list[Finding]) -> Decision:
        """聚合：任一 deny → deny；否则任一 warn → warn；否则 allow。
        deny 支配——一条硬违规即阻断，不被软提示稀释。"""
        if any(f.severity is Severity.DENY for f in findings):
            return cls(action="deny", findings=findings)
        if any(f.severity is Severity.WARN for f in findings):
            return cls(action="warn", findings=findings)
        return cls(action="allow", findings=findings)

    @property
    def blocked(self) -> bool:
        return self.action == "deny"

    def message(self) -> str:
        """驱动当前 action 的那些 finding 的消息（deny 的，或 warn 的），拼成一段。"""
        sev = Severity.DENY if self.action == "deny" else Severity.WARN
        return "\n\n".join(f.message for f in self.findings if f.severity is sev)


# ── Targets：被 Change 作用的主体。两个都按"agent 即将做的一件事"命名（同范畴 peer），
#    动词长在内部字段；不同 target 类型"内部丰不丰富"不同，这是子类该担的差异 ──────────
@dataclass
class Target:
    """基类。共同本质：能被投影成事实、被 Rule 评判。

    开放层级（非封闭枚举，仿 k8s admission 的 resource/GVK，而非硬编码 Pod|Service）：
    - 今天只为"有规则要用"的工具做类型化投影：`Command`(Bash)、`FileChange`(Edit/Write/MultiEdit/NotebookEdit)。
      现有守卫 100% 落这两类。
    - 其余工具（`mcp__*`、WebFetch、Agent…）暂不投影；引擎对它们产生空 targets → 默认放行。
    - 某工具需要结构化事实时才"提升"成自己的 typed 子类（规则驱动、惰性增长）→ 抽象不会被卡死。
    两件别误加子类的事：
    1. 文件格式（config/SQL/markdown/yaml）不是新 Target，是 `FileChange` 换 analyzer（codemodel 轴，N+M）。
    2. 很多"未来动作"（curl 联网 / rm / deploy / git）经 Bash 进来，本就是 `Command`，无需新 Target。
    """


@dataclass(frozen=True)
class WorkingDir:
    """命令实际执行目录；路径缺失表示 runtime 未提供足够信息，位置不可判定。

    `source` 只记录该事实来自哪个 adapter 字段或契约，供日志与兼容性排查；规则只应根据
    `path` 是否存在决定能否做位置相关的硬判断。
    """

    path: Path | None
    source: str = ""

    @property
    def is_exact(self) -> bool:
        return self.path is not None


@dataclass
class Command(Target):
    """从 cmdtree 投影出的一条 shell 命令（run 的对象）；`run_dir` 已对 cwd 解析完成。
    它内部又裹着一层动作（subcommand=rm/push…），按需在 Command-rule 里下钻，不在顶层展开。"""

    kind = TargetKind.COMMAND
    argv: list[str]
    run_dir: Path
    cd: str | None = None
    # git 专属（非 git 命令为 None）
    subcommand: str | None = None
    args: list[str] = field(default_factory=list)
    dash_c: str | None = None

    @property
    def base(self) -> str:
        import os

        return os.path.basename(self.argv[0]) if self.argv else ""


@dataclass
class Import:
    raw: str  # 源码里的 import spec，如 "app.service.user" / ".../internal/service"
    target_layer: str | None = None


@dataclass
class Decl:
    kind: str  # func / class / method
    name: str
    span: int = 0  # 行数 → 函数过长
    branches: int = 0  # 分支计数 → 圈复杂度
    max_nesting: int = 0


@dataclass
class FileChange(Target):
    """一次待落盘的文件改动（write 整文件 / edit 局部）。覆盖任意文件，不止源码——
    `requirements.txt`/markdown/yaml/SQL 都走这里，故名 FileChange 而非 SourceFile。
    `imports`/`decls` 惰性：为 None 表示尚未解析，只有 content-aware 的 Rule 命中时引擎才
    触发 codemodel 解析（省掉 path-only 规则的解析成本）；非源码类型 analyzer 不填。"""

    kind = TargetKind.FILE_CHANGE
    path: str
    mode: Literal["write", "edit"]
    lang: str | None = None
    layer: str | None = None
    module: str = ""
    text: str = ""
    imports: list[Import] | None = None
    decls: list[Decl] | None = None
    tool_input: dict | None = None  # 原始 Edit/Write 载荷，供惰性求"改后全文"


@dataclass
class Change:
    """一次被拦截、尚未生效的工具调用。引擎的中心输入。
    一次 Bash 可拆出多个 Command；一次 Edit/Write 是一个 FileChange。
    `tool` 仅作日志/溯源，不参与规则匹配（匹配看 target 类型）。"""

    targets: list[Target]
    cwd: str = ""
    tool: str = ""
    command: str = ""  # 原始 Bash 命令串（仅 Bash 非空）；少数 CHANGE 级规则需 env-aware 重解析时用
