"""Vault-side project memory for work outside the vault."""
from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


MEMORY_DIR_NAME = ".agent_memory"
CHARTER_FILE = "charter.md"
FAILURES_FILE = "failures.md"
TASK_LOG_FILE = "task_log.jsonl"
PROJECT_MARKERS = (
    ".git",
    "package.json",
    "pyproject.toml",
    "Cargo.toml",
    "go.mod",
    "composer.json",
    "Gemfile",
    "pom.xml",
    "build.gradle",
)

DEFAULT_CHARTER = "# Project Charter\n\nHuman-edited project description goes here.\n"
DEFAULT_FAILURES = "# Project Failure Modes\n\nProject-scoped failure modes go here.\n"

JUDGE_AXES = (
    "plan_adherence",
    "scope_discipline",
    "code_quality",
    "completeness",
    "risk",
)


def get_vault_root(vault_root: str | Path | None = None) -> Path:
    """Return the vault root, defaulting to this module's location."""
    if vault_root:
        return Path(vault_root).expanduser().resolve()
    return Path(__file__).resolve().parents[2]


def resolve_project_root(cwd_or_app_location: str | Path | None,
                         vault_root: str | Path | None = None) -> Path | None:
    """Return the vault-side memory bucket for non-vault project work."""
    if not cwd_or_app_location:
        return None

    root = get_vault_root(vault_root)
    candidate = _resolve_location(cwd_or_app_location, root)
    if candidate is None or _is_relative_to(candidate, root):
        return None

    stable_identifier = get_stable_project_identifier(candidate)
    if not stable_identifier:
        return None
    return root / MEMORY_DIR_NAME / stable_identifier


def get_stable_project_identifier(project_path: str | Path) -> str:
    """Build a cross-machine identifier from git remote origin or basename."""
    candidate = Path(project_path).expanduser()
    try:
        candidate = candidate.resolve()
    except OSError:
        candidate = candidate.absolute()

    project_root = infer_project_root(candidate)
    remote_url = _git_remote_origin(project_root)
    if remote_url:
        return _slug_from_remote_url(remote_url)
    return _slug_from_name(project_root.name)


def infer_project_root(path: str | Path) -> Path:
    """Infer the project root without climbing past the first project marker."""
    candidate = Path(path).expanduser()
    try:
        candidate = candidate.resolve()
    except OSError:
        candidate = candidate.absolute()

    if candidate.exists() and candidate.is_file():
        candidate = candidate.parent

    current = candidate
    home_root = _home_root()
    while True:
        if current == home_root:
            return candidate
        if any((current / marker).exists() for marker in PROJECT_MARKERS):
            return current
        if current.parent == current:
            return candidate
        current = current.parent


def project_memory_dir(cwd_or_app_location: str | Path | None,
                       vault_root: str | Path | None = None) -> Path | None:
    memory_bucket = resolve_project_root(cwd_or_app_location, vault_root)
    if memory_bucket is None:
        return None
    return memory_bucket


def bootstrap_project_memory(cwd_or_app_location: str | Path | None,
                             vault_root: str | Path | None = None) -> Path | None:
    """Create the vault-side project memory bucket and files if applicable.

    2026-05-12: failures.md retired from agent-readable surfaces. Project
    memory now consists of `charter.md` (hand-authored, read by agents) and
    `task_log.jsonl` (auto-appended, diagnostic only — never read back into
    agent context). Failures that matter are co-authored into the charter
    or distilled into a skill by user + orchestrator.
    """
    memory_dir = project_memory_dir(cwd_or_app_location, vault_root)
    if memory_dir is None:
        return None

    memory_dir.mkdir(parents=True, exist_ok=True)
    defaults = {
        CHARTER_FILE: DEFAULT_CHARTER,
        TASK_LOG_FILE: "",
    }
    for file_name, default_text in defaults.items():
        path = memory_dir / file_name
        if not path.exists():
            path.write_text(default_text, encoding="utf-8")
    return memory_dir


