"""Frontmatter sync — write LangGraph state back to the task file's YAML
frontmatter so Obsidian (Bases / Dataview / file explorer) sees the current
phase + status.

Bug T fix (2026-05-11): the LangGraph checkpointer was the source of truth
for runtime state, but the task file's YAML frontmatter never got updated
after the initial write. Result: `vault.py list` and `vault status` showed
correct data, but Obsidian Bases queries (which read frontmatter directly)
showed every task perpetually `pending_ai_planning`. Also caused at least
one stale background-poll on auto_0074 (waited for a status change that
never landed on disk).

Design:
- Pure helper, no side effects on state — only the disk file.
- Updates a fixed set of frontmatter keys (`status`, `current_phase`,
  `execution_attempts`, `judge_verdict`, `judge_score_avg`,
  `plan_review_verdict`). Leaves all other keys untouched.
- Defensive: never raises into the caller; logs to stderr-equivalent if
  the file is missing or corrupt.
- Idempotent: writing the same state twice produces the same file bytes.
- Only touches the FRONTMATTER block (between the leading `---` markers).
  Body content (the agent's plan, execution log, etc.) is preserved.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any


# Keys we sync from state → frontmatter. Other frontmatter keys (title,
# created, sharpener_model, app_location) are USER-OWNED or set once at
# task creation; the sync never overwrites them.
_SYNCED_KEYS = (
    "status",
    "current_phase",
    "execution_attempts",
)

# Compound keys derived from state.judge_verdict / state.plan_review_verdict
# (LangGraph stores them as dicts; we project the key facts into scalars).
_DERIVED_KEYS = (
    "judge_verdict",
    "judge_score_avg",
    "plan_review_verdict",
    "plan_review_score_avg",
)


def _format_scalar(value: Any) -> str:
    """Render a Python value as a YAML scalar for inline frontmatter."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    s = str(value)
    # Quote if the value contains characters that would confuse YAML parsing
    if any(ch in s for ch in (":", "#", "'", '"', "\n", "[", "]", "{", "}", ",")):
        # Escape internal quotes by switching to single quotes (YAML convention)
        return "'" + s.replace("'", "''") + "'"
    return s


def _projected_updates(state: dict) -> dict[str, str]:
    """Project the LangGraph state into the flat frontmatter key/value pairs
    we want to keep in sync. Returns only keys whose state value is set."""
    out: dict[str, str] = {}
    for k in _SYNCED_KEYS:
        if k in state and state[k] is not None and state[k] != "":
            out[k] = _format_scalar(state[k])
    # Project judge_verdict.verdict + score_avg
    jv = state.get("judge_verdict") or {}
    if isinstance(jv, dict):
        verdict = jv.get("verdict") or ""
        if verdict:
            out["judge_verdict"] = _format_scalar(verdict)
        score_avg = jv.get("score_avg")
        if score_avg is not None and score_avg != 0:
            out["judge_score_avg"] = _format_scalar(score_avg)
    # Project plan_review_verdict.verdict + score_avg
    prv = state.get("plan_review_verdict") or {}
    if isinstance(prv, dict):
        verdict = prv.get("verdict") or ""
        if verdict:
            out["plan_review_verdict"] = _format_scalar(verdict)
        score_avg = prv.get("score_avg")
        if score_avg is not None and score_avg != 0:
            out["plan_review_score_avg"] = _format_scalar(score_avg)
    return out


def sync_task_file_frontmatter(state: dict) -> bool:
    """Update the task file's YAML frontmatter to match LangGraph state.

    Returns True if the file was written, False otherwise (path missing,
    read failed, no changes needed, etc.). Never raises — defensive by
    design because frontmatter sync is observability, not control flow.
    """
    task_file_path = state.get("task_file_path", "") or ""
    if not task_file_path:
        return False
    path = Path(task_file_path)
    if not path.exists() or not path.is_file():
        return False
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False

    updates = _projected_updates(state)
    if not updates:
        return False

    # Locate the frontmatter block. Must start with --- on line 1 and end
    # with --- on its own line. If absent we don't touch the file (the
    # task file format requires it; absence is an upstream bug).
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.S)
    if not m:
        return False
    fm_body = m.group(1)
    new_body = _patch_frontmatter_body(fm_body, updates)
    if new_body == fm_body:
        return False
    new_text = "---\n" + new_body + "\n---\n" + text[m.end():]
    try:
        path.write_text(new_text, encoding="utf-8")
    except OSError:
        return False
    return True


def _patch_frontmatter_body(body: str, updates: dict[str, str]) -> str:
    """Apply key=value updates to a frontmatter body. Preserves order of
    existing keys; appends new keys at the end."""
    lines = body.splitlines()
    seen: set[str] = set()
    out_lines: list[str] = []
    for line in lines:
        # Match `key:` or `key: value` — allow leading whitespace zero
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:(.*)$", line)
        if not m:
            out_lines.append(line)
            continue
        key = m.group(1)
        if key in updates and key not in seen:
            out_lines.append(f"{key}: {updates[key]}")
            seen.add(key)
        else:
            out_lines.append(line)
    # Append any new keys that weren't already present
    for key, value in updates.items():
        if key not in seen:
            out_lines.append(f"{key}: {value}")
    return "\n".join(out_lines)
