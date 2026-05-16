#!/usr/bin/env python3
"""
post_write.py — PostToolUse hook for Write and Edit tools.

Logs every file write to ai_context/agent_trace.md for traceability.
"What agent wrote what file, when" — zero-cost audit trail.

Exit 0 always (PostToolUse hooks are non-blocking).
"""
import json
import sys
from pathlib import Path
from datetime import datetime


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    tool_input = payload.get("tool_input", {})
    tool_name = payload.get("tool_name", "Write")
    file_path_str = tool_input.get("file_path", "") or tool_input.get("path", "")

    if not file_path_str:
        sys.exit(0)

    try:
        vault = _find_vault_root()
        if vault:
            trace = vault / "ai_context" / "agent_trace.md"
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            # Relative path from vault root if possible; otherwise mark EXTERNAL.
            # Consumers (self_test._writes_since, audits) MUST be able to tell
            # a vault-relative path apart from an absolute path to elsewhere
            # on disk (e.g. orchestrator writing C:/Users/.../obsidian.json).
            # Latent bug fixed 2026-05-10: previously the except branch
            # silently emitted a `C:/...` path with no marker, which then
            # leaked into _writes_since and tripped audits asserting
            # vault-relative output.
            try:
                rel = str(Path(file_path_str).resolve().relative_to(vault.resolve()))
                rel = rel.replace("\\", "/")
            except ValueError:
                ext = str(file_path_str).replace("\\", "/")
                rel = f"EXTERNAL: {ext}"

            line = f"| {ts} | FILE-WRITE | {tool_name}: {rel} | hook |\n"
            if trace.exists():
                with open(trace, "a", encoding="utf-8") as f:
                    f.write(line)
    except Exception:
        pass

    sys.exit(0)


def _find_vault_root():
    for p in [Path.cwd(), *Path.cwd().parents]:
        if (p / "ai_main.md").exists():
            return p
    return None


if __name__ == "__main__":
    main()
