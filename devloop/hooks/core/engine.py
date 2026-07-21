"""投影 + 评估。

- `project(HookInput) → Change`：Bash → 若干 `Command`（经 cmdtree）；Edit/Write/… → 一个 `FileChange`；
  其它工具 → 空 targets（默认放行）。
- `evaluate(Change, ctx, rules) → Decision`：把每个 target 路由到匹配的 Rule，content-aware 规则
  命中时惰性解析文件内容，收集 Finding 聚合成 Decision。逐规则 fail-open。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from hooks.cmdtree import cmdparse
from hooks.codemodel.analyze import enrich

from .context import PolicyContext
from .domain import Change, Command, Decision, FileChange, Finding, Severity, Target, TargetKind, WorkingDir
from .protocol import FailurePolicy, Rule

_FILE_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit", "apply_patch")
_JS_STRING = re.compile(r'"(?:\\.|[^"\\])*"')
_EXEC_COMMAND_CALL = re.compile(
    r'tools\.exec_command\(\s*(\{(?:[^{}"]|"(?:\\.|[^"\\])*")*\})\s*\)',
    re.DOTALL,
)
_JS_OBJECT_KEY = re.compile(r'([{,]\s*)([A-Za-z_$][\w$]*)(\s*:)')


def project(inp) -> Change:
    """HookInput → Change。投影在工具层：Bash 的内部语义留给 Command-rule 下钻。"""
    if inp.is_tool("Bash"):
        return Change(
            targets=_command_targets(inp.command, _bash_working_dir(inp)),
            cwd=inp.cwd,
            tool="Bash",
            command=inp.command,
        )

    if inp.is_tool("exec"):
        return Change(targets=_exec_targets(inp.tool_input, inp.cwd), cwd=inp.cwd, tool=inp.tool_name)

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


def _command_targets(command: str, base: WorkingDir) -> list[Target]:
    def project(v) -> Command:
        path = v.run_dir(base.path)
        source = base.source if base.path is not None or path is None else "command cd/-C"
        return Command(
            argv=list(v.argv),
            working_dir=WorkingDir(path=path, source=source),
            env=list(v.env),
            cd=v.cd,
            subcommand=getattr(v, "subcommand", None),
            args=list(getattr(v, "args", []) or []),
            dash_c=getattr(v, "dash_c", None),
        )

    return [
        project(v)
        for v in cmdparse.command_invocations(command)
    ]


def _bash_working_dir(inp) -> WorkingDir:
    workdir = inp.tool_input.get("workdir")
    if isinstance(workdir, str) and workdir.strip():
        path = Path(workdir).expanduser()
        if not path.is_absolute():
            path = Path(inp.cwd or ".") / path
        return WorkingDir(path=path, source="tool_input.workdir")
    if inp.is_codex:
        return WorkingDir(path=None, source="codex Bash without workdir")
    return WorkingDir(path=Path(inp.cwd or "."), source="hook cwd")


def _exec_targets(tool_input: dict, cwd: str) -> list[Target]:
    """Project Codex's unified ``exec`` envelope back into its nested mutations.

    Codex currently exposes the JavaScript cell as the hook's top-level tool call, so nested
    ``apply_patch`` / ``exec_command`` calls do not fire their own hooks. Generated
    ``exec_command`` arguments use JSON-compatible JavaScript object literals (sometimes with
    bare keys), and generated patches are JSON string literals; unrecognised JavaScript stays
    fail-open.
    """
    source = tool_input.get("input") or tool_input.get("code") or ""
    if not isinstance(source, str):
        return []

    targets: list[Target] = []
    for raw in _EXEC_COMMAND_CALL.findall(source):
        try:
            payload = json.loads(_JS_OBJECT_KEY.sub(r'\1"\2"\3', raw))
        except (TypeError, ValueError):
            continue
        command = payload.get("cmd")
        if isinstance(command, str):
            workdir = payload.get("workdir")
            path = Path(workdir).expanduser() if isinstance(workdir, str) and workdir.strip() else Path(cwd or ".")
            if not path.is_absolute():
                path = Path(cwd or ".") / path
            source_name = "exec_command.workdir" if isinstance(workdir, str) and workdir.strip() else "exec cwd"
            targets.extend(_command_targets(command, WorkingDir(path=path, source=source_name)))

    if "tools.apply_patch" in source:
        seen: set[str] = set()
        for literal in _JS_STRING.findall(source):
            try:
                patch = json.loads(literal)
            except (TypeError, ValueError):
                continue
            if not isinstance(patch, str) or not patch.startswith("*** Begin Patch") or patch in seen:
                continue
            seen.add(patch)
            targets.extend(
                FileChange(path=path, mode=mode, tool_input={"input": patch})
                for path, mode in _patch_file_changes({"input": patch})
            )
    return targets


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
        target_ctx = ctx.for_target(target) if hasattr(ctx, "for_target") else ctx
        kind = getattr(target, "kind", None)
        applicable = [r for r in rules if r.target_kind == kind and _safe_applies(r, target, target_ctx)]
        if not applicable:
            continue
        # content-aware 规则命中 → 惰性解析（读盘+套 edit 得"改后全文"再解析 imports/decls）
        if isinstance(target, FileChange) and any(r.needs_content for r in applicable):
            try:
                enrich(target, target_ctx)
            except Exception:
                pass  # 解析失败 → 不产 content findings（fail-open）
        for r in applicable:
            findings.extend(_safe_check(r, target, target_ctx))

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
