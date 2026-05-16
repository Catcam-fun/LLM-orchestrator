#!/usr/bin/env python3
"""
ai_dougs.py - Multi-stage AI task processor.

LEGACY: as of 2026-05-04, this module is no longer launched as a daemon by
run_vault.py. The new entry point is `python vault.py daemon` (LangGraph
backend). This file remains as the import source for vault_graph/ported.py
which re-exports its helpers (cost log, file locks, routing, etc.) into the
new system. Will be extracted + deleted after the new pipeline proves itself.
"""

import contextlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

try:
    from .cli_builders import (
        build_cli_command_for_prompt,
        build_cli_command,
        resolveCmd,
    )
    from . import routing as _routing
    from .cost_tracking import (
        PHASE_COST_CEILING_FIELDS,
        PHASE_COST_GROUPS,
        PHASE_COST_OBSERVED_FIELDS,
        _MODEL_COSTS,
        _callCostUsd,
        _configureCostTracking,
        _estimateTokens,
        _loadPerformanceHistory,
        _phaseDurationSeconds,
        _phaseCostCeilingField,
        _phaseCostGroup,
        _phaseCostObservedField,
        _recordCallCost,
        _resetCostAccumulator as _resetCostAccumulatorState,
        _startPhaseTimer,
        _taskCostCeiling,
        _task_cost_accumulator,
        _writeCostLog,
    )
    from .routing import (
        _PROVIDER_EXPLORATION_FLOOR,
        _ROUTING_PARAMS_DEFAULTS,
        _loadRoutingParams,
        _rankProviders,
        _recently_used_providers,
        _routeModel,
        _should_explore_provider,
        _wilson_lower_bound,
    )
    from .subprocess_infra import (
        _active_subproc_file,
        _deregisterActiveSubproc,
        _is_ai_cli_command,
        _kill_process_tree,
        _registerActiveSubproc,
        _run_with_tree_kill_timeout,
        run_with_animation,
    )
    from .prompts import (
        EXECUTION_PROMPT,
        LEARNING_PROMPT,
        PLANNING_PROMPT,
        PLAN_REVIEW_PROMPT,
        REFINEMENT_PROMPT,
        VISUAL_COMPARISON_PROMPT,
    )
except ImportError:
    from cli_builders import (
        build_cli_command_for_prompt,
        build_cli_command,
        resolveCmd,
    )
    import routing as _routing
    from cost_tracking import (
        PHASE_COST_CEILING_FIELDS,
        PHASE_COST_GROUPS,
        PHASE_COST_OBSERVED_FIELDS,
        _MODEL_COSTS,
        _callCostUsd,
        _configureCostTracking,
        _estimateTokens,
        _loadPerformanceHistory,
        _phaseDurationSeconds,
        _phaseCostCeilingField,
        _phaseCostGroup,
        _phaseCostObservedField,
        _recordCallCost,
        _resetCostAccumulator as _resetCostAccumulatorState,
        _startPhaseTimer,
        _taskCostCeiling,
        _task_cost_accumulator,
        _writeCostLog,
    )
    from routing import (
        _PROVIDER_EXPLORATION_FLOOR,
        _ROUTING_PARAMS_DEFAULTS,
        _loadRoutingParams,
        _rankProviders,
        _recently_used_providers,
        _routeModel,
        _should_explore_provider,
        _wilson_lower_bound,
    )
    from subprocess_infra import (
        _active_subproc_file,
        _deregisterActiveSubproc,
        _is_ai_cli_command,
        _kill_process_tree,
        _registerActiveSubproc,
        _run_with_tree_kill_timeout,
        run_with_animation,
    )
    from prompts import (
        EXECUTION_PROMPT,
        LEARNING_PROMPT,
        PLANNING_PROMPT,
        PLAN_REVIEW_PROMPT,
        REFINEMENT_PROMPT,
        VISUAL_COMPARISON_PROMPT,
    )

# Force UTF-8 on Windows so box-drawing chars and arrows print correctly.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


SCRIPT_VERSION = "1.34"  # 2026-05-10 - extract subprocess helpers to scripts/ai_dougs/subprocess_infra.py

# ANSI styles
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
CYAN = "\033[95m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[31m"
WHITE = "\033[97m"
ORANGE = "\033[38;5;208m"
MAGENTA = "\033[35m"

W = 50

# Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬ Context injection Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬
# ai_main.md + vault root are loaded once at startup.
# Before every AI call Python keyword-scans the task text and injects only the
# relevant ai_context/ files.  This avoids asking the model to discover context
# at runtime, which wastes tokens and doesn't work equally across all providers.

_AI_MAIN_CONTENT = ""
_VAULT_ROOT = None           # set by loadAiMain(); used by buildTaskPreamble()
_CURRENT_APP_LOCATION = None  # set per-task in processTask(); used by context selector

# Standards keyword-injection system removed 2026-05-12.
# CONTEXT_ALWAYS_INCLUDE + CONTEXT_KEYWORD_MAP were the pre-Skills-SDK
# context-injection mechanism (referenced standards_*.md files keyword-matched
# to task text). Replaced by `ai_skills/<name>/SKILL.md` with
# description-as-trigger (Anthropic Skills SDK schema). One skill system, not two.
CONTEXT_ALWAYS_INCLUDE = []

# keyword Ã¢â€ â€™ filename in ai_context/ that gets injected when the keyword appears
# in the task text (case-insensitive).  A filename can appear under multiple keys.
CONTEXT_KEYWORD_MAP = {}


# Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬ Token estimation & cost tracking Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬


_context_injection_log = {}  # {filename: token_count} for the current task; reset per task
_context_stats = {}          # {filename: {"injected": N, "redundant": N}} Ã¢â‚¬â€ ratio-based pruning, loaded at startup
_last_routing_source = "default"  # "history", "resolver", or "default" Ã¢â‚¬â€ set by _routeModel, read by _writeCostLog

# Provider quota-ban TTL Ã¢â‚¬â€ once a provider returns a rate-limit signal, skip it
# for this many seconds before retrying. Survives across phases within one
# Python process (so refinement -> execution -> learning don't each waste 5-10s
# re-trying gemini after it already quota'd in refinement). Resets on next
# process invocation. Reset early via _clear_quota_bans() if needed.
_QUOTA_BAN_TTL_SEC = 3600  # 1 hour Ã¢â‚¬â€ typical free-tier daily/hourly reset window
_quota_banned_until: dict[str, float] = {}  # provider -> unix ts when ban lifts


def _resetCostAccumulator():
    """Zero out the cost accumulator at the start of each task."""
    global _context_injection_log, _last_routing_source
    _resetCostAccumulatorState()
    _context_injection_log = {}
    _last_routing_source = "default"


def _loadLatestSessionDigest(nasRoot, max_chars=2500):
    """Load the most recent session digest as planning context.

    Returns a markdown block (truncated to max_chars) summarising recent activity,
    so the planning agent starts a task knowing what happened in prior sessions.
    Empty string if no digest exists.
    """
    digest_dir = Path(nasRoot) / "logs" / "session_logs"
    if not digest_dir.exists():
        return ""
    digests = sorted(digest_dir.glob("*.md"), reverse=True)
    if not digests:
        return ""
    try:
        latest = digests[0]
        content = latest.read_text(encoding="utf-8")
        if len(content) > max_chars:
            content = content[:max_chars] + "\n\n... (truncated Ã¢â‚¬â€ see full digest at session_logs/)"
        return f"## Recent Session Digest ({latest.stem})\n\n{content}"
    except Exception:
        return ""


def _appendFailureMode(nasRoot, task_name, phase, failure_type, details,
                      app_location=""):
    """Append a structured failure entry to vault/ai_context/failure_modes.md.

    Failure modes accumulate over time. The planning phase loads relevant entries
    so future tasks can avoid repeating known failures. Cheap institutional memory.

    Append-only audit log; data, not prescriptive guidance. Nothing injects
    these entries into planning. Lessons that matter get promoted into skills
    via co-authoring (auto-curator removed 2026-05-12).
    """
    try:
        fm_path = Path(nasRoot) / "ai_context" / "failure_modes.md"
        fm_path.parent.mkdir(parents=True, exist_ok=True)

        # Initialize file with header if it doesn't exist
        if not fm_path.exists():
            fm_path.write_text(
                "# Failure Mode Library\n\n"
                "Append-only catalog of task failures. Data layer; not injected into planning.\n"
                "Each entry: when, where, what failed, why, how it was eventually resolved (if known).\n\n"
                "---\n\n",
                encoding="utf-8",
            )

        entry = (
            f"## {datetime.now().strftime('%Y-%m-%d %H:%M')} Ã¢â‚¬â€ {task_name} / {phase}\n\n"
            f"- **Type**: {failure_type}\n"
            f"- **App**: {app_location or 'n/a'}\n"
            f"- **Details**: {details[:600]}\n\n"
            f"---\n\n"
        )
        with _fileLock(fm_path, timeout=5.0):
            with open(fm_path, "a", encoding="utf-8") as f:
                f.write(entry)
    except Exception:
        pass  # never break a task because failure logging itself failed


def _loadRelevantFailures(nasRoot, task_text, max_entries=5, app_location=""):
    """Load recent failures relevant to the current task for injection into planning context.

    Relevance is scored by:
    1. Same app_location Ã¢â€ â€™ highest priority
    2. Keyword overlap with task_text Ã¢â€ â€™ secondary
    3. Recency Ã¢â€ â€™ tiebreaker

    Returns markdown-formatted block (or empty string if no relevant failures).
    """
    fm_path = Path(nasRoot) / "ai_context" / "failure_modes.md"
    if not fm_path.exists():
        return ""

    try:
        content = fm_path.read_text(encoding="utf-8")
    except Exception:
        return ""

    # Split entries by `---` separator (skip header)
    entries = [e.strip() for e in content.split("\n---\n") if e.strip().startswith("##")]
    if not entries:
        return ""

    task_lower = task_text.lower()
    task_keywords = set(re.findall(r"[a-z]{4,}", task_lower))

    def _score(entry):
        score = 0
        entry_lower = entry.lower()
        if app_location and app_location.lower() in entry_lower:
            score += 10
        # Keyword overlap
        entry_keywords = set(re.findall(r"[a-z]{4,}", entry_lower))
        overlap = len(task_keywords & entry_keywords)
        score += overlap
        return score

    scored = [(e, _score(e)) for e in entries]
    scored = [se for se in scored if se[1] > 0]
    if not scored:
        return ""
    scored.sort(key=lambda se: se[1], reverse=True)

    selected = [se[0] for se in scored[:max_entries]]
    return (
        "## Relevant Past Failures (auto-injected)\n\n"
        "Read these Ã¢â‚¬â€ past tasks failed in these specific ways. Plan to avoid them.\n\n"
        + "\n\n---\n\n".join(selected)
    )


def _shouldRunPlanReview():
    """Quality gate Ã¢â‚¬â€ runs on every plan.

    PRIOR DESIGN (removed 2026-05-08): adaptive skip when planning success
    rate >0.95 over the last 5+ runs. The skip violated the vault's quality
    hierarchy:

        quality of product > efficiency of vault > token efficiency

    Skipping plan review is a quality compromise. The right way to make it
    cheaper is to find a cheaper model that reviews with equivalent
    accuracy (the routing layer's job, evidence-driven), NEVER to skip the
    check. See VISION.md "Safety through observation, not arbitrary
    cutoffs" and the failure_modes.md entry tagged
    `vision/quality_subordinate_to_tokens` for the canonical anti-pattern.

    The `plan_review_enabled` flag remains as an emergency kill-switch.
    Default True. The skip-above-success-rate and min-runs params are now
    unused Ã¢â‚¬â€ kept in config for backward read-compat but ignored.
    """
    params = _loadRoutingParams()
    return bool(params.get("plan_review_enabled", True))


