"""Review-engine 协议：devloop 作为 caller 只依赖「一个 review tool 该提供什么能力、
返回什么」，具体引擎（ocr / ccr / 将来别的）各自实现。换或加引擎 = 在这里加一个
adapter，`run_review` 一行不用动。

协议 `ReviewEngine`：
  - `name` · `available()` · `configured(repo)` · `install_hint()` · `rule_path()`
  - `review(repo, from_ref, to_ref, background) -> ReviewResult`

`ReviewResult` 是 devloop 依赖的**归一化返回形状**（引擎无关）；adapter 负责把自家
CLI / 输出映射成它。ocr 与 ccr 恰好共用一套 CLI（ccr 是 ocr 的 fork），故复用
`OcrFamilyEngine`；一个 CLI 不兼容的引擎（别的命令行、库、或 HTTP API）直接实现
`ReviewEngine` 即可——这正是协议存在的意义。
"""
from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


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

    def review(self, repo: str, from_ref: str, to_ref: str, background: str | None) -> ReviewResult:
        ...


class OcrFamilyEngine:
    """ocr 及其 fork（ccr）共用一套 CLI：
      `<bin> review --from --to --format json --repo [--background]`、`<bin> llm test`、
      `<bin> config set llm.*`，输出 JSON `{comments, warnings, status, message}`。
    故只差二进制名 / 安装提示 / rule 路径——新增同族引擎 = 多一个实例，无需新类。"""

    def __init__(self, name: str, binary: str, install: str, rule_path: str):
        self.name = name
        self._bin = binary
        self._install = install
        self._rule = rule_path

    def available(self) -> bool:
        return shutil.which(self._bin) is not None

    def configured(self, repo: str) -> bool:
        return subprocess.run([self._bin, "llm", "test"], cwd=repo, capture_output=True).returncode == 0

    def install_hint(self) -> str:
        return self._install

    def rule_path(self) -> str:
        return self._rule

    def review(self, repo: str, from_ref: str, to_ref: str, background: str | None) -> ReviewResult:
        cmd = [self._bin, "review", "--from", from_ref, "--to", to_ref, "--format", "json", "--repo", repo]
        if background:
            cmd += ["--background", background]
        r = subprocess.run(cmd, cwd=repo, capture_output=True, text=True)
        try:
            out = json.loads(r.stdout)
        except json.JSONDecodeError:
            return ReviewResult(ok=False, error=(r.stderr or r.stdout or f"{self._bin} produced no JSON")[-2000:])
        warnings = out.get("warnings") or []
        failed = sum(1 for w in warnings if isinstance(w, dict) and w.get("type") == "subtask_error")
        return ReviewResult(
            ok=True,
            status=out.get("status", "success"),
            comments=out.get("comments") or [],
            warnings=warnings,
            failed=failed,
            message=out.get("message", ""),
        )


# 注册表：name → 引擎实例。默认 ccr；切换是一行配置 `{"review": {"tool": "ocr"}}`。
# install hints kept English to match the user-facing skip/error messages.
_ENGINES: dict[str, ReviewEngine] = {
    "ccr": OcrFamilyEngine("ccr", "ccr", "see github.com/qiankunli/case-code-review", "<repo>/.ccr/rule.json"),
    "ocr": OcrFamilyEngine("ocr", "ocr", "npm i -g @alibaba-group/open-code-review", "<repo>/.opencodereview/rule.json"),
}
_DEFAULT = "ccr"


def resolve(name: str | None) -> ReviewEngine:
    """按名字取引擎（默认 ccr）；未知 / 空名字回落到默认（graceful，不报错）。"""
    return _ENGINES.get(name or _DEFAULT, _ENGINES[_DEFAULT])