def load_project_memory_context(cwd_or_app_location: str | Path | None,
                                vault_root: str | Path | None = None,
                                max_log_entries: int = 8,  # kept for back-compat; unused
                                max_chars: int = 8000) -> str:
    """Read the project charter into a compact planner context block.

    2026-05-12: only the hand-authored charter is injected. failures.md is
    retired; task_log.jsonl is auto-data and not read back into agent
    context. If you want failure context to influence agents, co-author it
    into the charter or into a skill.
    """
    memory_dir = bootstrap_project_memory(cwd_or_app_location, vault_root)
    if memory_dir is None:
        return ""

    charter = _read_text(memory_dir / CHARTER_FILE).strip()
    if not charter or charter == DEFAULT_CHARTER.strip():
        return ""

    block = (
        "## Project Charter\n\n"
        f"{charter}"
    )
    if len(block) > max_chars:
        return block[:max_chars].rstrip() + "\n\n...(charter truncated)"
    return block


def load_judge_scope_block(cwd_or_app_location: str | Path | None,
                           vault_root: str | Path | None = None,
                           max_chars: int = 2000) -> str:
    """Return a compact project-scope block for the cross-model judge prompt.

    Closes GAP #1 (2026-05-11): the execution judge previously had no project-
    type context, so the 5 generic axes (plan_adherence, scope_discipline,
    code_quality, completeness, risk) were applied equally to a UI redesign
    and a CLI tool. With scope, the judge can weight axes appropriately and
    flag deviations from the project's actual goals.

    Scope sources, in priority order:
      1. `vault/.agent_memory/<slug>/judge_scope.md` (optional, project-specific
         axis weights / domain hints written by the project owner)
      2. `vault/.agent_memory/<slug>/charter.md` (the project's goal statement)
      3. Vault-internal default (when no app_location or path is inside the vault)

    Returned block is plain text intended for direct interpolation into the
    judge prompt — empty string means "no scope information available; use
    your defaults", which is also a valid signal for the judge to recognize.
    """
    # Vault-internal work has no per-project memory bucket; surface the
    # vault's own quality directives so the judge knows the project type.
    memory_dir = project_memory_dir(cwd_or_app_location, vault_root)
    if memory_dir is None:
        return (
            "PROJECT SCOPE: vault internals (the self-improving AI coding workflow itself).\n"
            "- Quality hierarchy: quality of product > efficiency of vault > token efficiency.\n"
            "- Safety through observation, not arbitrary cutoffs.\n"
            "- Everything is retroactive: classifications, thresholds, model choices, prompt\n"
            "  templates can change as data accumulates.\n"
            "- Vault never auto-modifies itself — vault infrastructure changes go through\n"
            "  the user + orchestrator session; subprocess agents never edit vault scripts.\n"
            "- Weight axes equally; do NOT penalize a change for adding behavioral verification\n"
            "  or supervisor symptom rules — those are intentional vault improvements.\n"
        )
    # Project-specific scope file takes precedence over charter
    scope_file = memory_dir / "judge_scope.md"
    if scope_file.exists():
        text = _read_text(scope_file).strip()
        if text:
            block = f"PROJECT SCOPE: {memory_dir.name}\n{text}\n"
            return block[:max_chars]
    # Fall back to charter excerpt
    charter = _read_text(memory_dir / CHARTER_FILE).strip()
    if charter and charter != DEFAULT_CHARTER.strip():
        # Trim charter to a sensible excerpt for the judge prompt
        excerpt = charter[:max_chars - 200]
        return (
            f"PROJECT SCOPE: {memory_dir.name}\n"
            f"Charter (excerpt):\n{excerpt}\n\n"
            "Weight axes according to the project's stated goals. A change that\n"
            "advances the charter should not be penalized for being unconventional.\n"
        )
    # Project bucket exists but no charter content — flag this to the judge
    return (
        f"PROJECT SCOPE: {memory_dir.name}\n"
        "No charter or judge_scope file found in the project memory bucket.\n"
        "Apply default axis weights; surface any deviation from the agreed plan\n"
        "as a concern so the human can decide whether it was intentional.\n"
    )