def _runPlanReview(initial_prompt, plan_text, nasRoot, addDirs, providerOrder, sendAiRequests, planning_provider, planning_model, task_name):
    """Run plan review by an independent model. Returns
    (review_text, provider, model) or (None, '', '') on failure.

    The reviewer uses phase='plan_review' so its model selection is independent
    of planning. If the data-driven choice happens to match the planner, that's
    accepted (degenerate case, still adds value as self-review).

    Prompt-variant A/B (GAP #5) removed 2026-05-12 — over-engineered for a
    system with no signal yet; one prompt, evolve manually.
    """
    base_prompt = PLAN_REVIEW_PROMPT

    # Truncation observability (2026-05-09): see truncate_observed docstring.
    review_prompt = base_prompt.format(
        initial_prompt=truncate_observed(initial_prompt, 5000, "plan_review.initial_prompt", task_name),
        plan=truncate_observed(plan_text, 8000, "plan_review.plan", task_name),
    )

    # Build a provider order that prefers a different provider/model than the planner
    # (heuristic; routing data still has the final say)
    diverse_order = []
    for entry in providerOrder:
        new_entry = dict(entry)
        # If the entry would otherwise pick the planner, hint to route differently
        if (entry.get("provider") == planning_provider
                and entry.get("model") == planning_model
                and entry.get("model", "").lower() not in VARIABLE_MODEL_VALUES):
            new_entry["model"] = "variable"  # force re-routing
        diverse_order.append(new_entry)

    print(f"    {DIM}Ã¢â€ â€™ Running plan review (independent model)...{RESET}")
    review_response, rev_provider, rev_model = runAiRequestWithFallback(
        review_prompt, nasRoot, addDirs, diverse_order, sendAiRequests,
        agent_mode=False, phase="plan_review",
    )
    if review_response:
        _archivePromptResponse(nasRoot, task_name, "plan_review", review_prompt, review_response, rev_provider, rev_model)
        # Log cost for plan review independently
        _writeCostLog(task_name, "plan_review", "success", _task_cost_accumulator,
                      extra={"routing_source": _last_routing_source,
                             "reviewer_model": f"{rev_provider}:{rev_model}"})
    return review_response, rev_provider, rev_model


def _lintTaskFile(task, nasRoot):
    """Pre-flight structural validation of a task file. No LLM call.

    Returns: list of error strings (empty if all checks pass).

    Catches malformed tasks before any LLM money is spent. Checks:
    - Required sections present (Initial Prompt non-empty)
    - app_location references a real folder
    - files_allowed/files_blocked paths exist
    - phase-specific cost ceilings and legacy max_cost_usd are valid floats (if set)
    - research_allowed only set with valid values
    - build_command is a string (not empty placeholder)
    """
    errors = []
    fm = task.get("frontmatter", {})

    # Initial Prompt must exist and be non-empty
    initial = extractSection(task, "Initial Prompt") if task.get("sections") else ""
    if not initial or len(initial.strip()) < 10:
        errors.append("Initial Prompt section is missing or too short (<10 chars)")

    # app_location: if set, the folder must exist
    app_loc = fm.get("app_location", "").strip()
    if app_loc:
        app_path = Path(nasRoot) / app_loc
        if not app_path.exists():
            errors.append(f"app_location '{app_loc}' does not exist relative to vault root")

    # files_allowed/files_blocked: if set, must reference paths that exist (warn, not error Ã¢â‚¬â€ paths can be globs)
    for field in ("files_allowed", "files_blocked"):
        val = fm.get(field, "").strip()
        if val and "*" not in val and "," not in val:
            check_path = Path(nasRoot) / val
            if not check_path.exists():
                errors.append(f"{field} '{val}' does not resolve to an existing path")

    # Cost ceilings: phase-specific fields are preferred; max_cost_usd remains
    # a deprecated fallback for existing tasks.
    for field in (*PHASE_COST_CEILING_FIELDS.values(), "max_cost_usd"):
        raw_cost = fm.get(field, "").strip()
        if raw_cost:
            try:
                v = float(raw_cost)
                if v <= 0:
                    errors.append(f"{field} must be > 0 (got {v})")
            except (ValueError, TypeError):
                errors.append(f"{field} '{raw_cost}' is not a valid number")

    # research_allowed: must be a valid bool-like value if set
    research = fm.get("research_allowed", "").strip().lower()
    if research and research not in ("true", "false", "yes", "no", "1", "0", ""):
        errors.append(f"research_allowed must be true/false/yes/no (got '{research}')")

    # build_command: warn if it looks like a placeholder
    bc = fm.get("build_command", "").strip()
    if bc and bc.lower() in ("tbd", "todo", "placeholder", "fix me"):
        errors.append(f"build_command looks like a placeholder: '{bc}'")

    return errors


def _archivePromptResponse(nasRoot, task_name, phase, prompt, response, provider, model):
    """Save the full prompt + response to vault/prompt_archive/<task>/<phase>.md.

    Enables debugging ("why did the agent decide X?"), prompt-pattern refinement
    based on what worked, and learning-agent analysis of prompt quality.
    Cheap (just disk), called after every successful AI call.
    """
    if not task_name or not prompt:
        return
    try:
        archive_dir = Path(nasRoot) / "logs" / "prompt_archive" / task_name
        archive_dir.mkdir(parents=True, exist_ok=True)
        # Use timestamp suffix so retries don't overwrite earlier attempts
        ts = datetime.now().strftime("%H%M%S")
        archive_file = archive_dir / f"{phase}_{ts}.md"
        content = (
            f"---\n"
            f"task: {task_name}\n"
            f"phase: {phase}\n"
            f"provider: {provider or 'unknown'}\n"
            f"model: {model or 'unknown'}\n"
            f"timestamp: {datetime.now().isoformat(timespec='seconds')}\n"
            f"prompt_chars: {len(prompt)}\n"
            f"response_chars: {len(response or '')}\n"
            f"---\n\n"
            f"## Prompt\n\n```\n{prompt}\n```\n\n"
            f"## Response\n\n```\n{response or '(no response)'}\n```\n"
        )
        archive_file.write_text(content, encoding="utf-8")
    except Exception:
        # Never let archiving break a real task
        pass


@contextlib.contextmanager
def _fileLock(target_path, timeout=10.0, retry_interval=0.05):
    """Cross-platform file lock using atomic .lock-file creation.

    Used to serialise writes to shared files (cost_log.jsonl, hook scope) when
    multiple agents (ai_dougs / vault.py daemon, ai_sharpener) might write concurrently,
    or when run_vault.py restarts an agent mid-write.

    Stale locks (older than timeout * 2) are forcibly removed to prevent deadlock
    after a process crash. On lock timeout, the caller proceeds with a warning
    rather than blocking Ã¢â‚¬â€ corruption is rare and a missed lock is preferable to
    a hung pipeline.
    """
    target_path = Path(target_path)
    lock_path = Path(str(target_path) + ".lock")
    deadline = time.monotonic() + timeout
    acquired = False

    while time.monotonic() < deadline:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            acquired = True
            break
        except FileExistsError:
            try:
                age = time.time() - lock_path.stat().st_mtime
                if age > timeout * 2:
                    lock_path.unlink()
                    continue
            except (OSError, FileNotFoundError):
                continue
            time.sleep(retry_interval)

    if not acquired:
        print(f"    {YELLOW}Ã¢Å¡Â  Lock timeout on {target_path.name} Ã¢â‚¬â€ proceeding without lock{RESET}")

    try:
        yield acquired
    finally:
        if acquired:
            try:
                lock_path.unlink()
            except (OSError, FileNotFoundError):
                pass


def _escalateProviderOrder(provider_order, attempt):
    """Return a modified provider order with upgraded model tier based on attempt count.

    attempt 1Ã¢â‚¬â€œ2 Ã¢â€ â€™ unchanged (default routing)
    attempt 3   Ã¢â€ â€™ force mid tier (e.g. sonnet)
    attempt 4+  Ã¢â€ â€™ force best tier (e.g. opus)
    """
    if attempt < 3:
        return provider_order

    tier_index = 1 if attempt == 3 else 2  # index into MODEL_TIERS tuple

    escalated = []
    for entry in provider_order:
        provider = entry.get("provider", "claude")
        tiers = MODEL_TIERS.get(provider)
        if tiers and len(tiers) > tier_index:
            forced_model = tiers[tier_index]
            new_entry = dict(entry)
            new_entry["model"] = forced_model
            escalated.append(new_entry)
            print(f"    {YELLOW}Ã¢Å¡Â¡ Attempt {attempt}: escalating model Ã¢â€ â€™ {forced_model}{RESET}")
        else:
            escalated.append(entry)

    return escalated


def _updateRoutingParams(param_suggestions):
    """Apply learning-agent param suggestions to logs/routing_params.json.

    param_suggestions: dict of {param_name: new_value} from ##PARAMS_START## parsing.
    Only updates keys that exist in _ROUTING_PARAMS_DEFAULTS Ã¢â‚¬â€ ignores unknown keys.
    """
    if not param_suggestions:
        return

    logs_dir = Path(__file__).parent.parent.parent / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    params_file = logs_dir / "routing_params.json"

    try:
        current = _loadRoutingParams()
        changed = []
        for key, value in param_suggestions.items():
            if key in _ROUTING_PARAMS_DEFAULTS and isinstance(value, (int, float)):
                old = current.get(key)
                if old != value:
                    current[key] = value
                    changed.append(f"{key}: {old} Ã¢â€ â€™ {value}")

        if changed:
            params_file.write_text(json.dumps(current, indent=2), encoding="utf-8")
            print(f"    {GREEN}Ã¢Å“â€œ Routing params updated: {', '.join(changed)}{RESET}")
            # Invalidate cache
            _routing._routing_params_cache = {}
            _routing._routing_params_mtime = 0.0
    except Exception as e:
        print(f"    {YELLOW}[WARN] Could not update routing params: {e}{RESET}")


def _logContextInjection(task_name, phase):
    """No-op. context_log.jsonl was write-only telemetry nobody read; the
    diagnostics CLI covers what we actually inspect. Kept as a no-op so the
    four call sites (planning/execution/refinement) don't need touching."""
    return


def loadAiMain(vault_root):
    """Read ai_main.md once at startup; cache content and vault root."""
    global _AI_MAIN_CONTENT, _VAULT_ROOT
    _VAULT_ROOT = Path(vault_root)
    path = _VAULT_ROOT / "ai_main.md"
    if path.exists():
        _AI_MAIN_CONTENT = path.read_text(encoding="utf-8").strip()
        print(f"{DIM}  ai_main.md loaded ({len(_AI_MAIN_CONTENT)} chars){RESET}")
    else:
        print(f"{YELLOW}  [WARN] ai_main.md not found Ã¢â‚¬â€ proceeding without vault rules{RESET}")


def _selectContextFiles():
    """Stub. Standards keyword system removed 2026-05-12.
    Kept as a no-op for backward compatibility with callers."""
    return []


def scanSkillsForTask(task_text):
    """Scan ai_skills/ subdirs for SKILL.md files whose triggers match task_text.

    Each SKILL.md may have a frontmatter field:
      triggers: react, electron, vite, ipc
    Agent-generated skills always have this field.  Legacy skills without it
    fall back to matching on their name + description fields.
    Returns list of Paths to matching SKILL.md files.
    """
    # 2026-05-12: delegates to vault_graph.skills.select_relevant_skills,
    # the canonical Anthropic Skills SDK loader (description-as-trigger).
    if not _VAULT_ROOT:
        return []
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        try:
            from vault_graph.skills import select_relevant_skills
        finally:
            try:
                sys.path.pop(0)
            except IndexError:
                pass
    except ImportError:
        return []
    matched_skills = select_relevant_skills(task_text, max_skills=3)
    return [Path(s["path"]) for s in matched_skills if s.get("path")]


def _selectContextFilesForTask(task_text):
    """Return the context file list for a task: skills only.

    Standards keyword-injection + the legacy in-project .agent-context.md
    path were removed 2026-05-12. Per-project charter is injected
    separately by project_memory.load_project_memory_context in the
    planning node.
    """
    if not _VAULT_ROOT:
        return []
    return scanSkillsForTask(task_text)


