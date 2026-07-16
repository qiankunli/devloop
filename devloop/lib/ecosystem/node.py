"""Node 生态（js/ts 共享）：仓内 worktree 的向上解析是它特有的泄漏面。

为什么 ready 校验必须显式、不能靠 tsc/构建自然失败：devloop 的 worktree 在仓库**内部**
（`.worktrees/<tag>`），而 Node 的模块解析从当前文件**逐级向上**找 node_modules——worktree
缺依赖时不报错，静默命中主 checkout 的 `<repo>/node_modules`。版本一致时"碰巧能过"、
不一致时报一堆莫名类型错，都是假信号。所以就绪与否是 gate 前的显式断言（存在 + 指纹一致），
不是错误兜底。
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from .base import Ecosystem

# lockfile → 确定性 install（frozen 语义：lockfile 与 manifest 不一致时失败，绝不改写）。
# 顺序即优先级——同仓多 lockfile 是配置事故，取第一个命中的。绿地基线是 pnpm；
# npm/yarn 只给对应的 frozen 命令，不做任何加速优化（要快去迁 pnpm）。
_LOCKFILES: tuple[tuple[str, list[str]], ...] = (
    ("pnpm-lock.yaml", ["pnpm", "install", "--frozen-lockfile", "--prefer-offline"]),
    ("package-lock.json", ["npm", "ci", "--prefer-offline"]),
    ("yarn.lock", ["yarn", "install", "--immutable"]),
)
_MARKER = ".devloop-envhash"   # node_modules/ 内的指纹：prepare 时 manifest+lockfile sha256


class NodeEcosystem(Ecosystem):
    name = "node"
    manifests = ("package.json",)   # tsconfig.json 只是编译配置，不定义项目（可有多份）

    def language(self, path):
        """ts/js 靠 package.json 内容嗅探——生态同一个，语言是展示属性。"""
        try:
            content = (Path(path) / "package.json").read_text(encoding="utf-8")
        except OSError:
            return "javascript"
        return "typescript" if ("typescript" in content.lower() or "@types/" in content) else "javascript"

    @staticmethod
    def _lockfile(path: str | Path) -> tuple[Path, list[str]] | None:
        for name, cmd in _LOCKFILES:
            f = Path(path) / name
            if f.exists():
                return f, cmd
        return None

    def prepare_command(self, path):
        found = self._lockfile(path)
        # 无 lockfile → 不猜：任何裸 install 都可能生成/改写 lockfile，违反"gate 只验证不变更"。
        return found[1] if found else None

    def env_problem(self, path):
        found = self._lockfile(path)
        nm = Path(path) / "node_modules"
        if not nm.is_dir():
            if found is None:
                return ("node_modules missing and no supported lockfile exists — "
                        "cannot auto-prepare without changing project state")
            return ("node_modules missing — in-repo worktrees silently resolve deps "
                    "UPWARD into the main checkout's node_modules")
        if found is None:
            return None                      # 用户自装、无 lockfile：无从校验，fail-open
        lockfile, _ = found
        marker = nm / _MARKER
        if not marker.exists():
            return None                      # 用户自装、无指纹：fail-open，不逼重装
        try:
            prepared = marker.read_text(encoding="utf-8").strip()
        except OSError as e:
            return f"cannot read devloop environment fingerprint: {e}"
        if prepared != _env_hash(path, lockfile):
            return "package.json or lockfile changed since devloop last installed (stale node_modules)"
        return None

    def mark_prepared(self, path):
        found = self._lockfile(path)
        nm = Path(path) / "node_modules"
        if found and nm.is_dir():
            (nm / _MARKER).write_text(_env_hash(path, found[0]), encoding="utf-8")


def _env_hash(path: str | Path, lockfile: Path) -> str:
    """manifest 与 lockfile 任一变化都让 frozen install 重跑。

    只 hash lockfile 会漏掉 package.json 已改、lockfile 尚未同步的状态；恰恰需要让包管理器
    用 frozen 语义把这种不一致报出来，而不是带着旧 node_modules 继续 typecheck。
    """
    h = hashlib.sha256()
    for f in (Path(path) / "package.json", lockfile):
        h.update(f.name.encode())
        h.update(b"\0")
        try:
            h.update(f.read_bytes())
        except OSError:
            pass
        h.update(b"\0")
    return h.hexdigest()
