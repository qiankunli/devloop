"""AGENTS.md References / Subprojects parsers. Pure stdlib."""
from __future__ import annotations

import os
import re
from pathlib import Path

_LINK_PATTERN = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_BARE_PATH_PATTERN = re.compile(r"`([^`]+\.md)`")


def parse_references_section(agents_md_path: str | Path) -> list[dict]:
    """Parse the `## References` section. Returns list of
    {"title", "path" (absolute), "description"}. Tolerant of several formats."""
    path = Path(agents_md_path)
    if not path.exists():
        return []
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return []

    refs_match = re.search(r"^##\s+References?\s*$", content, re.MULTILINE | re.IGNORECASE)
    if not refs_match:
        return []

    refs_block = content[refs_match.end():]
    next_section = re.search(r"^##\s+", refs_block, re.MULTILINE)
    if next_section:
        refs_block = refs_block[:next_section.start()]

    base_dir = path.parent
    results: list[dict] = []
    for raw_line in refs_block.split("\n"):
        line = raw_line.strip()
        if not line or not line.startswith("-"):
            continue
        body = line.lstrip("- ").strip()
        entry = _parse_one_entry(body, base_dir)
        if entry:
            results.append(entry)
    return results


def _parse_one_entry(body: str, base_dir: Path) -> dict | None:
    link_match = _LINK_PATTERN.search(body)
    if link_match:
        link_text = link_match.group(1).strip()
        link_path = link_match.group(2).strip()
        before = body[:link_match.start()].strip()
        after = body[link_match.end():].strip()
        before = re.sub(r"[:：—\-]+$", "", before).strip()
        after = re.sub(r"^[:：—\-]+", "", after).strip()
        if before:
            title = before
            description = " ".join(filter(None, [link_text if link_text != link_path else "", after])).strip() or link_text
        else:
            title = link_text
            description = after if after else link_text
        abs_path = _resolve_path(link_path, base_dir)
        return {"title": title, "path": abs_path, "description": description}

    bare = _BARE_PATH_PATTERN.search(body)
    if bare:
        link_path = bare.group(1).strip()
        rest = (body[:bare.start()] + body[bare.end():]).strip()
        rest = re.sub(r"[:：—\-]+", " ", rest).strip()
        abs_path = _resolve_path(link_path, base_dir)
        title = Path(link_path).name
        return {"title": title, "path": abs_path, "description": rest or title}
    return None


def _resolve_path(p: str, base_dir: Path) -> str:
    """Resolve relative paths against base_dir; pass through absolute paths.
    Expand `~`; keep `<placeholder>` paths as-is (they're not concrete)."""
    expanded = os.path.expanduser(p)
    if "<" in expanded or ">" in expanded:
        return p
    candidate = Path(expanded)
    if not candidate.is_absolute():
        candidate = (base_dir / candidate).resolve()
    return str(candidate)


def _strip_backticks(s: str) -> str:
    s = s.strip()
    if s.startswith("`") and s.endswith("`"):
        s = s[1:-1]
    return s.strip()


def parse_subprojects_section(agents_md_path: str | Path) -> list[dict]:
    """Parse a subprojects table (## 子项目清单 / ### Subprojects, H2–H4).

    Returns dicts: name / path / aliases / language / role / note.
    """
    path = Path(agents_md_path)
    if not path.exists():
        return []
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return []

    header_pattern = re.compile(
        r"^(#{2,4})\s+(子项目清单|Subprojects?)\s*$", re.MULTILINE | re.IGNORECASE,
    )
    header_match = header_pattern.search(content)
    if not header_match:
        return []

    matched_level = len(header_match.group(1))
    block = content[header_match.end():]
    end_pattern = re.compile(r"^#{2," + str(matched_level) + r"}\s+", re.MULTILINE)
    next_section = end_pattern.search(block)
    if next_section:
        block = block[:next_section.start()]

    table_lines = [ln for ln in block.split("\n") if ln.strip().startswith("|")]
    if len(table_lines) < 3:
        return []

    header_cells = [c.strip() for c in table_lines[0].strip("|").split("|")]
    rows: list[dict] = []
    for row_line in table_lines[2:]:
        cells = [c.strip() for c in row_line.strip("|").split("|")]
        if len(cells) < len(header_cells):
            continue
        first_col = cells[0].replace("`", "").strip() if cells else ""
        parts = [p.strip() for p in first_col.split("/") if p.strip()]
        primary_name = parts[0] if parts else first_col
        extra_names: list[str] = parts[1:] if len(parts) > 1 else []
        entry: dict = {"name": primary_name, "path": primary_name, "aliases": extra_names}
        for header_idx, header_name in enumerate(header_cells):
            lower = header_name.lower()
            val = _strip_backticks(cells[header_idx]) if header_idx < len(cells) else ""
            if "简称" in header_name or "alias" in lower:
                if val:
                    entry["aliases"] = extra_names + [v.strip() for v in val.split("/") if v.strip()]
            elif "语言" in header_name or "language" in lower:
                entry["language"] = val
            elif "角色" in header_name or "role" in lower:
                entry["role"] = val
            elif "备注" in header_name or "note" in lower:
                entry["note"] = val
        if "role" not in entry and entry.get("note"):
            entry["role"] = entry["note"]
        seen: set[str] = {entry["name"]}
        deduped: list[str] = []
        for a in entry.get("aliases", []):
            if a not in seen:
                seen.add(a)
                deduped.append(a)
        entry["aliases"] = deduped
        rows.append(entry)
    return rows
