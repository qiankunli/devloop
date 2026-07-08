"""投影 + 评估。

- `project(HookInput) → Change`：Bash → 若干 `Command`（经 cmdtree）；Edit/Write/… → 一个 `FileChange`；
  其它工具 → 空 targets（默认放行）。
- `evaluate(Change, ctx, rules) → Decision`：把每个 target 路由到匹配的 Rule，content-aware 规则
  命中时惰性解析文件内容，收集 Finding 聚合成 Decision。逐规则 fail-open。
"""
from __future__ import annotations

from pathlib import Path

from lib.cmdtree import cmdparse
from lib.codemodel.analyze import enrich

from .context import PolicyContext
from .domain import Change, Command, Decision, FileChange, Finding, Severity, Target, TargetKind
from .protocol import FailurePolicy, Rule

_FILE_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit", "apply_patch")


def project(inp) -> Change:
    """HookInput → Change。投影在工具层：Bash 的内部语义留给 Command-rule 下钻。"""
    if inp.is_tool("Bash"):
        base = Path(inp.cwd or ".")
        cmds: list[Target] = []
        for v in cmdparse.command_invocations(inp.command):
            cmds.append(
                Command(
                    argv=list(v.argv),
                    run_dir=v.run_dir(base),
                    cd=v.cd,
                    subcommand=getattr(v, "subcommand", None),
                    args=list(getattr(v, "args", []) or []),
                    dash_c=getattr(v, "dash_c", None),
                )
            )
        return Change(targets=cmds, cwd=inp.cwd, tool="Bash", command=inp.command)

    if inp.is_tool("apply_patch"):
        targets = [
            FileChange(path=path, mode=mode, tool_input=dict(inp.tool_input or {}))
            for path, mode in _patch_file_changes(inp.tool_input)
        ]
        return Change(targets=targets, cwd=inp.cwd, tool=inp.tool_name)

    if inp.is_tool(*_FILE_TOOLS):
        path = inp.file_path
        mode = "write" if inp.is_tool("Write") else "edit"
        targets: list[Target] = []
        if path:
            targets.append(FileChange(path=path, mode=mode, tool_input=dict(inp.tool_input or {})))
        return Change(targets=targets, cwd=inp.cwd, tool=inp.tool_name)

    return Change(targets=[], cwd=inp.cwd, tool=inp.tool_name)


def _patch_file_changes(tool_input: dict) -> list[tuple[str, str]]:
    """Extract file paths from Codex apply_patch payloads.

    The edit guards only need path-level targets. Parsing the full patch grammar here would
    duplicate the tool; these stable hunk headers are enough for owner / inactive branch /
    requirements-file rules, and malformed input just yields no targets (fail-open).
    """
    patch = (
        tool_input.get("patch")
        or tool_input.get("input")
        or tool_input.get("command")
        or ""
    )
    if not isinstance(patch, str):
        return []
    out: list[tuple[str, str]] = []
    for line in patch.splitlines():
        if line.startswith("*** Add File: "):
            out.append((line[len("*** Add File: "):].strip(), "write"))
        elif line.startswith("*** Update File: "):
            out.append((line[len("*** Update File: "):].strip(), "edit"))
        elif line.startswith("*** Delete File: "):
            out.append((line[len("*** Delete File: "):].strip(), "edit"))
    return out


def evaluate(change: Change, ctx: PolicyContext, rules: list[Rule]) -> Decision:
    """跑规则、聚合 Decision。"""
    findings: list[Finding] = []

    for target in change.targets:
        kind = getattr(target, "kind", None)
        applicable = [r for r in rules if r.target_kind == kind and _safe_applies(r, target, ctx)]
        if not applicable:
            continue
        # content-aware 规则命中 → 惰性解析（读盘+套 edit 得"改后全文"再解析 imports/decls）
        if isinstance(target, FileChange) and any(r.needs_content for r in applicable):
            try:
                enrich(target, ctx)
            except Exception:
                pass  # 解析失败 → 不产 content findings（fail-open）
        for r in applicable:
            findings.extend(_safe_check(r, target, ctx))

    # mutation 级规则：不看具体 target，直接吃 Change
    for r in rules:
        if r.target_kind == TargetKind.CHANGE and _safe_applies(r, change, ctx):
            findings.extend(_safe_check(r, change, ctx))

    return Decision.of(findings)


def _safe_applies(rule: Rule, target, ctx) -> bool:
    try:
        return bool(rule.applies(target, ctx))
    except Exception:
        return False  # applies 抛异常 → 视作不匹配（fail-open）


def _safe_check(rule: Rule, target, ctx) -> list[Finding]:
    try:
        return rule.check(target, ctx) or []
    except Exception:
        if rule.failure_policy() is FailurePolicy.FAIL_CLOSED:
            return [Finding(rule=rule.name, severity=Severity.DENY, message=f"{rule.name}: 规则执行出错（fail-closed）")]
        return []  # fail-open：守卫的 bug 绝不拦用户
