#!/usr/bin/env python3
"""Bump version across all CLI manifests under a plugin directory.

Iterates <plugin>/.{claude,codex,opencode}-plugin/plugin.json and writes the new
version. Designed for `make bump-version PLUGIN=<name>` — see ../Makefile.

Usage:
    bump_plugin_version.py --plugin <name> [--level patch|minor|major]
    bump_plugin_version.py --plugin <name> --version <x.y.z>
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_RELS = (
    ".claude-plugin/plugin.json",
    ".codex-plugin/plugin.json",
    ".opencode/plugin.json",
)


def parse_version(v: str) -> tuple[int, int, int]:
    parts = v.split(".")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        raise ValueError(f"Invalid semver (need x.y.z all-digits): {v}")
    return tuple(int(p) for p in parts)  # type: ignore[return-value]


def bump(v: tuple[int, int, int], level: str) -> tuple[int, int, int]:
    major, minor, patch = v
    if level == "major":
        return (major + 1, 0, 0)
    if level == "minor":
        return (major, minor + 1, 0)
    if level == "patch":
        return (major, minor, patch + 1)
    raise ValueError(f"Unknown level: {level}")


def fmt(v: tuple[int, int, int]) -> str:
    return ".".join(str(x) for x in v)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--plugin", required=True, help="plugin directory name (e.g. devloop)")
    parser.add_argument(
        "--level", default="patch", choices=["patch", "minor", "major"],
        help="bump level when --version not given (default: patch)",
    )
    parser.add_argument("--version", default=None, help="explicit semver, overrides --level")
    args = parser.parse_args(argv)

    plugin_dir = REPO_ROOT / args.plugin
    if not plugin_dir.is_dir():
        print(f"ERROR: plugin directory not found: {plugin_dir}", file=sys.stderr)
        return 1

    manifests = [plugin_dir / rel for rel in MANIFEST_RELS if (plugin_dir / rel).exists()]
    if not manifests:
        print(f"ERROR: no plugin.json found under {plugin_dir}", file=sys.stderr)
        return 1

    current_versions = []
    for p in manifests:
        v = json.loads(p.read_text(encoding="utf-8")).get("version") or "0.0.0"
        current_versions.append(parse_version(v))

    if args.version:
        parse_version(args.version)  # validate
        new_version = args.version
    else:
        if len(set(current_versions)) > 1:
            divergent = [fmt(v) for v in current_versions]
            print(
                f"WARNING: divergent versions across manifests {divergent}; "
                f"using max as bump basis",
                file=sys.stderr,
            )
        basis = max(current_versions)
        new_version = fmt(bump(basis, args.level))

    print(f"Bumping plugin '{args.plugin}' → {new_version}")
    for p, old in zip(manifests, current_versions):
        data = json.loads(p.read_text(encoding="utf-8"))
        data["version"] = new_version
        p.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"  {p.relative_to(REPO_ROOT)}: {fmt(old)} → {new_version}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
