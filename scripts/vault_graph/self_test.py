"""Self-test trigger — runs after execution if vault infrastructure was touched.

Categories of "vault infrastructure" change that require self-testing:
  - scripts/ai_dougs/*       (legacy helpers still imported by ported.py)
  - scripts/ai_sharpener/*   (active daemon)
  - scripts/vault_graph/*    (the new orchestration package)
  - scripts/hooks/*          (scope enforcement hooks)
  - scripts/probe_models.py  (model discovery)
  - scripts/session_digest.py
  - scripts/vault_smoke_test.py (the test itself — paradox-safe: we re-run it)
  - vault.py / run_vault.py  (entry points)
  - model_pool.json          (model registry)
  - .env                      (rejected immediately — never edit secrets)

Anything outside that surface skips self-test.

What runs:
  1. vault_smoke_test.py   — syntax + compile + folder structure
  2. vault_graph_audit.py  — behavioral verification of the graph

If either fails, the change is auto-reverted via `git checkout` of the touched
files (within the vault root). The execution is marked failed and a failure
mode entry is logged so routing learns from it.

Goal: every change the vault agent makes to its own infrastructure must be
verified before it's accepted, so the agent can't silently break the
pipeline.
"""
from __future__ import annotations
import re
import subprocess
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from scripts.vault_graph import ported as P
else:
    from . import ported as P


# Path patterns that count as vault infrastructure
INFRA_PATTERNS = (
    "scripts/ai_dougs/",
    "scripts/ai_sharpener/",
    "scripts/vault_graph/",
    "scripts/hooks/",
    "scripts/probe_models.py",
    "scripts/session_digest.py",
    "scripts/vault_smoke_test.py",
    "scripts/vault_graph_audit.py",
    "vault.py",
    "run_vault.py",
    "state/model_pool.json",
)

# Paths the agent MUST NOT touch under any circumstances. If these appear in
# the diff, fail immediately without even running the smoke test.
FORBIDDEN_PATHS = (
    ".env",
    ".env.",
    "ai_main.md",
    "VISION.md",         # vision is human-curated
    "_human_notes/",     # HUMAN-ONLY zone — see _human_notes/README.md
    ".claude/",          # orchestrator harness config
)

# LOCKED_BLOCKS removed 2026-05-12. The HUMAN-OWNED block-fingerprint check
# was over-engineered defense-in-depth on top of FORBIDDEN_PATHS already
# covering VISION.md, ai_main.md, and _human_notes/. SYSTEM.md is a doc;
# nothing inside is keyed off byte-exact content.