def build_project_task_log_entry(state: dict[str, Any], outcome: str | None = None) -> dict[str, Any]:
    """Build the durable per-project task_log.jsonl record."""
    judge_data = state.get("judge_verdict") or {}
    judge_scores = judge_data.get("scores") if isinstance(judge_data, dict) else {}
    if not isinstance(judge_scores, dict):
        judge_scores = {}

    judge_verdict = judge_data.get("verdict", "") if isinstance(judge_data, dict) else ""
    judge_score_avg = judge_data.get("score_avg", 0.0) if isinstance(judge_data, dict) else 0.0
    try:
        judge_score_avg = round(float(judge_score_avg), 2)
    except (TypeError, ValueError):
        judge_score_avg = 0.0

    entry = {
        "task_id": state.get("task_name", ""),
        "completed_at": datetime.now().isoformat(timespec="seconds"),
        "outcome": outcome if outcome is not None else state.get("status", ""),
        "app_location": state.get("app_location", ""),
        "task_tags": _task_tags_from_state(state),
        "verification_signal_tags": _verification_tags_from_state(state),
        "judge_verdict": judge_verdict or "",
        "judge_scores": {axis: int(judge_scores[axis]) for axis in JUDGE_AXES if axis in judge_scores},
        "judge_score_avg": judge_score_avg if judge_scores else 0.0,
    }
    return entry


def append_project_task_log(cwd_or_app_location: str | Path | None,
                            state_or_entry: dict[str, Any],
                            vault_root: str | Path | None = None,
                            outcome: str | None = None) -> Path | None:
    """Append one JSON object to the vault-side project task log."""
    memory_dir = bootstrap_project_memory(cwd_or_app_location, vault_root)
    if memory_dir is None:
        return None
    entry = (
        state_or_entry
        if _looks_like_task_log_entry(state_or_entry)
        else build_project_task_log_entry(state_or_entry, outcome=outcome)
    )
    path = memory_dir / TASK_LOG_FILE
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, sort_keys=True) + "\n")
    return path


def _looks_like_task_log_entry(value: dict[str, Any]) -> bool:
    required = {"task_id", "outcome", "judge_verdict", "judge_scores", "judge_score_avg"}
    return required.issubset(value.keys())


def _resolve_location(value: str | Path, vault_root: Path) -> Path | None:
    raw = Path(str(value)).expanduser()
    candidate = raw if raw.is_absolute() else vault_root / raw
    try:
        return candidate.resolve()
    except OSError:
        return None


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _git_remote_origin(project_root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(project_root), "remote", "get-url", "origin"],
            check=False,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, ValueError):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _home_root() -> Path | None:
    try:
        return Path.home().resolve()
    except OSError:
        return None


def _slug_from_remote_url(remote_url: str) -> str:
    normalized = remote_url.strip().replace("\\", "/")
    if "://" in normalized:
        parsed = urlparse(normalized)
        normalized = parsed.path.lstrip("/")
    elif "@" in normalized and ":" in normalized.split("@", 1)[1]:
        normalized = normalized.split("@", 1)[1].split(":", 1)[1]
    elif ":" in normalized and "/" in normalized:
        normalized = normalized.split(":", 1)[1]

    if normalized.endswith(".git"):
        normalized = normalized[:-4]
    return _slug_from_name(normalized)


def _slug_from_name(value: str) -> str:
    normalized = value.strip().replace("\\", "/").strip("/")
    normalized = re.sub(r"^[a-zA-Z]:/", "", normalized)
    normalized = re.sub(r"[^A-Za-z0-9/._-]+", "-", normalized)
    normalized = normalized.replace("/", "--").strip("-._").lower()
    return normalized or "unknown-project"


def _task_tags_from_state(state: dict[str, Any]) -> list[str]:
    tags: list[str] = []
    for key in ("tags", "task_tags", "loaded_skills"):
        value = state.get(key)
        if isinstance(value, str):
            tags.extend([part.strip() for part in value.split(",") if part.strip()])
        elif isinstance(value, list):
            tags.extend(str(part).strip() for part in value if str(part).strip())
    return sorted(set(tags))


def _verification_tags_from_state(state: dict[str, Any]) -> list[str]:
    outcome = state.get("verification_outcome") or {}
    results = outcome.get("results", []) if isinstance(outcome, dict) else []
    tags: set[str] = set()
    if isinstance(results, list):
        for result in results:
            if not isinstance(result, dict) or not result.get("passed"):
                continue
            for tag in result.get("tags", []) or []:
                tags.add(str(tag))
    return sorted(tags)


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _read_recent_jsonl(path: Path, max_entries: int) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    entries: list[dict[str, Any]] = []
    for line in lines[-max_entries:]:
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            entries.append(parsed)
    return entries