def _extractSkillSummary(content, full_fallback_cap=3500):
    """Extract only the actionable parts from a SKILL.md for injection.

    For AI-generated skills (with ### What to avoid + ### Reusable pattern):
      Keeps:  YAML frontmatter + What to avoid + Reusable pattern (~70-90% reduction)
      Strips: What works (human-readable narrative, not useful for prompting)

    For hand-written skills (different heading format):
      Falls back to full content capped at full_fallback_cap chars.
      Better than dropping to just frontmatter.
    """
    parts = []
    fm_text = ""

    # Keep YAML frontmatter
    if content.startswith("---"):
        fm_end = content.find("---", 3)
        if fm_end != -1:
            fm_text = content[:fm_end + 3]
            parts.append(fm_text)

    def _get_section(text, heading):
        # Match both ### and ## headings
        m = re.search(
            rf"(?:##{{1,3}})\s+{re.escape(heading)}\s*\n(.*?)(?=\n##|\Z)",
            text,
            re.DOTALL | re.IGNORECASE,
        )
        return m.group(1).strip() if m else None

    avoid = _get_section(content, "What to avoid")
    if avoid and avoid.lower() not in ("none", ""):
        parts.append(f"### What to avoid\n{avoid}")

    pattern = _get_section(content, "Reusable pattern")
    if pattern and pattern.lower() not in ("none", ""):
        pattern_lines = pattern.split("\n")[:25]
        parts.append("### Reusable pattern\n" + "\n".join(pattern_lines))

    # If we found actionable sections, return compressed version
    if len(parts) > 1:  # More than just frontmatter
        return "\n\n".join(parts)

    # Hand-written skill with different format Ã¢â‚¬â€ use full content capped to budget
    return content[:full_fallback_cap] + (
        "\n\n[... skill truncated for token budget]" if len(content) > full_fallback_cap else ""
    )


def _loadContextBlock(file_paths):
    """Read context files and return a formatted markdown block.

    SKILL.md files are compressed to actionable sections only
    (frontmatter + What to avoid + Reusable pattern).
    All other files are included verbatim.
    """
    parts = []
    for p in file_paths:
        try:
            content = Path(p).read_text(encoding="utf-8").strip()
            if not content:
                continue
            if Path(p).name == "SKILL.md":
                content = _extractSkillSummary(content)
            parts.append(f"### {Path(p).name}\n\n{content}")
        except Exception:
            pass
    return "\n\n".join(parts)


