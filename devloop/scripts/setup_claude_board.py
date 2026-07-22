#!/usr/bin/env python3
"""Install devloop's command-backed Board status line into Claude settings."""
from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path


LAUNCHER_NAME = "board_statusline.py"


def _claude_dir(value: str | None = None) -> Path:
    return Path(value or os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude").expanduser()


def _load_settings(path: Path) -> dict:
    if not path.exists() or not path.read_text(encoding="utf-8").strip():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Claude settings must be a JSON object: {path}")
    return value


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        os.replace(name, path)
    except BaseException:
        Path(name).unlink(missing_ok=True)
        raise


def _launcher_source(claude_dir: Path, plugin_root: Path) -> str:
    """A stable user-level launcher that follows marketplace version updates."""
    return f'''#!/usr/bin/env python3
from __future__ import annotations

import os
from pathlib import Path

CLAUDE_DIR = Path({str(claude_dir)!r})
FALLBACK_ROOT = Path({str(plugin_root)!r})


def version_key(path: Path) -> tuple[int, ...]:
    parts = path.name.split(".")
    return tuple(int(part) for part in parts) if parts and all(part.isdigit() for part in parts) else ()


def plugin_root() -> Path:
    base = Path(os.environ.get("CLAUDE_CONFIG_DIR") or CLAUDE_DIR)
    candidates = [
        path for path in base.glob("plugins/cache/*/devloop/*")
        if version_key(path) and (path / "scripts/board_hud.py").is_file()
    ]
    if candidates:
        return max(candidates, key=lambda path: (version_key(path), path.stat().st_mtime))
    return FALLBACK_ROOT


root = plugin_root()
launcher = root / "scripts/python"
script = root / "scripts/board_hud.py"
if launcher.is_file() and script.is_file():
    os.execv(str(launcher), [str(launcher), str(script), "--claude-statusline"])
'''


def install(claude_dir: Path, plugin_root: Path, replace: bool = False) -> tuple[Path, Path | None]:
    settings_path = claude_dir / "settings.json"
    settings = _load_settings(settings_path)
    existing = settings.get("statusLine")
    existing_command = existing.get("command", "") if isinstance(existing, dict) else ""

    launcher_path = claude_dir / "plugins" / "devloop" / LAUNCHER_NAME
    command = f"{shlex.quote(sys.executable)} {shlex.quote(str(launcher_path))}"
    owned = LAUNCHER_NAME in existing_command
    if existing_command and not owned and not replace:
        raise FileExistsError(
            "Claude already has a non-devloop statusLine; rerun with --replace only after user confirmation"
        )

    backup = None
    if settings_path.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        backup = settings_path.with_name(f"{settings_path.name}.bak.{stamp}")
        shutil.copy2(settings_path, backup)

    launcher_path.parent.mkdir(parents=True, exist_ok=True)
    launcher_path.write_text(_launcher_source(claude_dir, plugin_root), encoding="utf-8")
    launcher_path.chmod(0o700)
    settings["statusLine"] = {
        "type": "command",
        "command": command,
        "refreshInterval": 2,
    }
    _atomic_json(settings_path, settings)
    return settings_path, backup


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Configure Claude's native devloop Board status line")
    parser.add_argument("--claude-config-dir")
    parser.add_argument("--plugin-root", default=str(Path(__file__).resolve().parent.parent))
    parser.add_argument("--replace", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = _args()
    try:
        settings, backup = install(
            _claude_dir(args.claude_config_dir),
            Path(args.plugin_root).expanduser().resolve(),
            args.replace,
        )
    except FileExistsError as exc:
        print(f"CONFLICT: {exc}", file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:
        print(f"ERROR: unable to configure Claude Board status line: {exc}", file=sys.stderr)
        return 1
    print(f"Configured Claude Board status line in {settings}")
    if backup:
        print(f"Backup: {backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
