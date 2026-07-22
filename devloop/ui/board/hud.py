"""Fixed three-line terminal projection over a presentation-neutral Board snapshot."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Iterable


class HudTone(str, Enum):
    NORMAL = "normal"
    DIM = "dim"
    TITLE = "title"
    INFO = "info"
    SUCCESS = "success"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True)
class HudSegment:
    text: str
    tone: HudTone = HudTone.NORMAL
    priority: int = 5


@dataclass(frozen=True)
class HudPulse:
    text: str
    tone: HudTone = HudTone.DIM
    occurred_at: float | None = None


@dataclass(frozen=True)
class HudFrame:
    """Three semantic slots: context, current health, and latest transient pulse."""

    context: tuple[HudSegment, ...]
    health: tuple[HudSegment, ...]
    pulse: HudPulse


_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_ANSI_SGR_RE = re.compile(r"\x1b\[[0-9;]*m")
_ANSI = {
    HudTone.DIM: "\x1b[2m",
    HudTone.TITLE: "\x1b[1m",
    HudTone.INFO: "\x1b[36m",
    HudTone.SUCCESS: "\x1b[32m",
    HudTone.WARNING: "\x1b[33m",
    HudTone.ERROR: "\x1b[31m",
}
_RESET = "\x1b[0m"


def _clean(value: object) -> str:
    return _CONTROL_RE.sub("", _ANSI_SGR_RE.sub("", str(value or ""))).strip()


def _items(snapshot: dict) -> dict[str, dict]:
    rows = snapshot.get("items") if isinstance(snapshot, dict) else None
    if not isinstance(rows, list):
        return {}
    return {
        str(item.get("type")): item
        for item in rows
        if isinstance(item, dict) and item.get("type")
    }


def _payload(item: dict | None) -> dict:
    payload = item.get("payload") if isinstance(item, dict) else None
    return payload if isinstance(payload, dict) else {}


def _context_segments(snapshot: dict, items: dict[str, dict]) -> tuple[HudSegment, ...]:
    out = [HudSegment("[Board]", HudTone.TITLE, 0)]
    workspace = _payload(items.get("workspace"))
    workspace_root = _clean(workspace.get("root") or snapshot.get("root"))
    if workspace_root:
        out.append(HudSegment(f"ws:{Path(workspace_root).name}", HudTone.DIM, 3))
    requirement = _clean(_payload(items.get("requirement.current")).get("text"))
    if requirement:
        requirement = re.sub(r"^Requirement:\s*", "", requirement)
        out.append(HudSegment(f"req:{requirement}", HudTone.NORMAL, 1))

    identity = _payload(items.get("repo.identity"))
    code_dir = _clean(identity.get("code_dir"))
    if code_dir:
        out.append(HudSegment(f"repo:{Path(code_dir).name}", HudTone.INFO, 0))
    elif not workspace_root:
        root = _clean(snapshot.get("root"))
        out.append(HudSegment(f"workspace:{Path(root).name}" if root else "no workspace", HudTone.DIM, 2))
    return tuple(out)


def _validation_segment(item: dict | None) -> HudSegment:
    components = _payload(item).get("components")
    if not isinstance(components, list) or not components:
        return HudSegment("validation:never", HudTone.DIM, 2)
    lint_ok = all(isinstance(row, dict) and row.get("lint_at") for row in components)
    test_ok = all(isinstance(row, dict) and row.get("test_at") for row in components)
    if lint_ok and test_ok:
        return HudSegment("validation:✓", HudTone.SUCCESS, 1)
    parts = []
    if lint_ok:
        parts.append("lint✓")
    if test_ok:
        parts.append("test✓")
    return HudSegment("validation:" + ("/".join(parts) or "pending"), HudTone.WARNING, 1)


def _health_segments(items: dict[str, dict]) -> tuple[HudSegment, ...]:
    out: list[HudSegment] = []
    identity = _payload(items.get("repo.identity"))
    if not identity:
        subprojects = _payload(items.get("workspace")).get("subprojects")
        count = len(subprojects) if isinstance(subprojects, list) else 0
        return (
            HudSegment("focus:workspace", HudTone.DIM, 0),
            HudSegment(f"repos:{count}", HudTone.NORMAL, 1),
        )
    branch = _clean(identity.get("branch"))
    if branch:
        out.append(HudSegment(branch, HudTone.INFO, 0))
    modified = int(identity.get("modified_count") or 0)
    untracked = int(identity.get("untracked_count") or 0)
    dirty = modified + untracked
    out.append(HudSegment(f"dirty:{dirty}" if dirty else "clean", HudTone.WARNING if dirty else HudTone.SUCCESS, 2))
    ahead = int(identity.get("ahead") or 0)
    behind = int(identity.get("behind") or 0)
    if ahead or behind:
        out.append(HudSegment(f"↑{ahead} ↓{behind}", HudTone.DIM, 4))
    pr_label = _clean(identity.get("pr_label"))
    pr_state = _clean(identity.get("pr_state"))
    if pr_label:
        out.append(HudSegment(f"{pr_label}:{pr_state or '?'}", HudTone.NORMAL, 1))

    blocked = _payload(items.get("repo.pr-blocked"))
    if blocked:
        out.append(HudSegment(f"BLOCKED:{_clean(blocked.get('readiness'))}", HudTone.ERROR, 0))
    out.append(_validation_segment(items.get("repo.validation")))

    review = _payload(items.get("repo.review"))
    if review:
        status = _clean(review.get("status")) or "?"
        findings = int(review.get("findings") or 0)
        if status in {"error", "stale"}:
            tone = HudTone.ERROR
        elif status == "running":
            tone = HudTone.INFO
        elif findings:
            tone = HudTone.WARNING
        else:
            tone = HudTone.SUCCESS
        detail = f"{findings} findings" if findings else status
        out.append(HudSegment(f"review:{detail}", tone, 1))
    pending = int(_payload(items.get("repo.review-label")).get("pending") or 0)
    if pending:
        out.append(HudSegment(f"labels:{pending}", HudTone.WARNING, 3))
    return tuple(out)


def _revision(item: dict) -> str:
    revision = item.get("revision")
    return str(revision) if revision else repr(item.get("payload"))


class HudPulseTracker:
    """Remember the latest visible change in-process; newer Board revisions replace it."""

    def __init__(self):
        self._items: dict[str, dict] | None = None
        self.current = HudPulse("watching Board", HudTone.DIM)

    def observe(self, items: dict[str, dict], now: float | None = None) -> HudPulse:
        if self._items is None:
            self._items = items
            return self.current

        changed: list[tuple[int, HudPulse]] = []
        for item_type in set(self._items) | set(items):
            before = self._items.get(item_type)
            after = items.get(item_type)
            if before is not None and after is not None and _revision(before) == _revision(after):
                continue
            pulse = _pulse_for_change(item_type, before, after, now)
            if pulse:
                changed.append((_pulse_priority(item_type), pulse))
        self._items = items
        if changed:
            self.current = min(changed, key=lambda row: row[0])[1]
        return self.current


def _pulse_priority(item_type: str) -> int:
    return {
        "repo.pr-blocked": 0,
        "repo.review": 1,
        "repo.validation": 2,
        "repo.identity": 3,
        "requirement.current": 4,
        "repo.review-label": 5,
    }.get(item_type, 9)


def _pulse_for_change(
    item_type: str,
    before: dict | None,
    after: dict | None,
    now: float | None,
) -> HudPulse | None:
    old, new = _payload(before), _payload(after)
    at = datetime.now().timestamp() if now is None else now
    if item_type == "repo.pr-blocked":
        return HudPulse(
            f"merge blocked · {_clean(new.get('readiness'))}" if after else "merge blocker cleared",
            HudTone.ERROR if after else HudTone.SUCCESS,
            at,
        )
    if item_type == "repo.review":
        if not after:
            return HudPulse("review state cleared", HudTone.DIM, at)
        findings = int(new.get("findings") or 0)
        status = _clean(new.get("status")) or "updated"
        text = f"review {status} · {findings} findings" if findings else f"review {status}"
        tone = HudTone.ERROR if status in {"error", "stale"} else HudTone.WARNING if findings else HudTone.SUCCESS
        return HudPulse(text, tone, at)
    if item_type == "repo.validation":
        segment = _validation_segment(after)
        return HudPulse(segment.text.replace(":", " ", 1), segment.tone, at)
    if item_type == "repo.identity":
        old_branch, new_branch = _clean(old.get("branch")), _clean(new.get("branch"))
        if old_branch != new_branch:
            return HudPulse(f"switched {old_branch or '?'} → {new_branch or '?'}", HudTone.INFO, at)
        old_dirty = int(old.get("modified_count") or 0) + int(old.get("untracked_count") or 0)
        new_dirty = int(new.get("modified_count") or 0) + int(new.get("untracked_count") or 0)
        if old_dirty != new_dirty:
            return HudPulse(f"working tree changed · {new_dirty} files", HudTone.WARNING if new_dirty else HudTone.SUCCESS, at)
        if old.get("pr_state") != new.get("pr_state"):
            return HudPulse(f"{_clean(new.get('pr_label'))} became {_clean(new.get('pr_state'))}", HudTone.INFO, at)
        return HudPulse("repository state updated", HudTone.DIM, at)
    if item_type == "requirement.current":
        if not after:
            return HudPulse("requirement closed", HudTone.SUCCESS, at)
        text = re.sub(r"^Requirement:\s*", "", _clean(new.get("text")))
        return HudPulse(f"requirement focused · {text}", HudTone.INFO, at)
    if item_type == "repo.review-label":
        return HudPulse(f"review labels pending · {int(new.get('pending') or 0)}", HudTone.WARNING, at)
    return None


def frame_from_snapshot(
    snapshot: dict,
    tracker: HudPulseTracker | None = None,
    now: float | None = None,
) -> HudFrame:
    items = _items(snapshot)
    pulse = tracker.observe(items, now) if tracker else HudPulse("watching Board", HudTone.DIM)
    return HudFrame(
        context=_context_segments(snapshot, items),
        health=_health_segments(items),
        pulse=pulse,
    )


def _display_width(text: str) -> int:
    return sum(
        0 if unicodedata.combining(char) else 2 if unicodedata.east_asian_width(char) in {"F", "W"} else 1
        for char in text
    )


def _truncate(text: str, width: int) -> str:
    if _display_width(text) <= width:
        return text
    if width <= 1:
        return "…"[:width]
    out = ""
    for char in text:
        if _display_width(out + char) > width - 1:
            break
        out += char
    return out.rstrip() + "…"


def _fit_segments(segments: Iterable[HudSegment], width: int) -> list[HudSegment]:
    selected = [segment for segment in segments if _clean(segment.text)]
    width = max(1, width)
    while len(selected) > 1 and _display_width(" · ".join(_clean(item.text) for item in selected)) > width:
        worst = max(range(len(selected)), key=lambda index: (selected[index].priority, index))
        selected.pop(worst)
    if not selected:
        return [HudSegment("")]
    plain = " · ".join(_clean(item.text) for item in selected)
    if _display_width(plain) <= width:
        return selected
    first = selected[0]
    return [HudSegment(_truncate(_clean(first.text), width), first.tone, first.priority)]


def _paint(segment: HudSegment, color: bool) -> str:
    text = _clean(segment.text)
    code = _ANSI.get(segment.tone) if color else None
    return f"{code}{text}{_RESET}" if code else text


def _render_segments(segments: Iterable[HudSegment], width: int, color: bool) -> str:
    return " · ".join(_paint(item, color) for item in _fit_segments(segments, width))


def render_frame(frame: HudFrame, width: int = 120, color: bool = True) -> str:
    """Render exactly three lines; semantic slots never wrap into one another."""
    pulse_prefix = ""
    if frame.pulse.occurred_at is not None:
        pulse_prefix = datetime.fromtimestamp(frame.pulse.occurred_at).strftime("%H:%M:%S ")
    pulse = HudSegment(pulse_prefix + frame.pulse.text, frame.pulse.tone, 0)
    return "\n".join((
        _render_segments(frame.context, width, color),
        _render_segments(frame.health, width, color),
        _render_segments((pulse,), width, color),
    ))