def buildTaskPreamble(task):
    """Build the full prompt: ai_main.md + keyword-selected context + task.

    Works identically for all AI providers (Claude, Gemini, Codex, Cursor).
    Context is pre-selected in Python before the model call Ã¢â‚¬â€ no token waste
    asking the model to discover which files are relevant.
    """
    global _context_injection_log
    sections = []
    if _AI_MAIN_CONTENT:
        sections.append(_AI_MAIN_CONTENT)
    ctx_files = _selectContextFilesForTask(task)
    # Populate injection log before loading context
    _context_injection_log = {
        p.name: max(1, p.stat().st_size // 4)
        for p in ctx_files
        if p.exists()
    }
    if ctx_files:
        ctx_block = _loadContextBlock(ctx_files)
        if ctx_block:
            names = [Path(p).name for p in ctx_files]
            sections.append(
                f"## Vault Context (pre-selected: {', '.join(names)})\n\n{ctx_block}"
            )
    sections.append(f"TASK:\n{task}")
    return "\n\n---\n\n".join(sections)

RATE_LIMIT_SIGNALS = [
    # Rate / quota limits
    "rate limit exceeded",
    "rate_limit_exceeded",
    "ratelimiterror",
    "resource_exhausted",
    "quota exceeded",
    "quota_exceeded",
    "you've hit your limit",
    "you hit your limit",
    "hit your usage limit",          # Bug Q (2026-05-10): codex CLI phrasing — "You've hit your usage limit"
    "usage limit reached",
    "upgrade to plus to continue",   # Bug Q: codex CLI follow-up: "Upgrade to Plus to continue using Codex"
    "too many requests",
    "overloaded_error",
    "exhausted your capacity",       # gemini CLI phrasing
    "your quota will reset",         # gemini CLI follow-up phrasing
    "insufficient_quota",            # OpenAI API phrasing
    "exceeded your current quota",   # OpenAI long form
    "credit balance is too low",     # claude API/Max phrasing when credits exhausted
    "credit balance",                # broader catch for any "credit balance" warning
    "billing details",               # OpenAI generic billing errors
    "plan and billing",              # OpenAI "check your plan and billing"
    # Context window exceeded Ã¢â‚¬â€ fall back to a provider with a larger window
    "context_length_exceeded",
    "maximum context length",
    "prompt is too long",
    "prompt too long",
    "request too large",
    "input too long",
    "exceeds the maximum",
    "reduce the length",
    "tokens_limit_reached",
]

RATE_LIMIT_STATUS_CODES = ["429", "529", "413", "error 429", "error 529", "error 413", "status: 429", "status: 529"]

VARIABLE_MODEL_VALUES = {"variable", "decide"}

# Hardcoded fallback model pool. Live values are loaded from vault/model_pool.json
# at startup if it exists Ã¢â‚¬â€ that file is human-editable so new models can be added
# without code changes. Tuple position is initial test order, NOT a quality
# ranking. Real ranking comes from logs/cost_log.jsonl via _routeModel.
_DEFAULT_MODEL_TIERS = {
    "claude": ("haiku", "sonnet", "opus"),
    "gemini": ("gemini-2.5-flash-lite", "gemini-2.5-flash", "gemini-2.5-pro"),
    "codex":  ("gpt-5.4-mini", "gpt-5.4-nano", "gpt-5-codex"),
}


def _loadModelPool():
    """Load model pool from vault/model_pool.json, falling back to defaults.

    The JSON is the source of truth when present. The defaults exist only so
    fresh installs work before model_pool.json is created. Returns a dict
    mapping provider name -> tuple of model names in initial test order.
    """
    pool_path = Path(__file__).parent.parent.parent / "state" / "model_pool.json"
    if not pool_path.exists():
        return dict(_DEFAULT_MODEL_TIERS)
    try:
        data = json.loads(pool_path.read_text(encoding="utf-8"))
        providers = data.get("providers", {})
        if not isinstance(providers, dict):
            return dict(_DEFAULT_MODEL_TIERS)
        loaded = {}
        for provider, models in providers.items():
            if isinstance(models, list) and models:
                loaded[provider] = tuple(models)
        # Merge with defaults so partial JSON doesn't drop providers
        merged = dict(_DEFAULT_MODEL_TIERS)
        merged.update(loaded)
        return merged
    except Exception:
        return dict(_DEFAULT_MODEL_TIERS)


MODEL_TIERS = _loadModelPool()
_routing.MODEL_TIERS = MODEL_TIERS
_routing._last_routing_source_setter = lambda source: globals().__setitem__("_last_routing_source", source)
_configureCostTracking(model_tiers=MODEL_TIERS, routing_params_loader=_loadRoutingParams)

# Appended after AI output at end of task file so the human always sees what to do last.
HUMAN_FOOTER_AFTER_PLAN = (
    "**Your turn:** If the plan listed clarifying questions, answer them below this line. "
    "If there were none, add any notes or skip straight to refinement. "
    "When finished, set frontmatter `status` to `pending_ai_refinement`.\n"
)

HUMAN_FOOTER_AFTER_REFINEMENT = (
    "Review the refined plan above. When ready for execution, set frontmatter `status` to `pending_ai_execution`.\n"
)

HUMAN_FOOTER_AFTER_EXECUTION = (
    "Review the execution output above. When ready to capture learnings, set `status` to `pending_ai_learning`. "
    "If another execution pass is needed, describe it here and set `status` back to `pending_ai_execution`.\n"
)

VISUAL_VALIDATION_PROMPT = (
    "This is a screenshot of a React web app immediately after an automated code change. "
    "Does the UI look functional? Specifically check: "
    "Is the page blank or white? Is the layout broken or overlapping? "
    "Are there any visible error messages or stack traces? "
    "Is text readable and elements positioned correctly? "
    "Respond in one short paragraph. End with either PASS or FAIL on its own line."
)


def parseLearningOutput(response):
    """Extract the consumed sections from the learning prompt response.

    2026-05-12: trimmed to (model_content, params_content). SKILL / PROJECT /
    COST / CONTEXT sections were retired with the auto-curator, project-
    context auto-write, and context-effectiveness pruning. Returns a 2-tuple;
    either element may be None if missing or 'none'.
    """
    def _extract(start_tag, end_tag, text):
        pattern = re.escape(start_tag) + r"\n(.*?)" + re.escape(end_tag)
        m = re.search(pattern, text, re.DOTALL)
        if m:
            content = m.group(1).strip()
            return content if content and content.lower() != "none" else None
        return None

    model = _extract("##MODEL_START##", "##MODEL_END##", response)
    params = _extract("##PARAMS_START##", "##PARAMS_END##", response)
    return model, params


def _parseParamSuggestions(params_content):
    """Parse ##PARAMS_START## block content into a dict of {key: value}.

    Only keys present in _ROUTING_PARAMS_DEFAULTS are accepted.
    Values must be numeric (int or float). Ignores comment lines (starting with #).
    Returns {} if params_content is None or no valid keys found.
    """
    if not params_content:
        return {}
    result = {}
    for line in params_content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, val_str = line.partition(":")
        key = key.strip()
        val_str = val_str.strip()
        if key not in _ROUTING_PARAMS_DEFAULTS:
            continue
        try:
            val = float(val_str)
            # Convert to int if it's a whole number and the default is int
            if isinstance(_ROUTING_PARAMS_DEFAULTS[key], int) and val == int(val):
                val = int(val)
            result[key] = val
        except ValueError:
            pass
    return result


# Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬ Skill curation (Anthropic Agent Skills standard alignment) Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬
# A skill is a CURATED PROCEDURAL PLAYBOOK, not a chronological log of lessons.
# When learning extracts a new lesson for an existing category, we trigger a
# small follow-up LLM call ("curation") that merges the new lesson into the
# existing playbook Ã¢â‚¬â€ keeping useful specifics, dropping redundancy, preserving
# the standard's structure (frontmatter with name+description, body sections
# Purpose / When to use / Step-by-step / Common pitfalls / Examples).
#
# This is the second LLM call in the learning phase. It's cheap (small prompt,
# small response) and runs against the same provider rotation as the rest of
# the system, so per-model performance data accrues here too.


def updateModelPerformance(nasRoot, model_content):
    """Append a model performance entry to ai_context/model_performance.md.

    This file is read by the model resolver to prefer models that have worked
    well for specific task types in the past.
    Returns True on success, False if skipped.
    """
    if not model_content:
        return False
    perf_file = nasRoot / "ai_context" / "model_performance.md"
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    entry = f"\n---\n*Logged {timestamp}*\n{model_content}\n"

    if not perf_file.exists():
        header = (
            "# Model Performance Log\n\n"
            "Auto-updated by the learning stage after each completed task.\n"
            "The model resolver reads this file to route task types to the\n"
            "models that have historically worked best for them.\n\n"
            "Format per entry: task_type Ã¢â€ â€™ provider/model Ã¢â€ â€™ outcome Ã¢â€ â€™ notes\n"
        )
        perf_file.write_text(header + entry, encoding="utf-8")
        print(f"    {GREEN}Ã¢Å“â€œ Created model performance log{RESET}")
    else:
        with open(perf_file, "a", encoding="utf-8") as f:
            f.write(entry)
        print(f"    {DIM}Ã¢â€ â€™ Model performance logged{RESET}")
    return True


# Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬ Hook scope file (shared with scripts/hooks/*.py) Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬
HOOKS_SCOPE_FILE = Path(__file__).parent.parent / "hooks" / ".scope.json"


def _writeHookScope(task_name, files_blocked, files_allowed, app_root, nasRoot):
    """Write current task scope for hook enforcement during execution.

    The hook scripts (pre_write.py, pre_bash.py) read this file to know which
    paths are allowed/blocked for the currently-executing task.
    """
    blocked = [f.strip() for f in files_blocked.split(",") if f.strip()] if files_blocked else []
    allowed = [f.strip() for f in files_allowed.split(",") if f.strip()] if files_allowed else []
    scope = {
        "task": task_name,
        "files_blocked": blocked,
        "files_allowed": allowed,
        "app_root": str(app_root),
        "vault_root": str(nasRoot),
        "ts": datetime.now().isoformat(),
    }
    HOOKS_SCOPE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with _fileLock(HOOKS_SCOPE_FILE, timeout=5.0):
        HOOKS_SCOPE_FILE.write_text(json.dumps(scope, indent=2), encoding="utf-8")
    if blocked or allowed:
        print(f"    {DIM}Ã¢â€ â€™ Hook scope: blocked={blocked}  allowed={allowed}{RESET}")


def _clearHookScope():
    """Clear hook scope after execution so hooks impose no restrictions."""
    if HOOKS_SCOPE_FILE.exists():
        with _fileLock(HOOKS_SCOPE_FILE, timeout=5.0):
            HOOKS_SCOPE_FILE.write_text("{}", encoding="utf-8")


# Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬ Traceability Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬

def _logTrace(nasRoot, task_name, event, provider=None, model=None):
    """No-op. agent_trace.md writer removed 2026-05-12; cost_log.jsonl +
    the orchestrator JSONL transcript cover the same ground. Kept as a
    no-op so existing call sites do not need touching."""
    return


# Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬ Self-review helpers Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬

# Noise patterns that always appear in JS builds Ã¢â‚¬â€ not the agent's fault.
_BUILD_NOISE = [
    "browserslist",
    "caniuse-lite",
    "update-browserslist-db",
    "baseline-browser-mapping",
    "deprecationwarning",
    "onaftersetupmiddleware",
    "onbeforesetupmiddleware",
    "npm warn",
    "npm notice",
    "starting the development server",
    "compiled with warning",
    "webpack compiled",
    "search for the keywords",
    "to ignore, add",
    "why you should do it regularly",
]


def _filterLintWarnings(build_output, app_path):
    """Extract only actionable ESLint/compiler warnings in files the agent changed.

    Returns a trimmed warning string if there's something worth fixing,
    or None if all warnings are pre-existing noise unrelated to the agent's edits.
    """
    if not build_output:
        return None

    # Get list of files the agent actually modified in this git session
    modified_files = set()
    try:
        r = subprocess.run(
            ["git", "diff", "--name-only", "HEAD"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(app_path), timeout=10,
        )
        if r.returncode == 0:
            for line in r.stdout.strip().splitlines():
                modified_files.add(Path(line).name)   # just filename, e.g. "LandingPage.js"
                modified_files.add(line.replace("\\", "/"))  # also full path
    except Exception:
        pass

    meaningful = []
    lines = build_output.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        lower = line.lower()

        # Skip noise lines
        if any(noise in lower for noise in _BUILD_NOISE):
            i += 1
            continue

        # ESLint block header: "src\App.js" or "src/LandingPage.js"
        # These appear as standalone file-path lines followed by warning lines
        if re.match(r'^\s*(src[\\/]\S+\.(js|jsx|ts|tsx|css))\s*$', line):
            fname = line.strip()
            fname_base = Path(fname.replace("\\", "/")).name

            # Only include if agent modified this file (or we have no diff info)
            if not modified_files or fname_base in modified_files or fname.replace("\\", "/") in modified_files:
                block = [line]
                i += 1
                # Collect the warning lines that follow
                while i < len(lines) and lines[i].strip().startswith("Line"):
                    block.append(lines[i])
                    i += 1
                if len(block) > 1:  # has actual warnings, not just the filename
                    meaningful.extend(block)
                continue
            i += 1
            continue

        i += 1

    return "\n".join(meaningful).strip() or None


def _leanAgentCall(prompt, cwd, addDirs, providerOrder, sendAiRequests):
    """Run a lean agent call WITHOUT the full buildTaskPreamble overhead.

    Used for self-review passes where we only need the diff + warnings in context.
    Saves ~80% of input tokens vs a full runAiRequestWithFallback call.
    Supports all providers ranked by _rankProviders Ã¢â‚¬â€ not Claude-only.
    """
    ranked = _rankProviders(providerOrder)

    for entry in ranked:
        provider = entry.get("provider", "claude")
        model = entry.get("model", "")
        if model.lower() in VARIABLE_MODEL_VALUES:
            model, _ = _routeModel(prompt[:500], provider, sendAiRequests, phase="execution")

        # Build lean command per provider (agent mode Ã¢â‚¬â€ file access enabled)
        cmd, _use_stdin = build_cli_command_for_prompt(
            provider, model, agent_mode=True, cwd=cwd, addDirs=addDirs
        )
        if not cmd:
            continue

        if not sendAiRequests:
            print(f"    {YELLOW}[DRY RUN] lean agent call ({provider}) Ã¢â‚¬â€ prompt_len={len(prompt):,}{RESET}")
            return "[DRY RUN]"

        print(f"    {YELLOW}Ã¢â€ â€™ Lean review call ({provider}, stdin, {len(prompt):,} chars)...{RESET}")
        # Scrub API keys from env so the CLI uses subscription auth (see runAiRequest)
        clean_env = {k: v for k, v in os.environ.items()
                     if k not in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY",
                                  "GEMINI_API_KEY", "GOOGLE_API_KEY")}
        result = run_with_animation(
            cmd, prompt[:50],
            capture_output=True, text=True, encoding="utf-8",
            input=prompt, cwd=str(cwd),
            env=clean_env,
        )
        if isinstance(result, Exception):
            print(f"    {RED}[ERROR] Lean review failed ({provider}): {result}{RESET}")
            continue  # try next ranked provider
        # Same returncode check as runAiRequest Ã¢â‚¬â€ non-zero exits with only banner
        # text in stderr would otherwise be treated as successful responses.
        if getattr(result, "returncode", 0) != 0:
            rc_output = (result.stdout or "") + (result.stderr or "")
            is_limited, _ = _isRateLimited(rc_output, result.returncode)
            if is_limited:
                print(f"    {ORANGE}[FALLBACK] {provider} rate-limited on lean (rc={result.returncode}) Ã¢â‚¬â€ trying next...{RESET}")
                continue
            print(f"    {RED}[ERROR] Lean review {provider} exited rc={result.returncode}; trying next...{RESET}")
            continue
        output = (result.stdout or "") + (result.stderr or "")
        is_limited, _ = _isRateLimited(output, result.returncode)
        if is_limited:
            print(f"    {ORANGE}[FALLBACK] {provider} rate-limited on lean call Ã¢â‚¬â€ trying next...{RESET}")
            continue

        in_tok = _estimateTokens(prompt)
        out_tok = _estimateTokens(output) if output else 0
        _recordCallCost(in_tok, out_tok, model or "unknown")
        return output.strip() or None

    return None


# Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬ Git branch isolation Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬

def _setupGitBranch(app_path, task_name):
    """Create a task branch for execution isolation.

    Returns (branch_name, base_branch) or (None, None) if app_path is not a git repo.
    The agent works in this branch; the human merges it after review.
    """
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(app_path), timeout=10,
        )
        if r.returncode != 0:
            return None, None

        base_r = subprocess.run(
            ["git", "branch", "--show-current"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(app_path), timeout=10,
        )
        base_branch = base_r.stdout.strip() or "main"

        safe = re.sub(r"[^a-z0-9]", "-", task_name.lower())[:40].strip("-")
        branch = f"agent/{safe}"

        r2 = subprocess.run(
            ["git", "checkout", "-b", branch],
            capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(app_path), timeout=10,
        )
        if r2.returncode == 0:
            print(f"    {GREEN}Ã¢Å“â€œ Branch created: {branch}{RESET}")
            return branch, base_branch

        # Branch exists Ã¢â‚¬â€ switch to it
        r3 = subprocess.run(
            ["git", "checkout", branch],
            capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(app_path), timeout=10,
        )
        if r3.returncode == 0:
            print(f"    {DIM}Ã¢â€ â€™ Resumed branch: {branch}{RESET}")
            return branch, base_branch

        return None, None
    except Exception as e:
        print(f"    {YELLOW}[WARN] Git branch setup: {e}{RESET}")
        return None, None


def _captureDiff(app_path, base_branch):
    """Return git diff of current branch vs base_branch, truncated to 5 000 chars."""
    if not base_branch:
        return None
    try:
        r = subprocess.run(
            ["git", "diff", base_branch],
            capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(app_path), timeout=30,
        )
        if r.returncode == 0 and r.stdout.strip():
            diff = r.stdout.strip()
            if len(diff) > 5000:
                diff = (
                    diff[:5000]
                    + f"\n\n[diff truncated Ã¢â‚¬â€ run `git diff {base_branch}` for full output]"
                )
            return diff
    except Exception:
        pass
    return None


def _revertGitChanges(app_path):
    """Revert all uncommitted working-tree changes (safe reset after build failure)."""
    try:
        subprocess.run(
            ["git", "checkout", "."],
            capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(app_path), timeout=30,
        )
        print(f"    {YELLOW}Ã¢Å¸Â² Changes reverted (build gate failed){RESET}")
    except Exception as e:
        print(f"    {YELLOW}[WARN] Could not revert changes: {e}{RESET}")


def _autoCommit(app_path, task_name, branch_name):
    """Commit all working-tree changes to the current branch after a successful build.

    This makes the branch state clean so the human can:
      - Run `git checkout <branch>` + `npm start` to preview
      - See a clean diff with `git diff main`
      - Merge or discard the branch easily

    Does NOT push Ã¢â‚¬â€ that remains a manual human action.
    Returns True on success.
    """
    try:
        # Stage everything
        r_add = subprocess.run(
            ["git", "add", "-A"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(app_path), timeout=30,
        )
        if r_add.returncode != 0:
            print(f"    {YELLOW}[WARN] git add failed Ã¢â‚¬â€ changes remain unstaged{RESET}")
            return False

        # Check there's actually something to commit
        r_status = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(app_path), timeout=10,
        )
        if not r_status.stdout.strip():
            print(f"    {DIM}Ã¢â€ â€™ Nothing to commit (files unchanged){RESET}")
            return True

        commit_msg = f"agent: {task_name[:60]}"
        r_commit = subprocess.run(
            ["git", "commit", "-m", commit_msg],
            capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(app_path), timeout=30,
        )
        if r_commit.returncode == 0:
            print(f"    {GREEN}Ã¢Å“â€œ Committed to branch: {branch_name}{RESET}")
            print(f"    {DIM}  Preview: cd {app_path.name}/frontend && npm start{RESET}")
            return True
        else:
            print(f"    {YELLOW}[WARN] git commit failed: {r_commit.stderr.strip()[:100]}{RESET}")
            return False
    except Exception as e:
        print(f"    {YELLOW}[WARN] Auto-commit error: {e}{RESET}")
        return False


# Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬ Quality gate Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬

def _runBuildGate(build_command, nasRoot):
    """Run the project build command and return (passed: bool, output: str|None).

    Uses cmd /c on Windows so && chaining works (PowerShell 5.1 doesn't support &&).
    Caps output at 2 000 chars (tail Ã¢â‚¬â€ where errors/warnings live).
    Does not raise Ã¢â‚¬â€ gate infrastructure failure Ã¢â€ â€™ (True, None) so it never
    blocks execution due to a hook problem.

    Returns:
      (True,  None)         Ã¢â‚¬â€ clean build, no warnings
      (True,  warning_text) Ã¢â‚¬â€ built successfully but with warnings
      (False, error_text)   Ã¢â‚¬â€ build failed
    """
    if not build_command:
        return True, None

    print(f"    {CYAN}Ã¢Å¡â„¢ Build gate: {build_command[:70]}{RESET}")

    try:
        if sys.platform == "win32":
            # Use cmd /c so && chaining works (PowerShell 5.1 doesn't support &&)
            cmd = ["cmd", "/c", build_command]
            result = subprocess.run(
                cmd, capture_output=True, text=True, encoding="utf-8", timeout=_loadRoutingParams()["build_timeout_sec"], cwd=str(nasRoot),
            )
        else:
            result = subprocess.run(
                build_command, shell=True, capture_output=True, text=True,
                encoding="utf-8", timeout=_loadRoutingParams()["build_timeout_sec"], cwd=str(nasRoot),
            )

        combined = (result.stdout + "\n" + result.stderr).strip()
        combined = combined[-3000:] if len(combined) > 3000 else combined

        if result.returncode == 0:
            # Check for lint/compiler warnings even on a passing build
            warn_markers = ("warning", "[eslint]", "compiled with warning", "deprecation")
            has_warnings = any(m in combined.lower() for m in warn_markers)
            if has_warnings:
                print(f"    {YELLOW}Ã¢Å¡Â  Build passed with warnings{RESET}")
                return True, combined  # warnings returned as output
            print(f"    {GREEN}Ã¢Å“â€œ Build gate passed (clean){RESET}")
            return True, None

        print(f"    {RED}Ã¢Å“â€” Build gate FAILED (exit {result.returncode}){RESET}")
        return False, combined

    except subprocess.TimeoutExpired:
        print(f"    {RED}Ã¢Å“â€” Build gate timed out after 300s{RESET}")
        return False, "Build timed out after 300 seconds."
    except Exception as e:
        print(f"    {YELLOW}[WARN] Build gate error: {e} Ã¢â‚¬â€ skipping{RESET}")
        return True, None  # Gate infrastructure failure Ã¢â€ â€™ don't block


def _runVisualValidation(app_path, task_name, nasRoot, visual_reference=None):
    """Take a screenshot of the built app and validate UI visually.

    If visual_reference is provided (path relative to nasRoot), screenshots both
    the reference HTML and the built output, then runs a design-fidelity comparison
    with both images. Without a reference, falls back to crash/blank detection only.

    Returns (passed: bool, notes: str, screenshot_path: str|None).
    Gracefully skips if Playwright not installed, no API key, or no build dir.
    Never crashes or blocks task completion.
    """
    logs_dir = Path(__file__).parent.parent.parent / "logs"
    screenshot_dir = logs_dir / "screenshots"

    try:
        import playwright
    except ImportError:
        return True, "visual validation skipped: playwright not installed", None

    if not os.environ.get("ANTHROPIC_API_KEY"):
        return True, "visual validation skipped: no API key", None

    if not (app_path / "build").exists():
        return True, "visual validation skipped: no build dir", None

    # Resolve reference path if provided
    ref_path = None
    if visual_reference:
        ref_path = (nasRoot / visual_reference).resolve()
        if not ref_path.exists():
            print(f"    {YELLOW}[WARN] visual_reference not found: {ref_path} Ã¢â‚¬â€ falling back to crash check{RESET}")
            ref_path = None

    try:
        import socket
        import base64
        import anthropic
        from playwright.sync_api import sync_playwright

        # Find a free port
        sock = socket.socket()
        sock.bind(("", 0))
        port = sock.getsockname()[1]
        sock.close()

        # Start http.server subprocess
        server_proc = subprocess.Popen(
            [sys.executable, "-m", "http.server", str(port)],
            cwd=str(app_path / "build"),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        try:
            # Give server a moment to start
            time.sleep(0.5)

            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            safe_name = re.sub(r"[^\w\-]", "_", task_name)
            screenshot_path = screenshot_dir / f"{safe_name}_{ts}.png"
            screenshot_dir.mkdir(parents=True, exist_ok=True)

            ref_screenshot_path = None

            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)

                # Screenshot the reference first (if provided)
                if ref_path:
                    ref_screenshot_path = screenshot_dir / f"{safe_name}_{ts}_ref.png"
                    ref_page = browser.new_page(viewport={"width": 1440, "height": 900})
                    ref_file_url = f"file:///{ref_path.as_posix()}"
                    ref_page.goto(ref_file_url, timeout=10000)
                    ref_page.wait_for_load_state("networkidle")
                    time.sleep(0.5)  # allow CSS animations to settle
                    ref_page.screenshot(path=str(ref_screenshot_path), full_page=False)
                    ref_page.close()

                # Screenshot the built app
                page = browser.new_page(viewport={"width": 1440, "height": 900})
                page.goto(f"http://localhost:{port}", timeout=10000)
                page.wait_for_load_state("networkidle")
                time.sleep(0.5)
                page.screenshot(path=str(screenshot_path), full_page=False)
                page.close()

                browser.close()

            _p = _loadRoutingParams()
            vision_model = _p.get("vision_model", "claude-haiku-4-5-20251001")
            vision_max_tokens = _p.get("vision_max_tokens", 300)

            # Build vision message content
            if ref_screenshot_path and ref_screenshot_path.exists():
                # Design-fidelity comparison Ã¢â‚¬â€ needs more tokens
                vision_max_tokens = max(vision_max_tokens, 700)
                with open(ref_screenshot_path, "rb") as f:
                    ref_img = base64.standard_b64encode(f.read()).decode("utf-8")
                with open(screenshot_path, "rb") as f:
                    out_img = base64.standard_b64encode(f.read()).decode("utf-8")

                content = [
                    {"type": "text",
                     "text": "Image 1 is the REFERENCE design. Image 2 is the BUILT output."},
                    {"type": "image",
                     "source": {"type": "base64", "media_type": "image/png", "data": ref_img}},
                    {"type": "image",
                     "source": {"type": "base64", "media_type": "image/png", "data": out_img}},
                    {"type": "text", "text": VISUAL_COMPARISON_PROMPT},
                ]
                print(f"    {DIM}Ã¢â€ â€™ Visual: comparing output against reference '{visual_reference}'{RESET}")

            else:
                # Crash/blank check only
                with open(screenshot_path, "rb") as f:
                    out_img = base64.standard_b64encode(f.read()).decode("utf-8")

                content = [
                    {"type": "image",
                     "source": {"type": "base64", "media_type": "image/png", "data": out_img}},
                    {"type": "text", "text": VISUAL_VALIDATION_PROMPT},
                ]

            client = anthropic.Anthropic()
            message = client.messages.create(
                model=vision_model,
                max_tokens=vision_max_tokens,
                messages=[{"role": "user", "content": content}],
            )

            response_text = message.content[0].text if message.content else ""

            # Check for PASS/FAIL on the last non-empty line of the response
            last_line = next((l for l in reversed(response_text.upper().splitlines()) if l.strip()), "")
            passed = "FAIL" not in last_line

            return passed, response_text, str(screenshot_path)

        finally:
            server_proc.terminate()
            try:
                server_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server_proc.kill()

    except Exception as e:
        return True, f"visual validation skipped: {e}", None


# Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬ Scope block for execution prompt Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬

def _buildScopeBlock(files_blocked, files_allowed, research_allowed=False):
    """Format hard constraints for injection at the top of the execution prompt."""
    lines = [
        "## HARD CONSTRAINTS Ã¢â‚¬â€ read before touching any file",
        "- NEVER run `git push` or any remote push command",
        "- NEVER modify files outside the allowed scope below",
    ]
    if files_allowed:
        lines.append(f"- **Files you MAY write:** `{files_allowed}`")
    if files_blocked:
        lines.append(f"- **Files you MUST NOT write:** `{files_blocked}` Ã¢â‚¬â€ hooks will block these")
    if research_allowed:
        lines.append("")
        lines.append("## RESEARCH GATE Ã¢â‚¬â€ external fetching is enabled for this task")
        lines.append("- You may ONLY fetch URLs listed in the task's `## Research Approved` section.")
        lines.append("- Do NOT fetch any URL not explicitly approved, even if it seems helpful.")
        lines.append("- Approved fetch commands: `curl -sL <url>`, `playwright screenshot <url> <path>.png`")
        lines.append("- Save all fetched content to a `references/` subfolder inside the project.")
        lines.append("- Research before building Ã¢â‚¬â€ fetch approved references first, then design from them.")
    else:
        lines.append("- **No external fetching** Ã¢â‚¬â€ do not run curl, wget, playwright, or any network command. "
                     "All required context is in the task file or local vault.")
    lines.append("")
    return "\n".join(lines)


# Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬ Project scout (pure Python, no LLM) Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬

def scoutProject(app_path, max_lines=120, prompt_cap=7_000):
    """Read project source files and return a COMPACT snapshot for planning prompts.

    Runs before the planning AI call so the model sees actual current file
    contents.  Pure Python Ã¢â‚¬â€ no LLM.

    Design constraints:
      - prompt_cap (default 7 000 chars) keeps the total prompt safely under the
        Windows CreateProcess command-line limit of 32 767 chars.
      - Files Ã¢â€°Â¤ max_lines are included in full; larger files are tree-only.
      - Small focused files (CSS, config) always get included in full even if
        close to max_lines so the planner sees the current styling baseline.

    For a FULL snapshot (all files, unlimited), call scoutProjectFull().
    """
    SOURCE_EXTS = {
        ".js", ".jsx", ".ts", ".tsx", ".css", ".scss", ".sass",
        ".py", ".html", ".json", ".md", ".yaml", ".yml", ".env.example",
    }
    EXCLUDE_DIRS = {
        "node_modules", ".git", "__pycache__", "dist", "build",
        ".next", "coverage", "venv", ".venv", ".cache", "out", ".turbo",
        "_human_notes",  # HUMAN-ONLY zone Ã¢â‚¬â€ agents must never read/scan this
        ".claude",       # orchestrator harness config Ã¢â‚¬â€ not for vault agents
        ".backups",      # backup tarballs Ã¢â‚¬â€ not relevant context
        "_archive",      # retired skills / artifacts
    }
    EXCLUDE_FILES = {
        "package-lock.json", "yarn.lock", "setupTests.js",
        "reportWebVitals.js", ".DS_Store", "App.test.js",
    }

    app_path = Path(app_path)
    if not app_path.exists():
        return ""

    entries = []
    total_chars = 0

    for root, dirs, files in os.walk(app_path):
        dirs[:] = sorted(d for d in dirs if d not in EXCLUDE_DIRS)
        for fname in sorted(files):
            if fname in EXCLUDE_FILES:
                continue
            fpath = Path(root) / fname
            if fpath.suffix.lower() not in SOURCE_EXTS:
                continue
            rel = str(fpath.relative_to(app_path)).replace("\\", "/")
            try:
                text = fpath.read_text(encoding="utf-8", errors="replace")
                n = len(text.splitlines())
                fits_budget = total_chars + len(text) <= prompt_cap
                if n <= max_lines and fits_budget:
                    entries.append(("full", rel, n, text.strip()))
                    total_chars += len(text)
                else:
                    entries.append(("skip", rel, n, None))
            except Exception:
                continue

    if not entries:
        return ""

    out_parts = [
        f"**Scout: `{app_path.name}/`** Ã¢â‚¬â€ {len(entries)} files "
        f"({sum(1 for e in entries if e[0]=='full')} expanded, "
        f"{sum(1 for e in entries if e[0]=='skip')} tree-only)\n"
    ]

    tree_lines = []
    for kind, rel, n, _ in entries:
        mark = "Ã¢Å“â€œ" if kind == "full" else "Ã¢â€”Å’"
        tree_lines.append(f"  {mark} {rel}  ({n} lines)")
    out_parts.append("```\n" + "\n".join(tree_lines) + "\n```")

    content_parts = []
    for kind, rel, n, text in entries:
        if kind == "full" and text:
            ext = Path(rel).suffix.lstrip(".") or "text"
            content_parts.append(f"#### `{rel}`\n\n```{ext}\n{text}\n```")
    if content_parts:
        out_parts.append("\n\n".join(content_parts))

    return "\n\n".join(out_parts)


def scoutProjectFull(app_path, max_lines=400):
    """Full project snapshot written to disk Ã¢â‚¬â€ no prompt injection.

    Returns the snapshot string (caller writes it to .project-snapshot.md or
    similar). No size cap Ã¢â‚¬â€ safe because it never hits a CLI argument limit.
    """
    SOURCE_EXTS = {
        ".js", ".jsx", ".ts", ".tsx", ".css", ".scss", ".sass",
        ".py", ".html", ".json", ".md", ".yaml", ".yml", ".env.example",
    }
    EXCLUDE_DIRS = {
        "node_modules", ".git", "__pycache__", "dist", "build",
        ".next", "coverage", "venv", ".venv", ".cache", "out", ".turbo",
        "_human_notes",  # HUMAN-ONLY zone Ã¢â‚¬â€ agents must never read/scan this
        ".claude",       # orchestrator harness config Ã¢â‚¬â€ not for vault agents
        ".backups",      # backup tarballs Ã¢â‚¬â€ not relevant context
        "_archive",      # retired skills / artifacts
    }
    EXCLUDE_FILES = {
        "package-lock.json", "yarn.lock", "setupTests.js",
        "reportWebVitals.js", ".DS_Store", "App.test.js",
    }

    app_path = Path(app_path)
    if not app_path.exists():
        return ""

    entries = []
    for root, dirs, files in os.walk(app_path):
        dirs[:] = sorted(d for d in dirs if d not in EXCLUDE_DIRS)
        for fname in sorted(files):
            if fname in EXCLUDE_FILES:
                continue
            fpath = Path(root) / fname
            if fpath.suffix.lower() not in SOURCE_EXTS:
                continue
            rel = str(fpath.relative_to(app_path)).replace("\\", "/")
            try:
                text = fpath.read_text(encoding="utf-8", errors="replace")
                n = len(text.splitlines())
                entries.append(("full" if n <= max_lines else "skip", rel, n,
                                 text.strip() if n <= max_lines else None))
            except Exception:
                continue

    if not entries:
        return ""

    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    out_parts = [
        f"# Project Snapshot Ã¢â‚¬â€ {app_path.name}\n\n*Generated {ts} by scoutProjectFull()*\n"
    ]
    tree_lines = [
        f"  {'Ã¢Å“â€œ' if e[0]=='full' else 'Ã¢â€”Å’'} {e[1]}  ({e[2]} lines)"
        for e in entries
    ]
    out_parts.append("## File tree\n\n```\n" + "\n".join(tree_lines) + "\n```")

    content_parts = []
    for kind, rel, n, text in entries:
        if kind == "full" and text:
            ext = Path(rel).suffix.lstrip(".") or "text"
            content_parts.append(f"### `{rel}`\n\n```{ext}\n{text}\n```")
    if content_parts:
        out_parts.append("## File contents\n\n" + "\n\n".join(content_parts))

    return "\n\n".join(out_parts)


def truncate_observed(text: str, cutoff: int, label: str, task_id: str = "") -> str:
    """Truncate text to cutoff chars; log when truncation actually happened.

    Per the threshold-audit work (2026-05-09), context truncation is a
    SILENT QUALITY LOSS pattern: long plans, execution logs, or diffs get
    cropped before the judge/reviewer sees them, and the cropped tail
    might contain exactly the verification block / late error / true diff
    the reviewer needed. This helper makes truncation OBSERVABLE so a
    future symptom rule can fire ("execution_judge truncated input by
    73% Ã¢â‚¬â€ verdict may be unreliable") and so we have evidence to drive
    the cutoff-fixing work.

    Args:
        text: original (possibly long) string.
        cutoff: max chars to keep.
        label: human-readable site label (e.g. "execution_judge.plan",
               "plan_review.initial_prompt"). Used in the log entry to
               identify which truncation site fired.
        task_id: optional task id to attach to the observation.

    Returns:
        The truncated string. If text is short enough, returns it unchanged
        and logs nothing.
    """
    if text is None:
        return ""
    n = len(text)
    if n <= cutoff:
        return text
    try:
        log_path = findNasRoot() / "logs" / "events.jsonl"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "event": "truncation_observed",
            "task": task_id,
            "label": label,
            "original_len": n,
            "cutoff": cutoff,
            "lost_chars": n - cutoff,
            "lost_pct": round(100.0 * (n - cutoff) / n, 1),
            "details": (
                f"truncation at site {label!r}: original={n} chars, "
                f"cutoff={cutoff}, lost={n - cutoff} "
                f"({round(100.0 * (n - cutoff) / n, 1)}%). "
                f"Quality-affecting if the cropped tail contained relevant content."
            ),
        }
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass  # observation must never break the call
    return text[:cutoff]


