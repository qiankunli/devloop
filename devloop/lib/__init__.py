"""devloop CLI-agnostic shared logic.

Pure-stdlib modules with no CLI-protocol or plugin-root dependency, so the same
code runs under any CLI's hook entry scripts (which self-locate via sys.path).
Import submodules directly: `from lib import gitcmd, hook_io, repo_layout`.
"""
