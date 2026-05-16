"""Content diff via .backups tarball — feeds real before-vs-after content to
the cross-model judge for vault-infra tasks.

Bug S fix (2026-05-11): the judge previously received `combined_diff` which
for vault-infra tasks was just synthetic `diff --git a/path b/path` lines
(declaration only, no content). Result: judge concluded "no work happened"
even when files were materially changed. Surfaced 3 times in a row on
auto_0066, auto_0070, auto_0071.

Strategy:
- Vault execution writes a pre-execution backup to
  `<vault_root>/.backups/<safe_task_name>_<YYYY-MM-DD_HHMMSS>.tar.gz` containing
  the whole vault (minus caches / node_modules / .backups).
- After execution, we know which paths the agent touched (mtime walk -
  orchestrator writes, computed in execution.py around line 317).
- For each touched path, extract the pre-execution bytes from the most
  recent tarball, read the current bytes from disk, run difflib.unified_diff,
  and concatenate per-file diffs into one blob the judge can read.

The result is appended to `combined_diff` so existing judge wiring (Bug L
fix) consumes it without changes. Truncated overall to ~6000 chars to match
the judge prompt's diff slot budget.
"""
from __future__ import annotations

import difflib
import re
import tarfile
from pathlib import Path

from .vault_backup import _safe_task_name


# Per-file content cap so a single 4000-line file doesn't eat the whole
# judge-diff budget. Files larger than this are truncated with a marker.
_PER_FILE_CHAR_CAP = 1500

# Whole-output cap. Above this we stop adding files and append a count.
_TOTAL_CHAR_CAP = 6000

# Binary-extension prefilter — these never produce useful unified diffs and
# their content can break PyYAML / cost the judge tokens for no signal.
_BINARY_SUFFIXES = {
    ".tar", ".gz", ".zip", ".7z", ".bz2", ".xz",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".bmp",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx",
    ".pyc", ".pyo", ".so", ".dll", ".dylib",
    ".db", ".sqlite", ".sqlite3",
    ".mp3", ".mp4", ".wav", ".avi", ".mov",
}


def find_latest_backup(vault_root: Path, task_name: str) -> Path | None:
    """Locate the most recent backup tarball for `task_name` in
    `<vault_root>/.backups/`. Returns None if none found."""
    backup_dir = Path(vault_root) / ".backups"
    if not backup_dir.exists():
        return None
    safe = _safe_task_name(task_name)
    # File pattern: <safe_task_name>_YYYY-MM-DD_HHMMSS.tar.gz
    candidates = sorted(
        (p for p in backup_dir.glob(f"{safe}_*.tar.gz") if p.is_file()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def _looks_binary_path(rel_path: str) -> bool:
    suffix = Path(rel_path).suffix.lower()
    return suffix in _BINARY_SUFFIXES


def _read_pre_execution_bytes(tar: tarfile.TarFile, vault_name: str,
                              rel_path: str) -> bytes | None:
    """Look up rel_path in the tarball (arcname-prefixed with vault dir name)
    and return its raw bytes. Returns None if the member doesn't exist
    (i.e. the path was created by the agent, not pre-existing)."""
    # The backup is created with `arcname=root.name`, so members are like
    # 'vault/scripts/foo.py' regardless of the actual vault directory.
    candidates = (
        f"{vault_name}/{rel_path}",
        rel_path,                       # in case some backups didn't use arcname
        f"./{rel_path}",
    )
    for cand in candidates:
        try:
            member = tar.getmember(cand)
        except KeyError:
            continue
        if not member.isfile():
            continue
        f = tar.extractfile(member)
        if f is None:
            continue
        try:
            return f.read()
        except OSError:
            return None
    return None


def _decode_text(data: bytes) -> str | None:
    """Best-effort UTF-8 decode. Returns None if the data is obviously
    binary (lots of NUL bytes)."""
    if data is None:
        return None
    # Quick binary sniff: NUL byte in first 4KB → binary
    sniff = data[:4096]
    if b"\x00" in sniff:
        return None
    try:
        return data.decode("utf-8", errors="replace")
    except Exception:
        return None


def compute_content_diff_from_backup(vault_root: Path | str,
                                     task_name: str,
                                     paths: list[str]) -> str:
    """For each path in `paths`, extract pre-execution bytes from the most
    recent backup tarball and unified-diff against the current file. Return
    a single text blob suitable for appending to combined_diff.

    Returns "" when no backup is found or no path produced a useful diff
    (callers should fall back to the existing combined_diff content).
    """
    vault = Path(vault_root).resolve()
    backup_path = find_latest_backup(vault, task_name)
    if backup_path is None:
        return ""
    vault_name = vault.name  # 'vault' typically; matches the tar arcname

    sections: list[str] = []
    total_chars = 0
    files_added = 0
    files_skipped_binary = 0
    files_no_pre = 0
    files_no_change = 0
    try:
        with tarfile.open(backup_path, "r:gz") as tar:
            for rel in paths:
                if total_chars >= _TOTAL_CHAR_CAP:
                    break
                rel_norm = rel.replace("\\", "/")
                if _looks_binary_path(rel_norm):
                    files_skipped_binary += 1
                    continue
                pre_bytes = _read_pre_execution_bytes(tar, vault_name, rel_norm)
                # Current file bytes
                current_path = vault / rel_norm
                try:
                    cur_bytes = current_path.read_bytes() if current_path.exists() else b""
                except OSError:
                    cur_bytes = b""
                pre_text = _decode_text(pre_bytes) if pre_bytes is not None else ""
                cur_text = _decode_text(cur_bytes) if cur_bytes else ""
                if pre_text is None or cur_text is None:
                    files_skipped_binary += 1
                    continue
                if pre_bytes is None:
                    pre_text = ""  # file is new (didn't exist pre-execution)
                    files_no_pre += 1
                if pre_text == cur_text:
                    files_no_change += 1
                    continue
                diff_iter = difflib.unified_diff(
                    pre_text.splitlines(keepends=True),
                    cur_text.splitlines(keepends=True),
                    fromfile=f"a/{rel_norm} (pre-execution)",
                    tofile=f"b/{rel_norm} (post-execution)",
                    n=3,  # 3 lines of context, matches git's default
                )
                file_diff = "".join(diff_iter)
                if not file_diff:
                    files_no_change += 1
                    continue
                if len(file_diff) > _PER_FILE_CHAR_CAP:
                    file_diff = (file_diff[:_PER_FILE_CHAR_CAP]
                                 + f"\n... (file diff truncated; {len(file_diff)} chars total)\n")
                sections.append(file_diff)
                total_chars += len(file_diff)
                files_added += 1
    except (tarfile.TarError, OSError):
        return ""

    if not sections:
        # Nothing useful; surface a one-line summary so the judge still
        # knows something happened
        if files_added == 0 and (files_no_pre or files_no_change or files_skipped_binary):
            return (f"## Content diff summary (from {backup_path.name})\n"
                    f"- new files: {files_no_pre}\n"
                    f"- unchanged: {files_no_change}\n"
                    f"- binary skipped: {files_skipped_binary}\n")
        return ""

    header = (f"## Content diff vs pre-execution backup "
              f"({backup_path.name}, {files_added} file(s))\n\n")
    return header + "".join(sections)
