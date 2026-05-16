#!/usr/bin/env python3
"""pre_read.py — PreToolUse hook for the Read tool (and Glob/Grep).

Mechanically blocks subprocess agents (VAULT_SUBPROCESS_AGENT=1) from
reading anything under `_human_notes/`. The orchestrator session is
unaffected — this hook is a no-op when VAULT_SUBPROCESS_AGENT is unset.

Why it exists (2026-05-09):
  - SessionStart fires for `claude -p` subprocesses (empirically confirmed
    2026-05-08).
  - The reminder text in session_start_notes_reminder.py is now suppressed
    for subprocess agents (gated on VAULT_SUBPROCESS_AGENT), so they don't
    SEE the pointer to _human_notes/.
  - But that gate is prevention, not enforcement. If the gate ever
    regresses, OR if a subprocess agent independently discovers
    _human_notes/ via grep/glob/ls, it could still read the file. This
    hook is the mechanical-refusal layer: even if the agent tries, the
    Read fails with a clear explanation.

  Defense-in-depth: SessionStart gate + Read gate + diff EXCLUDE_DIRS.

Exit 2 + print = block (reason shown to Claude).
Exit 0 = allow.
"""
import json
import os
import sys
from pathlib import Path


HUMAN_ONLY_PREFIX = "_human_notes"


def main():
    # Only enforce on subprocess agents — orchestrator session is unaffected.
    if os.environ.get("VAULT_SUBPROCESS_AGENT") != "1":
        sys.exit(0)

    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)  # malformed payload → allow (best-effort)

    tool_input = payload.get("tool_input", {}) or {}

    # Read tool uses 'file_path'; Glob uses 'pattern' + optional 'path';
    # Grep uses 'pattern' + 'path'/'glob'. Check whichever applies.
    paths_to_check: list[str] = []
    for key in ("file_path", "path", "notebook_path"):
        v = tool_input.get(key)
        if isinstance(v, str) and v:
            paths_to_check.append(v)
    pattern = tool_input.get("pattern") or ""
    if isinstance(pattern, str) and HUMAN_ONLY_PREFIX in pattern:
        paths_to_check.append(pattern)
    glob = tool_input.get("glob") or ""
    if isinstance(glob, str) and HUMAN_ONLY_PREFIX in glob:
        paths_to_check.append(glob)

    for raw in paths_to_check:
        norm = str(raw).replace("\\", "/").lower()
        if f"/{HUMAN_ONLY_PREFIX}/" in f"/{norm}/" or norm.startswith(f"{HUMAN_ONLY_PREFIX}/"):
            print(
                f"BLOCKED: {raw!r} is inside `_human_notes/` — the orchestrator's "
                f"HUMAN-ONLY zone. Vault subprocess agents must NEVER read this "
                f"directory (it contains the orchestrating session's notes, plans, "
                f"and human-owned vision content). If you need information about "
                f"how the vault works, read `VISION.md`, `ai_main.md`, or files "
                f"under `ai_context/` instead. This block is mechanical — change "
                f"your approach rather than retrying.",
                flush=True,
            )
            sys.exit(2)

    sys.exit(0)


if __name__ == "__main__":
    main()