def header(title):
    """Print section header."""
    print(f"\n{BOLD}{CYAN}{'Ã¢â€â‚¬' * W}{RESET}")
    print(f"{BOLD}{CYAN}  {title.upper()}{RESET}")
    print(f"{BOLD}{CYAN}{'Ã¢â€â‚¬' * W}{RESET}")


def divider():
    """Print divider line."""
    print(f"{DIM}{'Ã¢â€â‚¬' * W}{RESET}")


def loadConfig():
    """Load ai_dougs_config.json with optional local overrides."""
    configPath = Path(__file__).parent / "ai_dougs_config.json"
    if not configPath.exists():
        print(f"{RED}[ERROR] Config file not found: {configPath}{RESET}")
        sys.exit(1)
    with open(configPath, "r") as f:
        config = json.load(f)
    localPath = Path(__file__).parent / "ai_dougs_config.local.json"
    if localPath.exists():
        with open(localPath, "r") as f:
            config.update(json.load(f))
    return config


def findNasRoot():
    """Find root directory containing ai_main.md."""
    for p in [Path.cwd(), *Path.cwd().parents]:
        if (p / "ai_main.md").exists():
            return p
    raise FileNotFoundError("Could not find ai_main.md in current directory or any parent.")


def _isRateLimited(output, returncode):
    """Return (is_limited, matched_signal) tuple indicating rate limit status."""
    if not output:
        return False, None
    lower = output.lower()
    for sig in RATE_LIMIT_SIGNALS:
        if sig in lower:
            return True, sig
    for code in RATE_LIMIT_STATUS_CODES:
        if code in lower:
            return True, code
    if "rate limit" in lower and ("exceeded" in lower or "reached" in lower or "error" in lower):
        return True, "rate limit + exceeded/reached/error"
    return False, None


