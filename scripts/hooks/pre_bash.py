#!/usr/bin/env python3
"""
pre_bash.py — PreToolUse hook for the Bash tool.

Enforces:
  - BLOCK: git push from VAULT SUBPROCESS AGENTS (the workers spawned by
    ai_dougs.py to do execution work). Subprocess agents are scoped to the
    user's project work and must NEVER auto-push to remotes.
  - ALLOW: git push from the orchestrator session (no VAULT_SUBPROCESS_AGENT
    env var). The orchestrator deliberately drives vault-side commits +
    pushes (initial repo setup, post-task commits, etc.).
  - LOG:   (legacy) attempted to log to agent_trace.md; that writer is
           gone but the no-op stays for backward compat with the hook
           contract.

Distinguishes contexts via the same VAULT_SUBPROCESS_AGENT=1 env var that
ai_dougs.py injects when spawning AI CLI subprocesses (claude/codex/gemini/
agent). pre_read.py uses the same pattern. Env vars are per-process —
subprocess agent inherits the flag from its parent (the spawn call); the
orchestrator session does not have it set.

Claude Code sends JSON via stdin. Exit 2 + print = block with reason shown to Claude.
Exit 0 = allow.
"""
import json
import os
import sys
from pathlib import Path
from datetime import datetime


BLOCKED_PATTERNS = [
    "git push",
    "git push --force",
    "git push -f",
    "git push origin",
]


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)  # malformed payload → allow

    command = payload.get("tool_input", {}).get("command", "")
    cmd_lower = command.lower()

    # ── Block git push ONLY for vault subprocess agents ─────────────────────────
    # See module docstring: orchestrator pushes are deliberate; subprocess
    # agent pushes would be auto-push-of-user-projects (the failure mode).
    is_subprocess_agent = os.environ.get("VAULT_SUBPROCESS_AGENT") == "1"
    if is_subprocess_agent:
        for pattern in BLOCKED_PATTERNS:
            if pattern in cmd_lower:
                print(
                    "BLOCKED: git push from a vault subprocess agent is not permitted.\n"
                    "Subprocess agents work on the USER'S code and must NEVER auto-push\n"
                    "to remotes. Commit your work; the human (or orchestrator-Claude on\n"
                    "their behalf) will handle the push.",
                    flush=True,
                )
                sys.exit(2)

    # agent_trace.md writer removed 2026-05-12; no logging hook here anymore.
    sys.exit(0)


def _find_vault_root():
    for p in [Path.cwd(), *Path.cwd().parents]:
        if (p / "ai_main.md").exists():
            return p
    return None


if __name__ == "__main__":
    main()
