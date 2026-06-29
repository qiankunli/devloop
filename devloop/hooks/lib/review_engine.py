"""Review-engine 协议：devloop 作为 caller 只依赖「一个 review tool 该提供什么能力、
返回什么」，具体引擎（ocr / ccr / 将来别的）各自实现。换或加引擎 = 在这里加一个
adapter，`run_review` 一行不用动。

协议 `ReviewEngine`：
  - `name` · `available()` · `configured(repo)` · `install_hint()` · `rule_path()`
  - `review(repo, from_ref, to_ref, background) -> ReviewResult`

`ReviewResult` 是 devloop 依赖的**归一化返回形状**（引擎无关）；adapter 负责把自家
CLI / 输出映射成它。ocr 与 ccr 现在 CLI 同形（ccr 是 ocr 的 fork），但**各自独立 adapter、
刻意不共享基类**（接受重复代码）——好让它们将来自由演进（ccr 加 flag、换输出都不牵连 ocr）。
一个 CLI 不兼容的引擎（别的命令行、库、HTTP API）直接实现 `ReviewEngine` 即可——这正是协议
存在的意义。

advisory 约束：review 跑在 detach 后台进程里，**绝不能 hang 或崩**。故所有外部调用都带
timeout，并吞掉 `TimeoutExpired` / `FileNotFoundError`（二进制被中途卸载的竞态），降级为
「不可用 / 出错」而非抛异常。
"""
from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

_REVIEW_TIMEOUT = 600   # review 自身要跑 LLM、审全量 diff，给足
_PROBE_TIMEOUT = 30     # llm test 健康探针，短


@dataclass
class ReviewResult:
    """归一化的 review 输出——协议的返回契约，引擎无关。`status` 用 devloop 的词汇
    （`success` / `completed_with_warnings` / `completed_with_errors` / …），adapter 负责
    把自家状态映射过来。"""
    ok: bool                                   # 引擎跑通且输出可解析
    status: str = "success"
    comments: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    failed: int = 0                            # review 失败的文件数
    message: str = ""
    error: str = ""                            # ok=False 时的诊断（写进 review.json）


@runtime_checkable
class ReviewEngine(Protocol):
    """devloop 调的就是这个协议——不依赖任何引擎的具体形态。"""
    name: str

    def available(self) -> bool:               # 装好了、能调用（典型：在 PATH 上）
        ...

    def configured(self, repo: str) -> bool:   # LLM / 凭据就绪、能真跑
        ...

    def install_hint(self) -> str:             # 没装时怎么装
        ...

    def rule_path(self) -> str:                # 项目级 rule.json 位置（给文档 / 提示）
        ...

    def review(self, repo: str, from_ref: str, to_ref: str, background: str | None,
               history_path: str | None = None) -> ReviewResult:
        ...


class CcrEngine:
    """case-code-review（ccr）adapter。与 OcrEngine 刻意各自独立、不共享基类——现在
    CLI 同形，但允许 ccr 自行演进（接受重复）。CLI：
    `ccr review --from --to --format json --repo [--background]`、`ccr llm test`。"""
    name = "ccr"

    def available(self) -> bool:
        return shutil.which("ccr") is not None

    def configured(self, repo: str) -> bool:
        try:
            return subprocess.run(["ccr", "llm", "test"], cwd=repo,
                                  capture_output=True, timeout=_PROBE_TIMEOUT).returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False

    def install_hint(self) -> str:
        return "see github.com/qiankunli/case-code-review"

    def rule_path(self) -> str:
        return "<repo>/.casecodereview/rule.json"

    def review(self, repo: str, from_ref: str, to_ref: str, background: str | None,
               history_path: str | None = None) -> ReviewResult:
        cmd = ["ccr", "review", "--from", from_ref, "--to", to_ref, "--format", "json", "--repo", repo]
        if background:
            cmd += ["--background", background]
        if history_path:  # prior-review findings, injected per unit so the reviewer reconciles them
            cmd += ["--history", history_path]
        try:
            r = subprocess.run(cmd, cwd=repo, capture_output=True, text=True, timeout=_REVIEW_TIMEOUT)
        except subprocess.TimeoutExpired:
            return ReviewResult(ok=False, error=f"ccr review timed out after {_REVIEW_TIMEOUT}s")
        except FileNotFoundError:
            return ReviewResult(ok=False, error="ccr binary not found")
        try:
            out = json.loads(r.stdout)
        except json.JSONDecodeError:
            return ReviewResult(ok=False, error=(r.stderr or r.stdout or "ccr produced no JSON")[-2000:])
        warnings = out.get("warnings") or []
        failed = sum(1 for w in warnings if isinstance(w, dict) and w.get("type") == "subtask_error")
        return ReviewResult(ok=True, status=out.get("status", "success"),
                            comments=out.get("comments") or [], warnings=warnings,
                            failed=failed, message=out.get("message", ""))


class OcrEngine:
    """open-code-review（ocr）adapter。与 CcrEngine 各自独立（见上）。CLI：
    `ocr review --from --to --format json --repo [--background]`、`ocr llm test`。"""
    name = "ocr"

    def available(self) -> bool:
        return shutil.which("ocr") is not None

    def configured(self, repo: str) -> bool:
        try:
            return subprocess.run(["ocr", "llm", "test"], cwd=repo,
                                  capture_output=True, timeout=_PROBE_TIMEOUT).returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False

    def install_hint(self) -> str:
        return "npm i -g @alibaba-group/open-code-review"

    def rule_path(self) -> str:
        return "<repo>/.opencodereview/rule.json"

    def review(self, repo: str, from_ref: str, to_ref: str, background: str | None,
               history_path: str | None = None) -> ReviewResult:
        # history_path ignored: per-unit review history is a ccr-only concept (ocr has no unit).
        cmd = ["ocr", "review", "--from", from_ref, "--to", to_ref, "--format", "json", "--repo", repo]
        if background:
            cmd += ["--background", background]
        try:
            r = subprocess.run(cmd, cwd=repo, capture_output=True, text=True, timeout=_REVIEW_TIMEOUT)
        except subprocess.TimeoutExpired:
            return ReviewResult(ok=False, error=f"ocr review timed out after {_REVIEW_TIMEOUT}s")
        except FileNotFoundError:
            return ReviewResult(ok=False, error="ocr binary not found")
        try:
            out = json.loads(r.stdout)
        except json.JSONDecodeError:
            return ReviewResult(ok=False, error=(r.stderr or r.stdout or "ocr produced no JSON")[-2000:])
        warnings = out.get("warnings") or []
        failed = sum(1 for w in warnings if isinstance(w, dict) and w.get("type") == "subtask_error")
        return ReviewResult(ok=True, status=out.get("status", "success"),
                            comments=out.get("comments") or [], warnings=warnings,
                            failed=failed, message=out.get("message", ""))


# 注册表：name → 引擎实例。默认 ccr；切换是一行配置 `{"review": {"tool": "ocr"}}`。
_ENGINES: dict[str, ReviewEngine] = {
    "ccr": CcrEngine(),
    "ocr": OcrEngine(),
}
_DEFAULT = "ccr"


def resolve(name: str | None) -> ReviewEngine:
    """按名字取引擎（默认 ccr）；未知 / 空名字回落到默认（graceful，不报错——解析出的
    引擎名会出现在 run_review 的输出里，配错了肉眼可见）。"""
    return _ENGINES.get(name or _DEFAULT, _ENGINES[_DEFAULT])
