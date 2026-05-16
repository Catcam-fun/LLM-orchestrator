#!/usr/bin/env python3
"""
pre_write.py — PreToolUse hook for Write and Edit tools.

Backstop, not primary design. The intended design is that subprocess
agents only ever work on user projects (per ai_main.md + the task's
allowlist) and therefore never have a reason to write outside their
scope. This hook exists to mechanically refuse anyway, in case an
agent gets the wrong idea.

Enforces file scope from scripts/hooks/.scope.json (written by ai_dougs
at execution start, cleared when execution ends).

Scope file fields:
  files_blocked: list of path prefixes (relative to vault or app root) to deny
  files_allowed: list of path prefixes (relative to vault or app root) to allow
                 — if non-empty, writes OUTSIDE these paths are also denied

Subprocess deny-by-default: when VAULT_SUBPROCESS_AGENT=1 AND no .scope.json
is active (non-execution phases: planning, plan_review, refinement,
learning, judge), the hook denies ALL writes. The orchestrator session
(no env var set) is unaffected.

Exit 2 + print = block (reason shown to Claude).
Exit 0 = allow.
"""
import json
import os
import sys
from pathlib import Path


SCOPE_CANDIDATES = [
    Path(__file__).parent / ".scope.json",
    Path.cwd() / "scripts" / "hooks" / ".scope.json",
]


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    tool_input = payload.get("tool_input", {})
    file_path_str = tool_input.get("file_path", "") or tool_input.get("path", "")
    if not file_path_str:
        sys.exit(0)

    try:
        file_path = Path(file_path_str).resolve()
    except Exception:
        sys.exit(0)

    scope = _load_scope()
    if not scope:
        # Subprocess agents (VAULT_SUBPROCESS_AGENT=1) are denied by default
        # when no scope is active. They MUST run with explicit scope. The
        # orchestrator session (env var unset) keeps the old permissive
        # behavior so the user can edit anywhere from their Claude Code
        # session.
        if os.environ.get("VAULT_SUBPROCESS_AGENT") == "1":
            print(
                f"BLOCKED: subprocess agent attempted to write `{file_path}` "
                f"with no active .scope.json.\n"
                f"  Subprocess agents are deny-by-default during non-execution "
                f"phases (planning, plan_review, refinement, learning, judge).\n"
                f"  If this is execution work, the orchestrator should have "
                f"set scope via _writeHookScope BEFORE invoking the agent.\n"
                f"  This hook closes the gap where misbehaving subprocesses "
                f"could modify vault scripts during non-execution phases.",
                flush=True,
            )
            sys.exit(2)
        sys.exit(0)  # Orchestrator or other context with no scope → no restrictions

    files_blocked = scope.get("files_blocked", [])
    files_allowed = scope.get("files_allowed", [])
    vault_root = scope.get("vault_root", "")
    app_root = scope.get("app_root", "")

    vault_path = Path(vault_root).resolve() if vault_root else None
    app_path = Path(app_root).resolve() if app_root else None

    # ── Blocked path check ──────────────────────────────────────────────────────
    for blocked in files_blocked:
        if not blocked:
            continue
        for base in [p for p in [vault_path, app_path] if p]:
            candidate = (base / blocked).resolve()
            if _is_subpath(file_path, candidate):
                print(
                    f"BLOCKED: Write to `{file_path}` is denied.\n"
                    f"  Blocked path: `{blocked}`\n"
                    f"  This file is explicitly off-limits for this task.\n"
                    f"  Do not touch the backend or any other blocked directory.",
                    flush=True,
                )
                sys.exit(2)
        # String-match fallback (handles platform path differences)
        norm = str(file_path).replace("\\", "/")
        if blocked.replace("\\", "/") in norm:
            print(
                f"BLOCKED: Write to `{file_path}` matches blocked pattern `{blocked}`.\n"
                f"  Stay within the task's allowed scope.",
                flush=True,
            )
            sys.exit(2)

    # ── Allowed path check (if specified) ──────────────────────────────────────
    if files_allowed:
        permitted = False
        for allowed in files_allowed:
            if not allowed:
                continue
            for base in [p for p in [vault_path, app_path] if p]:
                candidate = (base / allowed).resolve()
                if _is_subpath(file_path, candidate):
                    permitted = True
                    break
            if permitted:
                break
            # String-match fallback
            norm = str(file_path).replace("\\", "/")
            if allowed.replace("\\", "/") in norm:
                permitted = True
                break

        if not permitted:
            print(
                f"BLOCKED: Write to `{file_path}` is outside the allowed scope.\n"
                f"  Allowed paths: {files_allowed}\n"
                f"  Only write to files within the approved scope for this task.",
                flush=True,
            )
            sys.exit(2)
    else:
        # Bug AA fix (2026-05-11): when files_allowed is empty AND the writer
        # is a subprocess agent, restrict writes to the app_root (or vault_root
        # if no app_root is set). Without this, an empty files_allowed list
        # was treated as "anything goes" — and the agent could write to
        # `.claude/settings.json` or anywhere else outside the project.
        # Self-test caught this case during auto_0076 by detecting the
        # vault-tracked .claude/settings.json modification, but the right
        # place to deny is here at the source.
        if os.environ.get("VAULT_SUBPROCESS_AGENT") == "1":
            # Determine the implicit allowed root: app_root if set, else vault_root.
            implicit_root = app_path if app_path else vault_path
            if implicit_root and not _is_subpath(file_path, implicit_root):
                print(
                    f"BLOCKED: subprocess agent attempted to write `{file_path}` "
                    f"outside the implicit task root.\n"
                    f"  files_allowed was empty (no explicit allowlist).\n"
                    f"  Implicit root: `{implicit_root}`\n"
                    f"  Empty allowlist + subprocess agent = writes must stay "
                    f"inside the app/vault root. If this task legitimately "
                    f"needs to write elsewhere, the orchestrator should set "
                    f"files_allowed explicitly when writing the scope.json.",
                    flush=True,
                )
                sys.exit(2)

    sys.exit(0)


def _is_subpath(path: Path, parent: Path) -> bool:
    """Return True if path is inside parent (or is parent itself)."""
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _load_scope() -> dict:
    for candidate in SCOPE_CANDIDATES:
        if candidate.exists():
            try:
                data = json.loads(candidate.read_text(encoding="utf-8"))
                if data and isinstance(data, dict):
                    return data
            except Exception:
                pass
    return {}


if __name__ == "__main__":
    main()