# Phrases indicating the configured model name itself is invalid (404 etc).
# Distinct from rate limits Ã¢â‚¬â€ a stale model name needs human action (update
# model_pool.json), not a retry. Used by runAiRequestWithFallback to log
# status=model_unavailable so routing permanently deprioritises dead names.
_MODEL_UNAVAILABLE_PHRASES = [
    "modelnotfounderror",
    "model not found",
    "requested entity was not found",
    '"code": 404',
    "code: 404",
    "404 not found",
    "is not a valid model",
    "unknown model",
    "no such model",
    "model does not exist",
    "it may not exist or you may not have access",  # claude CLI phrasing
    "issue with the selected model",                # claude CLI phrasing
]


def _looksLikeModelNotFound(output):
    """True if the CLI/API rejected the model name itself."""
    if not output:
        return False
    return any(p in output.lower() for p in _MODEL_UNAVAILABLE_PHRASES)


def runAiRequest(prompt, cwd, addDirs, provider, sendAiRequests, model="", agent_mode=True):
    """Build and optionally execute an AI request.

    agent_mode=True  Ã¢â€ â€™ full subprocess CLI agent session with provider
                       edit permissions. Use for execution stages where the
                       agent edits files.
    agent_mode=False Ã¢â€ â€™ simple text-in/text-out (-p only, no memory loading).
                       Use for planning, refinement, and learning stages.
    """
    fullPrompt = buildTaskPreamble(prompt)

    # Ã¢â€â‚¬Ã¢â€â‚¬ Build command and decide prompt delivery method Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬
    # Prompt delivery Ã¢â‚¬â€ provider-specific. Most CLIs accept stdin when no
    # inline value follows their prompt flag; gemini CLI requires the prompt
    # as the value of -p (verified 2026-05-04: "Not enough arguments following: p"
    # error otherwise). Windows CreateProcess arg limit is ~32 767 chars; for
    # very long prompts gemini will fail and routing falls back to next provider.
    prompt_arg = None
    if provider == "gemini":
        # gemini CLI requires -p to have a value; stdin-only doesn't work.
        # For prompts under the Windows arg limit (~30K chars), pass as arg.
        # Above that, this gemini call will fail and routing falls back.
        if len(fullPrompt) > 30000:
            print(f"    {YELLOW}[gemini] prompt too long ({len(fullPrompt)} chars) Ã¢â‚¬â€ falling back to next provider{RESET}")
            return None, False
        prompt_arg = fullPrompt
    cmd, use_stdin = build_cli_command_for_prompt(
        provider, model, agent_mode=agent_mode, cwd=cwd, prompt=prompt_arg, addDirs=addDirs
    )
    if not cmd:
        print(f"    {RED}[UNKNOWN PROVIDER: {provider}] skipping AI call.{RESET}")
        return None, False

    if not sendAiRequests:
        print(f"    {YELLOW}[DRY RUN] Command:{RESET} {' '.join(cmd[:6])}...")
        print(f"    {DIM}cwd={cwd}  prompt_len={len(fullPrompt):,}  use_stdin={use_stdin}{RESET}")
        return "[DRY RUN - No AI response]", False

    # Text-only calls run from script dir; execution from vault root (picks up CLAUDE.md).
    run_cwd = str(cwd) if agent_mode else str(Path(__file__).parent)

    transport = "stdin" if use_stdin else "arg"
    print(f"    {YELLOW}Ã¢â€ â€™ Sending to AI ({provider}, {transport}, {len(fullPrompt):,} chars)...{RESET}")

    # Phase-specific timeout. Bumped 2026-05-04 Ã¢â‚¬â€ 120s was killing complex
    # planning calls (some valid plans take 150-250s for big tasks like UI
    # scaffolds + WebSocket backends). Cascade to all providers banned. Now:
    # 300s for text phases, 600s for execution.
    call_timeout = 600 if agent_mode else 300

    # Scrub provider API keys from the subprocess env BEFORE invoking the CLI.
    # Architecture rule: API keys live in vault/.env for free /v1/models metadata
    # ONLY. CLI calls (claude, gemini, codex from terminal) must use subscription
    # auth (Claude Max OAuth, ChatGPT, Google free tier). If the API keys leak
    # into the subprocess env, CLIs prefer them over OAuth and bill against the
    # API account, which has $0 balance and returns "Credit balance is too low"
    # as the response (verified bug 2026-05-04).
    clean_env = {k: v for k, v in os.environ.items()
                 if k not in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY",
                              "GEMINI_API_KEY", "GOOGLE_API_KEY")}

    result = run_with_animation(
        cmd, prompt[:50],
        capture_output=True, text=True, encoding="utf-8", cwd=run_cwd,
        input=fullPrompt if use_stdin else None,
        timeout=call_timeout,
        env=clean_env,
    )
    # TimeoutExpired exception Ã¢â€ â€™ treat as rate limit so we fall back to next provider
    if isinstance(result, subprocess.TimeoutExpired):
        print(f"    {YELLOW}[TIMEOUT: {provider}/{model or '?'}] exceeded {call_timeout}s Ã¢â‚¬â€ falling back{RESET}")
        return None, True  # rate_limited=True triggers fallback in runAiRequestWithFallback
    if isinstance(result, Exception):
        print(f"    {RED}[ERROR] AI command failed: {result}{RESET}")
        return None, False
    if result is None:
        print(f"    {RED}[ERROR] AI command returned no result.{RESET}")
        return None, False
    # Non-zero exit handling. Two distinct cases:
    #  (a) bailed without doing work Ã¢â‚¬â€ only banner / quota error / empty output
    #      Ã¢â€ â€™ treat as failure (was the auto_0010/0011 silent-success bug)
    #  (b) did work but exited non-zero Ã¢â‚¬â€ e.g. codex sometimes returns rc=1
    #      with a full response after long sessions. Substantial response
    #      means the work happened; rc is just noise.
    # We distinguish by looking at output SUBSTANCE post-codex-strip.
    if getattr(result, "returncode", 0) != 0:
        rc_output = (result.stdout or "") + (result.stderr or "")
        # Check for rate-limit signals first Ã¢â‚¬â€ quota errors often non-zero
        is_limited, _ = _isRateLimited(rc_output, result.returncode)
        if is_limited:
            print(f"    {ORANGE}[RATE LIMITED via rc={result.returncode}: {provider}/{model or '?'}]{RESET}")
            return None, True
        # Quick substance check: strip codex header to see if there's real
        # response content. If so, accept it despite rc != 0.
        substance = rc_output
        if provider == "codex" and "\ncodex\n" in substance:
            substance = substance.rsplit("\ncodex\n", 1)[1]
            for footer in ("\n--------", "\ntokens used"):
                if footer in substance:
                    substance = substance[: substance.index(footer)]
            substance = substance.strip()
        # If 200+ chars of substance, the agent did real work despite rc != 0
        if len(substance.strip()) >= 200:
            print(f"    {DIM}[note] {provider}/{model or '?'} rc={result.returncode} but {len(substance)} chars of substance Ã¢â‚¬â€ accepting{RESET}")
            # Fall through to normal output handling below
        else:
            print(f"    {RED}[ERROR] {provider}/{model or '?'} exited with rc={result.returncode} and minimal output ({len(substance)} chars); treating as failure{RESET}")
            print(f"    {DIM}snippet: {rc_output[:200]!r}{RESET}")
            return None, False
    output = result.stdout + (result.stderr if result.stderr else "")
    if provider == "codex":
        # Strip codex CLI session header AND footer. Real format:
        #   Reading additional input from stdin...
        #   OpenAI Codex v...      <-- preamble
        #   --------
        #   workdir/model/session info
        #   --------
        #   user
        #   <our prompt>
        #   codex
        #   <ACTUAL RESPONSE WE WANT>
        #   --------                  <-- footer (sometimes)
        #   tokens used: ...          <-- footer (always)
        # We want everything AFTER the LAST '\ncodex\n' marker, then trim
        # trailing '\n--------' OR '\ntokens used' (whichever appears first).
        def _trim_codex_trailers(text: str) -> str:
            for footer in ("\n--------", "\ntokens used"):
                if footer in text:
                    text = text[: text.index(footer)]
            return text.rstrip()

        if "\ncodex\n" in output:
            after = output.rsplit("\ncodex\n", 1)[1]
            output = _trim_codex_trailers(after).strip()
        else:
            # Bug P fix (2026-05-10): when codex doesn't emit the `\ncodex\n`
            # marker (CLI version drift, malformed session, mixed stderr,
            # etc.), the prior defensive stripping was OVER-EAGER:
            # `_trim_codex_trailers` cuts at the FIRST `\n--------`, which
            # in a no-marker response is actually the HEADER separator (after
            # the banner), not a footer. Result: the entire response body
            # got truncated, leaving only the 40-char banner. Surfaced via
            # auto_0068 — codex returned 20369 chars, parser butchered to 40.
            #
            # New strategy: ONLY strip aggressive markers if the resulting
            # output is still substantive (>200 chars). If stripping would
            # leave us with a tiny stub, KEEP the full output and let the
            # downstream BANNER_PATTERNS / substance check decide. Better to
            # ship a noisy long response than to corrupt it down to a banner.
            stripped_attempt = output
            for marker in ("\nReading additional input from stdin",
                           "\nOpenAI Codex v"):
                if marker in stripped_attempt:
                    stripped_attempt = stripped_attempt[: stripped_attempt.index(marker)].rstrip()
            stripped_attempt = _trim_codex_trailers(stripped_attempt)
            if len(stripped_attempt.strip()) >= 200:
                output = stripped_attempt
            else:
                # The aggressive stripping would leave nothing usable.
                # Keep the full output — downstream banner check will
                # correctly reject if it's truly banner-only, but if there's
                # real content past the supposed "footer", we preserve it.
                print(f"    {YELLOW}[parse note] codex no `\\ncodex\\n` marker; "
                      f"aggressive strip would leave {len(stripped_attempt)} "
                      f"chars — keeping full {len(output)}-char output{RESET}")
                output = output.strip()
    is_limited, matched_signal = _isRateLimited(output, result.returncode)
    if is_limited:
        print(f"    {YELLOW}[LIMIT HIT: {provider}]{RESET}  Rate limit or quota reached.")
        print(f"    {DIM}Matched signal: '{matched_signal}'{RESET}")
        print(f"    {DIM}Output snippet: {output[:500]}...{RESET}")
        return None, True

    # Ã¢â€â‚¬Ã¢â€â‚¬ Content-based substance check (rc=0 stub class) Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬
    # Discovered 2026-05-08 via auto_0062: the planner CLI returned rc=0 with
    # an 85-char message: "Waiting for file read permissions Ã¢â‚¬â€ please approve
    # the reads above and I'll proceed." The rc-based substance check above
    # never fired (rc was 0). The output passed through as a "successful"
    # plan, polluting the checkpoint with a stub. New class of false-success.
    # Same shape as auto_0010/auto_0011 / banner-string but with rc=0.
    if output:
        stripped = output.strip()
        BAIL_PATTERNS = (
            "Waiting for file read permissions",
            "please approve the reads",
            "I'll proceed once you approve",
            "I cannot read these files without",
            "please approve the file read requests",
        )
        looks_like_bail = any(p.lower() in stripped.lower() for p in BAIL_PATTERNS)
        if looks_like_bail and len(stripped) < 600:
            print(f"    {RED}[ERROR] {provider}/{model or '?'} returned a "
                  f"permission/bail stub ({len(stripped)} chars); treating as "
                  f"failure so routing falls to next provider{RESET}")
            print(f"    {DIM}snippet: {stripped[:200]!r}{RESET}")
            return None, False

        # Bug N fix (2026-05-09): the CLI sometimes returns ONLY its banner
        # string ("OpenAI Codex v0.128.0 (research preview)") with no
        # actual response. Surfaced via auto_0067 — codex returned 40 chars,
        # the vault accepted it as a valid plan, planning produced
        # nonsense, plan_review reviewed nonsense. Detect: short response
        # matching version-banner patterns. Any output < 200 chars that
        # matches a known CLI banner is treated as failure → fallback.
        BANNER_PATTERNS = (
            r"^OpenAI Codex v\d",
            r"^Claude Code v\d",
            r"^Gemini CLI v\d",
            r"^claude\s+v?\d+\.\d",
            r"\(research preview\)\s*$",
            r"\(beta\)\s*$",
            r"\(preview\)\s*$",
        )
        if len(stripped) < 200:
            for pat in BANNER_PATTERNS:
                if re.search(pat, stripped, re.MULTILINE):
                    print(f"    {RED}[ERROR] {provider}/{model or '?'} returned "
                          f"only a CLI banner ({len(stripped)} chars matching "
                          f"{pat!r}); treating as failure so routing falls to "
                          f"next provider{RESET}")
                    print(f"    {DIM}snippet: {stripped[:200]!r}{RESET}")
                    return None, False

    # Ã¢â€â‚¬Ã¢â€â‚¬ Cost tracking Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬
    in_tok = _estimateTokens(fullPrompt)
    out_tok = _estimateTokens(output) if output else 0
    _recordCallCost(in_tok, out_tok, model or "unknown")

    return output, False


# Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬ Infra protection (non-execution phase rollback) Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬
# Root cause this addresses (2026-05-06): codex/gemini CLIs always run with
# provider-level approval bypass behavior regardless of agent_mode. The hook scope only restricts the parent Claude Code harness,
# not subprocess CLIs. So during a non-execution phase (planning / refinement /
# learning / plan_review / skill_curation / sharpener), an over-eager codex
# could rewrite ai_dougs.py from scratch Ã¢â‚¬â€ and did, on 2026-05-06 00:11 AM,
# wholesale-replacing 140KB of working code with a 12.7KB stub.
#
# Defense: snapshot all infra file contents BEFORE the LLM call, restore any
# that changed AFTER. Phases that legitimately need to modify infra files
# (only execution does) skip this protection. Restorations are logged loudly
# so we know when an agent attempted unauthorized modification.

# Mirrors INFRA_PATTERNS in scripts/vault_graph/self_test.py Ã¢â‚¬â€ kept duplicate
# here to avoid the import cycle (this file is imported BY self_test).
def _is_provider_quota_banned(provider: str) -> bool:
    """True if this provider hit a rate limit recently and the ban hasn't expired."""
    until = _quota_banned_until.get(provider)
    return until is not None and time.time() < until


def _ban_provider_for_quota(provider: str, ttl_seconds: float = None) -> None:
    """Skip this provider for ttl_seconds. Default = _QUOTA_BAN_TTL_SEC (1h)."""
    _quota_banned_until[provider] = time.time() + (ttl_seconds or _QUOTA_BAN_TTL_SEC)


def _clear_quota_bans() -> None:
    """Forget all quota bans Ã¢â‚¬â€ useful for tests or manual reset."""
    _quota_banned_until.clear()


def _explainRoutingEnabled():
    """True only when VAULT_EXPLAIN_ROUTING is exactly the literal '1'.

    Other values ('0', 'true', 'yes', '', etc.) all leave the explain mode disabled.
    """
    return os.environ.get("VAULT_EXPLAIN_ROUTING") == "1"


def _buildRoutingExplanation(
    provider,
    model,
    phase,
    attempt_index,
    ranked_providers,
    routing_source,
    original_model,
    prior_failures,
    was_quota_banned,
    agent_mode,
):
    """Compose a single human-readable paragraph explaining why provider/model was chosen.

    Combines primary-vs-fallback status, position in the ranked chain, provider-selection
    rationale, model-routing source (config / explore / history / default), prior failures
    accumulated in this chain, quota-ban context, agent-mode, and provider-specific
    constraints into one prose paragraph (no bullets, no JSON, no debug dump).
    """
    total = len(ranked_providers) if ranked_providers else 1
    position_word = "primary" if attempt_index == 0 else "fallback"
    if attempt_index == 0:
        ordinal = "first"
    elif attempt_index == 1:
        ordinal = "second"
    elif attempt_index == 2:
        ordinal = "third"
    else:
        ordinal = f"{attempt_index + 1}th"

    chain_names = ", ".join(
        e.get("provider", "?") for e in (ranked_providers or [])
    ) or provider
    phase_clause = f" for the '{phase}' phase" if phase else ""
    mode_clause = (
        "agent execution mode (Claude can edit files)" if agent_mode
        else "text-only mode (no file edits)"
    )

    original_lower = (original_model or "").lower()
    if original_lower in VARIABLE_MODEL_VALUES:
        if routing_source == "explore":
            model_clause = (
                f"the model '{model}' was picked by phase-aware exploration because it still has the "
                f"fewest runs in this phase and needs more evidence before any model can be preferred"
            )
        elif routing_source == "history":
            model_clause = (
                f"the model '{model}' was picked from historical phase-success data as the best performer "
                f"for this phase"
            )
        else:
            model_clause = (
                f"the model '{model or 'provider default'}' was used because no model tiers are configured "
                f"for {provider}, so default routing applied"
            )
    else:
        if original_model:
            model_clause = f"the model '{model}' is pinned by config for {provider}, so no dynamic routing ran"
        else:
            model_clause = f"no specific model was configured, so {provider}'s CLI default applies"

    if attempt_index == 0:
        fallback_clause = (
            f"This is the {ordinal} (primary) attempt in a ranked chain of {total} provider(s) "
            f"[{chain_names}], ordered by exploration-then-history in _rankProviders"
        )
    else:
        fallback_clause = (
            f"This is the {ordinal} attempt Ã¢â‚¬â€ a fallback after earlier providers in the ranked chain "
            f"[{chain_names}] did not succeed"
        )

    if prior_failures:
        prior_clause = (
            " Prior failures in this chain (previous attempts): "
            + "; ".join(prior_failures)
            + "."
        )
    else:
        prior_clause = " No prior failures have occurred in this chain yet."

    quota_clause = (
        f" Note: {provider} was quota-banned earlier this session and is being tried only after fresh "
        f"providers were exhausted."
        if was_quota_banned else ""
    )

    constraint_notes = []
    if provider == "gemini":
        constraint_notes.append(
            "gemini delivers the prompt as a CLI argument, so very long prompts may hit Windows arg "
            "limits and trigger a fallback"
        )
    if provider not in {"claude", "gemini", "codex", "cursor"}:
        constraint_notes.append(
            f"{provider} is not a recognised CLI in runAiRequest and will be skipped"
        )
    constraint_clause = (
        " Notable constraints: " + "; ".join(constraint_notes) + "."
        if constraint_notes else ""
    )

    return (
        f"[ROUTING] {fallback_clause}{phase_clause}; calling {provider}/{model or '<default>'} as the "
        f"{position_word} provider, where {model_clause}. Running in {mode_clause}.{prior_clause}"
        f"{quota_clause}{constraint_clause}"
    )