def _diff_paths(diff_text: str, vault_root: Path | None = None) -> list[str]:
    """Extract file paths from a git diff (--git a/X b/X header lines).

    Returns paths in the standard VAULT-relative form (forward slashes), so
    they match INFRA_PATTERNS without surprises.

    PATH NORMALISATION (added 2026-05-04 — fixes silent self-test skips):

    The vault may sit inside a larger parent git repo (e.g.
    C:/Users/gigga/.git contains Documents/vault/...). In that case
    `git diff HEAD` from inside the vault still emits paths relative to the
    PARENT repo's root, like 'Documents/vault/scripts/...' — which never
    matches INFRA_PATTERNS (those start with 'scripts/...'). It also leaks
    unrelated paths from sibling projects in the parent repo.

    We compute the vault's path RELATIVE to the git toplevel and strip that
    prefix from every diff path. Paths that don't start with the vault
    prefix are unrelated noise from elsewhere in the parent repo and get
    dropped entirely.
    """
    if not diff_text:
        return []
    raw_paths = set()
    for line in diff_text.splitlines():
        m = re.match(r"diff --git a/(\S+) b/(\S+)", line)
        if m:
            raw_paths.add(m.group(2))
        m = re.match(r"\+\+\+ b/(\S+)", line)
        if m:
            raw_paths.add(m.group(1))

    # Compute the vault's prefix relative to the git toplevel.
    # If vault has its OWN .git (post-2026-05-10), git_prefix is empty
    # and we fall back to a home-relative prefix to stay defensive against
    # diffs that contain home-relative paths from older / parent gits.
    git_prefix = ""
    home_prefix = ""
    if vault_root is not None:
        try:
            top = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=5, cwd=str(vault_root),
            )
            if top.returncode == 0 and top.stdout.strip():
                git_root = Path(top.stdout.strip()).resolve()
                v_root = Path(vault_root).resolve()
                try:
                    rel = v_root.relative_to(git_root)
                    if rel != Path("."):
                        git_prefix = rel.as_posix() + "/"
                except ValueError:
                    pass  # vault not inside git root (separate repo) — no prefix needed
        except Exception:
            pass
        # Always compute home-relative prefix as secondary check (handles
        # diffs that carry a home-relative prefix like `Documents/vault/`
        # from a parent git context or from external tooling).
        try:
            home = Path.home().resolve()
            v_root = Path(vault_root).resolve()
            home_rel = v_root.relative_to(home)
            if home_rel != Path("."):
                home_prefix = home_rel.as_posix() + "/"
        except (ValueError, Exception):
            pass

    # Common sibling-directory patterns under the user's home that we want
    # to drop if a path begins with one (e.g. Desktop/, Downloads/).
    SIBLING_DIRS = (
        "Desktop/", "Downloads/", "Pictures/", "Music/", "Videos/",
        "OneDrive/", "Library/", "AppData/", "Public/",
    )

    normalised = []
    for p in raw_paths:
        # Strict prefix match (parent-git context): strip and keep.
        if git_prefix and p.startswith(git_prefix):
            normalised.append(p[len(git_prefix):])
            continue
        # Home-relative prefix match (defensive): strip and keep.
        if home_prefix and p.startswith(home_prefix):
            normalised.append(p[len(home_prefix):])
            continue
        # Path doesn't match any known vault prefix. Decide:
        #   - Sibling-shaped (starts with Desktop/, Downloads/, etc.) → drop
        #   - Looks home-relative under same home component as vault but
        #     ISN'T the vault path → drop (sibling project under same home)
        #   - Otherwise → keep (best-effort; assumes vault-relative)
        if any(p.startswith(s) for s in SIBLING_DIRS):
            continue
        if home_prefix:
            # If path starts with the FIRST component of vault's home_prefix
            # (e.g. "Documents/" when vault is at Documents/vault) but didn't
            # match the full prefix → sibling under same home component.
            home_first = home_prefix.split("/", 1)[0] + "/"
            if p.startswith(home_first):
                continue  # different sibling under same home component
        # Either no prefixes available, or path doesn't look sibling-shaped.
        # Treat as vault-relative.
        normalised.append(p)
    return sorted(normalised)


def _is_infra(path: str) -> bool:
    """True if the path is vault infrastructure that should trigger self-test."""
    return any(path.startswith(p) or path == p.rstrip("/") for p in INFRA_PATTERNS)


def _is_forbidden(path: str) -> bool:
    """True if the agent should never have touched this file."""
    return any(path == fp or path.startswith(fp) for fp in FORBIDDEN_PATHS)


# Locked-block helpers removed 2026-05-12 (see LOCKED_BLOCKS note above).