def runAiRequestWithFallback(prompt, cwd, addDirs, providerOrder, sendAiRequests, agent_mode=True, phase=""):
    """Try providers ranked by historical performance until one succeeds or all are exhausted.

    agent_mode=False for planning/refinement/learning (text only).
    agent_mode=True  for execution (Claude edits files).
    phase: task phase string ("planning", "execution", "refinement", "learning") Ã¢â‚¬â€ used for
           phase-aware model routing so each phase builds its own evidence independently.

    Quota bans: if a provider returns a rate-limit signal, it's skipped for the
    rest of this Python process (1h TTL). This stops the cross-phase waste where
    e.g. gemini quota'd in refinement and the system re-tried it (and ate ~10s
    of timeout) in execution and learning of the same task. The fresh providers
    are tried first; banned providers are tried last as a fallback in case the
    fresh ones also fail.

    Explain mode: when VAULT_EXPLAIN_ROUTING=1 (literal '1' only), prints a
    one-paragraph prose explanation to stdout immediately before each
    provider/model call, describing why that pick was made.
    """
    ranked_all = _rankProviders(providerOrder)
    fresh = [e for e in ranked_all if not _is_provider_quota_banned(e.get("provider", ""))]
    banned = [e for e in ranked_all if _is_provider_quota_banned(e.get("provider", ""))]
    if banned:
        names = ", ".join(e.get("provider", "?") for e in banned)
        print(f"    {DIM}Ã¢â€ â€™ Quota-banned this session (deprioritised): {names}{RESET}")
    ranked = fresh + banned  # fresh first; banned only as last-resort

    # Infra protection: snapshot all guardrail files before NON-execution phases.
    # Execution legitimately modifies code; planning/refinement/learning/
    # plan_review/skill_curation/sharpener/judge phases must not. After the
    # call, _restoreInfraIfChanged compares + reverts any unauthorized changes.
    # See header comment near _snapshotInfraFiles for the bug this prevents.
    
    prior_failures = []
    explain = _explainRoutingEnabled()

    for attempt_index, entry in enumerate(ranked):
        provider = entry.get("provider", "claude")
        original_model = entry.get("model", "")
        model = original_model
        routing_source = "config"
        if model.lower() in VARIABLE_MODEL_VALUES:
            model, routing_source = _routeModel(prompt[:500], provider, sendAiRequests, phase=phase)
        was_quota_banned = entry in banned

        if explain:
            print(_buildRoutingExplanation(
                provider=provider,
                model=model,
                phase=phase,
                attempt_index=attempt_index,
                ranked_providers=ranked,
                routing_source=routing_source,
                original_model=original_model,
                prior_failures=prior_failures,
                was_quota_banned=was_quota_banned,
                agent_mode=agent_mode,
            ), flush=True)

        output, rate_limited = runAiRequest(prompt, cwd, addDirs, provider, sendAiRequests, model, agent_mode=agent_mode)
        # Model-name validity check Ã¢â‚¬â€ distinct from rate-limit. The CLI rejected
        # the model name (404 / ModelNotFoundError / etc). Log so routing
        # permanently deprioritises this stale name; fall through to next provider.
        # After every LLM call (success or fail), if we took a snapshot,
        # immediately verify+restore. This prevents an over-eager subprocess
        # CLI from leaving infra files modified between provider attempts.
        if infra_snapshot is not None:
            _restoreInfraIfChanged(infra_snapshot, phase or "non-execution",
                                    provider=provider, model=model)
        # FIX #3: read-only audit of every FILE-WRITE since phase start.
        # Runs unconditionally (incl. agent_mode/execution) so infra writes
        # during execution still get a yellow flag in the daemon log.
        try:
            _auditPhaseWrites(phase or "non-execution", _phase_start_ts,
                              provider=provider, model=model,
                              agent_mode=agent_mode)
        except Exception:
            pass
        if output is not None and _looksLikeModelNotFound(output):
            print(f"    {RED}[MODEL UNAVAILABLE: {provider}/{model or '?'}] CLI rejected the model name Ã¢â‚¬â€ trying next provider...{RESET}")
            _writeCostLog(
                "model-validity-check", phase or "unknown", "model_unavailable",
                {"model": model, "passes": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0},
                extra={"provider": provider, "snippet": output[:200]},
            )
            prior_failures.append(f"{provider}/{model or '?'} rejected the model name (model unavailable)")
            continue
        if output is not None:
            return output, provider, model
        if not rate_limited:
            prior_failures.append(f"{provider}/{model or '?'} returned no output (non-rate-limit failure)")
            return None, provider, model
        # Rate-limited Ã¢â€ â€™ ban this provider for the rest of the session so
        # subsequent phases of the same task don't re-burn time on it.
        _ban_provider_for_quota(provider)
        print(f"    {ORANGE}[FALLBACK] {provider} unavailable Ã¢â‚¬â€ banned for this session, trying next provider...{RESET}")
        prior_failures.append(f"{provider}/{model or '?'} hit a rate limit / quota and was banned for this session")
    print(f"    {RED}[ALL PROVIDERS EXHAUSTED] Could not complete AI request.{RESET}")
    return None, None, None


def parseFrontmatter(lines):
    """Parse YAML frontmatter from lines if present.
    
    Returns (frontmatter_dict, content_start_index).
    If no frontmatter, returns ({}, 0).
    """
    if not lines or lines[0].strip() != "---":
        return {}, 0
    
    end_idx = -1
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end_idx = i
            break
    
    if end_idx == -1:
        return {}, 0
    
    frontmatter = {}
    for i in range(1, end_idx):
        line = lines[i]
        if ":" in line:
            key, _, value = line.partition(":")
            frontmatter[key.strip()] = value.strip()
    
    return frontmatter, end_idx + 1


TITLE_FROM_FRONTMATTER_PLACEHOLDER = "=this.title"


def resolveTaskTitleFromHeading(heading_text, frontmatter):
    """Use YAML `title` when H1 is Obsidian/Templater placeholder `=this.title` (optional backticks)."""
    inner = heading_text.strip().strip("`").strip()
    if inner == TITLE_FROM_FRONTMATTER_PLACEHOLDER:
        ft = (frontmatter or {}).get("title", "").strip()
        if ft:
            return ft
    return heading_text.strip()


def parseTaskFile(content):
    """Parse a task file and extract task info.
    
    Supports YAML frontmatter at the top of the file (between --- delimiters).
    Falls back to inline status/app_location after the # heading if no frontmatter.
    
    Returns dict with: name, status, app_location, sections (dict of section_name -> content),
                       frontmatter (dict), frontmatter_end (line index after frontmatter)
    """
    lines = content.split("\n")
    task = {
        "name": "",
        "status": "",
        "app_location": "",
        "sections": {},
        "raw_lines": lines,
        "frontmatter": {},
        "frontmatter_end": 0,
    }
    
    frontmatter, content_start = parseFrontmatter(lines)
    task["frontmatter"] = frontmatter
    task["frontmatter_end"] = content_start
    
    if "status" in frontmatter:
        task["status"] = frontmatter["status"]
    if "app_location" in frontmatter:
        task["app_location"] = frontmatter["app_location"]
    
    i = content_start
    while i < len(lines):
        line = lines[i]
        if line.startswith("# ") and not line.startswith("## "):
            raw_heading = line[2:].strip()
            task["name"] = resolveTaskTitleFromHeading(raw_heading, frontmatter)
            i += 1
            while i < len(lines) and not lines[i].startswith("## "):
                meta_line = lines[i].strip()
                if meta_line.startswith("status:") and not task["status"]:
                    task["status"] = meta_line[7:].strip()
                elif meta_line.startswith("app_location:") and not task["app_location"]:
                    task["app_location"] = meta_line[13:].strip()
                i += 1
            break
        i += 1
    
    current_section = None
    section_start = -1
    
    for idx, line in enumerate(lines):
        if line.startswith("## "):
            if current_section is not None:
                task["sections"][current_section] = {
                    "start": section_start,
                    "end": idx,
                    "content": "\n".join(lines[section_start + 1 : idx]).strip(),
                }
            current_section = line[3:].strip()
            section_start = idx
    
    if current_section is not None:
        task["sections"][current_section] = {
            "start": section_start,
            "end": len(lines),
            "content": "\n".join(lines[section_start + 1 :]).strip(),
        }
    
    return task


def extractSection(task, section_name):
    """Get content of a named section from a task."""
    section = task["sections"].get(section_name)
    if section:
        content = section["content"]
        if content.startswith("*[") and content.endswith("]*"):
            return ""
        if content.startswith("[") and content.endswith("]"):
            return ""
        return content
    return ""


_PLAN_REFINEMENT_RE = re.compile(r"^Plan refinement (\d+)$", re.IGNORECASE)


def resolveFilePrompt(text, baseDir):
    """If text starts with 'file: <filename>', read and return that file's contents."""
    if text.lower().startswith("file:"):
        filename = text[5:].strip()
        filePath = Path(baseDir) / filename
        if filePath.exists():
            return filePath.read_text(encoding="utf-8").strip()
        print(f"    {RED}Ã¢Å“â€” File prompt not found: {filePath}{RESET}")
    return text


def updateTaskFile(filePath, task, field_updates=None, section_updates=None, section_placement=None, append_markdown=None):
    """Update a task file with new field values and/or section content.

    field_updates: dict of field_name -> new_value (e.g., {"status": "pending_human_answers"})
    section_updates: dict of section_name -> new_content
    section_placement: when creating a NEW section, optional map section_name -> anchor section
        to insert immediately after (e.g. {"Plan refinement 2": "Plan refinement 1"}).
    append_markdown: optional string appended at EOF after other updates (human-facing headers should be last).

    If YAML frontmatter exists and contains the field, updates it there.
    Otherwise updates the inline field after the # heading.
    """
    content = "\n".join(task["raw_lines"])
    
    if field_updates:
        lines = content.split("\n")
        frontmatter_end = task.get("frontmatter_end", 0)
        frontmatter = task.get("frontmatter", {})
        
        fields_updated_in_frontmatter = set()
        if frontmatter_end > 0:
            for i in range(1, frontmatter_end - 1):
                for field, value in field_updates.items():
                    if lines[i].strip().startswith(f"{field}:"):
                        lines[i] = f"{field}: {value}"
                        fields_updated_in_frontmatter.add(field)
        
        remaining_fields = {k: v for k, v in field_updates.items() if k not in fields_updated_in_frontmatter}
        if remaining_fields:
            i = frontmatter_end
            while i < len(lines):
                line = lines[i]
                if line.startswith("# ") and not line.startswith("## "):
                    i += 1
                    while i < len(lines) and not lines[i].startswith("## "):
                        for field, value in remaining_fields.items():
                            if lines[i].strip().startswith(f"{field}:"):
                                lines[i] = f"{field}: {value}"
                        i += 1
                    break
                i += 1
        content = "\n".join(lines)
    
    if section_updates:
        placement = section_placement or {}
        for section_name, new_content in section_updates.items():
            task = parseTaskFile(content)
            section = task["sections"].get(section_name)
            lines = content.split("\n")
            if section:
                start = section["start"]
                end = section["end"]
                if start < len(lines):
                    header_line = lines[start]
                    new_section_lines = [header_line, new_content, ""]
                    lines = lines[:start] + new_section_lines + lines[end:]
                    content = "\n".join(lines)
            else:
                new_section_lines = [f"## {section_name}", new_content, ""]
                anchor_name = placement.get(section_name)
                anchor = task["sections"].get(anchor_name) if anchor_name else None
                if anchor:
                    end = anchor["end"]
                    lines = lines[:end] + new_section_lines + lines[end:]
                    content = "\n".join(lines)
                else:
                    lines.extend(new_section_lines)
                    content = "\n".join(lines)

    if append_markdown:
        if not content.endswith("\n"):
            content += "\n"
        content += append_markdown
        if not content.endswith("\n"):
            content += "\n"

    if not content.endswith("\n"):
        content += "\n"
    
    tmpPath = filePath.with_suffix(".tmp")
    tmpPath.write_text(content, encoding="utf-8")
    tmpPath.replace(filePath)

    return parseTaskFile(content)