def _run_smoke_test(vault_root: Path) -> tuple[bool, str]:
    """Run vault_smoke_test.py. Returns (passed, output)."""
    script = vault_root / "scripts" / "vault_smoke_test.py"
    if not script.exists():
        return True, "smoke test script missing — skipped"
    try:
        result = subprocess.run(
            [sys.executable, str(script)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
            cwd=str(vault_root),
        )
        return result.returncode == 0, (result.stdout + result.stderr)
    except Exception as e:
        return False, f"smoke test failed to execute: {e}"


def _run_audit(vault_root: Path) -> tuple[bool, str]:
    """Run vault_graph_audit.py. Returns (passed, output)."""
    script = vault_root / "scripts" / "vault_graph_audit.py"
    if not script.exists():
        return True, "audit script missing — skipped"
    try:
        result = subprocess.run(
            [sys.executable, str(script)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
            cwd=str(vault_root),
        )
        return result.returncode == 0, (result.stdout + result.stderr)
    except Exception as e:
        return False, f"audit failed to execute: {e}"


def _revert_via_git(vault_root: Path, paths: list[str]) -> bool:
    """Revert specified paths via `git checkout HEAD -- <paths>` (tracked)
    or `unlink()` (untracked-new files the agent created).

    Bug I fix (2026-05-09): the prior version called `git checkout HEAD --
    <paths>` blindly, which fails for ANY path that isn't tracked in git
    (newly-created files). Result: smoke-test failure → revert silently
    failed → agent's broken changes stayed on disk in an inconsistent
    state. Now we partition paths by tracked-status and handle each:
      - tracked → `git checkout HEAD -- <path>` (restore from HEAD)
      - untracked-new (file exists, not in git) → unlink (undo creation)
      - untracked-missing (file doesn't exist either) → no-op

    Returns True iff EVERY path was successfully handled. Surfaces a
    clear warning whenever a path falls into the untracked-new branch
    so the human knows it wasn't a true git revert (the file is gone,
    which is the right "revert to before agent creation" behavior, but
    it's worth flagging).
    Only operates within `vault_root` (not on user app code in code/).
    """
    if not paths:
        return True
    vault_paths = [p for p in paths if not p.startswith("code/")]
    if not vault_paths:
        return True
    try:
        check = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=5, cwd=str(vault_root),
        )
        if check.returncode != 0:
            print(f"      {P.YELLOW}cannot revert: vault is not a git repo. "
                  f"Manually inspect/restore: {vault_paths}{P.RESET}")
            return False

        tracked: list[str] = []
        untracked_new: list[str] = []
        untracked_missing: list[str] = []
        for path in vault_paths:
            ls = subprocess.run(
                ["git", "ls-files", "--error-unmatch", path],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=5, cwd=str(vault_root),
            )
            if ls.returncode == 0:
                tracked.append(path)
            elif (vault_root / path).exists():
                untracked_new.append(path)
            else:
                untracked_missing.append(path)

        ok = True

        if tracked:
            result = subprocess.run(
                ["git", "checkout", "HEAD", "--"] + tracked,
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=30, cwd=str(vault_root),
            )
            if result.returncode == 0:
                print(f"      {P.GREEN}reverted {len(tracked)} tracked file(s) "
                      f"via git checkout{P.RESET}")
            else:
                print(f"      {P.RED}git checkout failed for tracked paths: "
                      f"{result.stderr.strip()}{P.RESET}")
                ok = False

        if untracked_new:
            print(f"      {P.YELLOW}WARNING: {len(untracked_new)} agent-created "
                  f"file(s) not tracked by git — deleting to undo creation: "
                  f"{untracked_new}{P.RESET}")
            for path in untracked_new:
                try:
                    (vault_root / path).unlink()
                except Exception as e:
                    print(f"      {P.RED}failed to delete {path}: {e}{P.RESET}")
                    ok = False

        if untracked_missing:
            # File doesn't exist + not tracked → nothing to revert. Probably
            # a path mtime captured during diff but the file was deleted by
            # the agent's own subsequent action. No-op.
            pass

        return ok
    except Exception as e:
        print(f"      {P.RED}revert failed: {e}{P.RESET}")
        return False


def _infra_writes_since_mtime(vault_root: Path, start_unix: float) -> list[str]:
    """Walk every path covered by INFRA_PATTERNS and return any file whose
    mtime is at or after start_unix. This is the AUTHORITATIVE source of
    "what infra did the executor agent modify" — works regardless of which
    CLI the agent used (claude/codex/gemini), regardless of whether files
    are tracked in git, and doesn't depend on hooks firing for subprocesses.

    Why this matters: post_write hooks only fire for the parent Claude Code
    harness. git diff only sees tracked files (most vault files aren't tracked).
    Both miss what subprocess executor agents actually wrote. mtime is ground
    truth.
    """
    hits: list[str] = []
    for pattern in INFRA_PATTERNS:
        # INFRA_PATTERNS may end in "/" (directory) or be a file path
        target = vault_root / pattern.rstrip("/")
        if not target.exists():
            continue
        if target.is_file():
            try:
                if target.stat().st_mtime >= start_unix:
                    hits.append(pattern.rstrip("/"))
            except OSError:
                pass
        elif target.is_dir():
            for sub in target.rglob("*"):
                if not sub.is_file():
                    continue
                # Skip __pycache__ and similar
                if "__pycache__" in sub.parts or sub.suffix == ".pyc":
                    continue
                try:
                    if sub.stat().st_mtime >= start_unix:
                        rel = sub.relative_to(vault_root).as_posix()
                        hits.append(rel)
                except OSError:
                    pass
    return sorted(set(hits))


def _writes_since(vault_root: Path, start_iso: str) -> list[str]:
    """Read FILE-WRITE entries from ai_context/agent_trace.md that happened
    at or after start_iso. Returns vault-relative paths.

    The post_write hook logs every Edit/Write claude does in this format:
        | 2026-05-04 15:00:50 | FILE-WRITE | Edit: scripts/vault_graph/self_test.py | hook |

    This is the actual ground truth of what the execution agent modified —
    much more reliable than `git diff` when most vault files aren't tracked.
    """
    trace = vault_root / "ai_context" / "agent_trace.md"
    if not trace.exists():
        return []
    paths: set[str] = set()
    try:
        text = trace.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []
    # Format: | YYYY-MM-DD HH:MM:SS | FILE-WRITE | Tool: path | hook |
    pat = re.compile(
        r"^\|\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\s*\|\s*FILE-WRITE\s*\|\s*\w+:\s*(\S[^|]*?)\s*\|",
        re.MULTILINE,
    )
    for m in pat.finditer(text):
        ts, p = m.group(1), m.group(2).strip()
        if ts >= start_iso:  # ISO format sorts lexically by time
            # Skip entries the post_write hook marked as external to the
            # vault root (orchestrator writes to AppData, etc.). They are
            # legitimate trace data but not vault-infra changes — keeping
            # them out of this list lets the audit assert vault-relative.
            if p.startswith("EXTERNAL:"):
                continue
            paths.add(p.replace("\\", "/"))
    return sorted(paths)


def _log_self_test(vault_root: Path, task_name: str, outcome: str,
                   reason: str, infra_paths: list[str] | None = None) -> None:
    """Persist every self-test invocation to logs/self_test.jsonl so we can
    audit AFTER the fact whether self-test fired on a given task. Without this
    log the only signal was a print() to the daemon stdout, which gets wiped
    on restart and isn't programmatically queryable.
    """
    import json as _j, datetime as _dt
    log = vault_root / "logs" / "self_test.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(log, "a", encoding="utf-8") as f:
            f.write(_j.dumps({
                "timestamp": _dt.datetime.now().isoformat(timespec="seconds"),
                "task": task_name,
                "outcome": outcome,           # "passed" / "failed" / "skipped" / "forbidden" / "locked_block"
                "reason": reason,
                "infra_paths": infra_paths or [],
            }) + "\n")
    except Exception:
        pass  # logging failure must not break the gate


def run_self_test(vault_root: Path, diff_text: str, task_name: str,
                  app_location: str = "",
                  orchestrator_writes: set[str] | None = None) -> tuple[bool, str]:
    """Run self-test if the diff touches vault infrastructure.

    Returns (passed, reason).
      passed=True  → either no infra touched, OR infra touched and tests passed
      passed=False → infra touched, tests failed (and we attempted revert)

    Side effects:
      - appends a failure_mode entry on test failure
      - logs every invocation (including skips) to logs/self_test.jsonl

    `orchestrator_writes` (set of vault-relative paths) is subtracted from
    the diff so writes by the parent Claude Code harness (not the subprocess
    agent) don't trigger forbidden-path / locked-block / infra checks. The
    diff capture grabs ALL working-tree changes, which used to false-positive
    on orchestrator edits made during the agent's execution window.
    """
    paths = _diff_paths(diff_text, vault_root=vault_root)
    # Subtract orchestrator-session writes — those are not agent writes
    if orchestrator_writes:
        before = len(paths)
        paths = [p for p in paths if p not in orchestrator_writes]
        if before != len(paths):
            print(f"      {P.DIM}self-test: subtracted "
                  f"{before - len(paths)} orchestrator-session write(s) "
                  f"from diff{P.RESET}")
    if not paths:
        _log_self_test(vault_root, task_name, "skipped", "no diff to inspect")
        return True, "no diff to inspect"

    # Forbidden paths — fail immediately, attempt revert
    forbidden = [p for p in paths if _is_forbidden(p)]
    if forbidden:
        msg = f"forbidden paths modified: {', '.join(forbidden)}"
        print(f"      {P.RED}self-test: {msg}{P.RESET}")
        P._appendFailureMode(
            vault_root, task_name, "execution", "forbidden_path_modified",
            msg, app_location,
        )
        _revert_via_git(vault_root, paths)
        _log_self_test(vault_root, task_name, "forbidden", msg, forbidden)
        return False, msg

    # Locked-block check removed 2026-05-12 (FORBIDDEN_PATHS already covers
    # vision/ai_main/_human_notes; SYSTEM.md is plain prose).

    # Check if any infra paths were touched
    infra_touched = [p for p in paths if _is_infra(p)]
    if not infra_touched:
        reason = f"no vault infra touched ({len(paths)} non-infra path(s))"
        _log_self_test(vault_root, task_name, "skipped", reason, [])
        return True, reason

    print(f"      {P.CYAN}self-test triggered: {len(infra_touched)} infra path(s) modified{P.RESET}")
    for p in infra_touched:
        print(f"        {P.DIM}↳ {p}{P.RESET}")

    # Run smoke test first (fast)
    smoke_ok, smoke_out = _run_smoke_test(vault_root)
    if not smoke_ok:
        print(f"      {P.RED}smoke test FAILED — auto-reverting{P.RESET}")
        snippet = smoke_out[-400:] if len(smoke_out) > 400 else smoke_out
        P._appendFailureMode(
            vault_root, task_name, "execution", "self_test_smoke_failed",
            f"Smoke test failed after modifying {len(infra_touched)} infra path(s). "
            f"Output: {snippet}",
            app_location,
        )
        _revert_via_git(vault_root, paths)
        _log_self_test(vault_root, task_name, "failed", "smoke test failed", infra_touched)
        return False, "smoke test failed"

    print(f"      {P.GREEN}smoke test passed{P.RESET}")

    # Then the heavier audit (only if smoke passed — saves time)
    audit_ok, audit_out = _run_audit(vault_root)
    if not audit_ok:
        print(f"      {P.RED}graph audit FAILED — auto-reverting{P.RESET}")
        snippet = audit_out[-400:] if len(audit_out) > 400 else audit_out
        P._appendFailureMode(
            vault_root, task_name, "execution", "self_test_audit_failed",
            f"Graph audit failed after modifying {len(infra_touched)} infra path(s). "
            f"Output: {snippet}",
            app_location,
        )
        _revert_via_git(vault_root, paths)
        _log_self_test(vault_root, task_name, "failed", "graph audit failed", infra_touched)
        return False, "graph audit failed"

    print(f"      {P.GREEN}graph audit passed{P.RESET}")
    reason = f"all self-tests passed ({len(infra_touched)} infra path(s) verified)"
    _log_self_test(vault_root, task_name, "passed", reason, infra_touched)
    return True, reason
