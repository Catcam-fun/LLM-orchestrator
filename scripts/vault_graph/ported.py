"""Ported helpers from ai_dougs.py — used by graph nodes during the migration.

Strategy: import directly from ai_dougs (the existing source of truth) rather
than copy-paste. This guarantees identical behavior and avoids drift during
the migration. After Day 4.5 verification confirms the new system matches the
old, we extract what we need into vault_graph/ proper and delete ai_dougs.py.

What's exposed here:
- All cost-tracking helpers (_writeCostLog, _recordCallCost, etc.)
- All routing helpers (_routeModel, runAiRequestWithFallback)
- All quality helpers (_lintTaskFile, _looksLikeRefusal, etc.)
- All context helpers (buildTaskPreamble, _selectContextFilesForTask, scout*)
- All persistence helpers (_archivePromptResponse, _appendFailureMode)
- All review helpers (_shouldRunPlanReview, _runPlanReview)
- All prompts (PLANNING_PROMPT, PLAN_REVIEW_PROMPT, etc.)
- Constants (MODEL_TIERS, ANSI colors, etc.)

Plus graph-friendly wrappers where the ai_dougs API doesn't fit cleanly:
- adapt_state_for_routing — convert TaskState to the dict format old helpers want
"""
import sys
from pathlib import Path

# Add ai_dougs to path so we can import it as a module
_AI_DOUGS_DIR = Path(__file__).resolve().parent.parent / "ai_dougs"
if str(_AI_DOUGS_DIR) not in sys.path:
    sys.path.insert(0, str(_AI_DOUGS_DIR))

import ai_dougs as _d

# ─── Re-export everything we need (no shadow imports — keep one source of truth) ─

# Routing
_routeModel               = _d._routeModel
_loadPerformanceHistory   = _d._loadPerformanceHistory
_loadRoutingParams        = _d._loadRoutingParams
_escalateProviderOrder    = _d._escalateProviderOrder
_rankProviders            = _d._rankProviders
runAiRequest              = _d.runAiRequest
runAiRequestWithFallback  = _d.runAiRequestWithFallback
_is_provider_quota_banned = _d._is_provider_quota_banned
_ban_provider_for_quota   = _d._ban_provider_for_quota
_clear_quota_bans         = _d._clear_quota_bans

# Quality detection
_isRateLimited            = _d._isRateLimited
_looksLikeModelNotFound   = _d._looksLikeModelNotFound

# Cost + time
_writeCostLog             = _d._writeCostLog
_recordCallCost           = _d._recordCallCost
_resetCostAccumulator     = _d._resetCostAccumulator
_startPhaseTimer          = _d._startPhaseTimer
_phaseDurationSeconds     = _d._phaseDurationSeconds
_estimateTokens           = _d._estimateTokens
_callCostUsd              = _d._callCostUsd

# Locks
_fileLock                 = _d._fileLock

# Pre-flight + budget
_lintTaskFile             = _d._lintTaskFile
_taskCostCeiling          = _d._taskCostCeiling
_phaseCostGroup           = _d._phaseCostGroup
_phaseCostCeilingField    = _d._phaseCostCeilingField
_phaseCostObservedField   = _d._phaseCostObservedField

# Persistence
_archivePromptResponse    = _d._archivePromptResponse
_appendFailureMode        = _d._appendFailureMode
_loadRelevantFailures     = _d._loadRelevantFailures
_loadLatestSessionDigest  = _d._loadLatestSessionDigest

# Plan review
_shouldRunPlanReview      = _d._shouldRunPlanReview
_runPlanReview            = _d._runPlanReview

# Context + scouting
buildTaskPreamble         = _d.buildTaskPreamble
loadAiMain                = _d.loadAiMain
_selectContextFilesForTask = _d._selectContextFilesForTask
scoutProject              = _d.scoutProject
scoutProjectFull          = _d.scoutProjectFull
resolveFilePrompt         = _d.resolveFilePrompt

# Hook scope
_writeHookScope           = _d._writeHookScope
_clearHookScope           = _d._clearHookScope

# Execution-specific
_setupGitBranch           = _d._setupGitBranch
_captureDiff              = _d._captureDiff
_autoCommit               = _d._autoCommit
_runBuildGate             = _d._runBuildGate
_runVisualValidation      = _d._runVisualValidation
_filterLintWarnings       = _d._filterLintWarnings
_leanAgentCall            = _d._leanAgentCall
_buildScopeBlock          = _d._buildScopeBlock

# Learning-phase outputs
parseLearningOutput       = _d.parseLearningOutput
# writeSkillFile removed 2026-05-12 (auto-curator off; skills are co-authored)
updateModelPerformance    = _d.updateModelPerformance
_updateRoutingParams      = _d._updateRoutingParams
_parseParamSuggestions    = _d._parseParamSuggestions
_logContextInjection      = _d._logContextInjection

# Prompts
PLANNING_PROMPT           = _d.PLANNING_PROMPT
REFINEMENT_PROMPT         = _d.REFINEMENT_PROMPT
EXECUTION_PROMPT          = _d.EXECUTION_PROMPT
LEARNING_PROMPT           = _d.LEARNING_PROMPT
PLAN_REVIEW_PROMPT        = _d.PLAN_REVIEW_PROMPT

# Model registry
MODEL_TIERS               = _d.MODEL_TIERS
_loadModelPool            = _d._loadModelPool

# Config
loadConfig                = _d.loadConfig

# ANSI colors (so node print statements match the existing style)
RESET, BOLD, DIM = _d.RESET, _d.BOLD, _d.DIM
GREEN, YELLOW, CYAN, RED, MAGENTA = _d.GREEN, _d.YELLOW, _d.CYAN, _d.RED, _d.MAGENTA
ORANGE = _d.ORANGE


# ─── Graph-friendly wrappers ─────────────────────────────────────────────────

def vault_root() -> Path:
    """Resolve vault root from this file's location."""
    return Path(__file__).resolve().parent.parent.parent


def cost_accumulator_snapshot() -> dict:
    """Snapshot the global accumulator into a dict suitable for TaskState.

    The old code uses a module-level _task_cost_accumulator dict. Graph state
    needs a serialisable copy. We snapshot after each LLM call.
    """
    return dict(_d._task_cost_accumulator)


def reset_for_phase(existing_cost_usd: float = 0.0) -> None:
    """Reset the cost accumulator + start phase timer.

    Called at the top of each LLM-using phase node. The optional argument is
    retained for old callers, but phase budget state is tracked separately.
    """
    _resetCostAccumulator()
    _startPhaseTimer()


def _state_cost_value(state, field: str) -> float:
    try:
        return float(state.get(field, 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def phase_budget_status(state, phase: str) -> dict:
    """Return phase ceiling/spend metadata for a TaskState."""
    frontmatter = task_dict_from_state(state).get("frontmatter", {})
    phase_group = _phaseCostGroup(phase)
    observed_field = _phaseCostObservedField(phase)
    ceiling_field = _phaseCostCeilingField(phase)
    observed = _state_cost_value(state, observed_field)
    ceiling = _taskCostCeiling(frontmatter, phase)
    return {
        "phase": phase_group,
        "observed_field": observed_field,
        "ceiling_field": ceiling_field,
        "observed": observed,
        "ceiling": ceiling,
        "exceeded": observed >= ceiling,
    }


truncate_observed = _d.truncate_observed  # re-exported from ai_dougs (single source of truth)


def _record_cost_ceiling_observation(phase_group: str, observed: float, ceiling: float, ceiling_field: str) -> None:
    """Append a cost-ceiling-exceeded observation to logs/events.jsonl.

    Cost ceilings are observation-only (per the quality hierarchy in
    VISION.md); this writer surfaces the crossing event so the diagnostics
    CLI can show it. Quality work is never halted on cost.
    """
    try:
        from datetime import datetime as _dt
        from pathlib import Path as _Path
        log_path = vault_root() / "logs" / "events.jsonl"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "timestamp": _dt.now().isoformat(timespec="seconds"),
            "event": "cost_ceiling_observed",
            "phase": phase_group,
            "observed": round(observed, 4),
            "ceiling": round(ceiling, 4),
            "ceiling_field": ceiling_field,
            "details": (
                f"observation only — task continues. "
                f"phase={phase_group} cost=${observed:.4f} >= ceiling=${ceiling:.4f} "
                f"({ceiling_field}). See VISION.md."
            ),
        }
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass  # best-effort; never break a phase because logging failed


def budget_exceeded_update(state, phase: str) -> dict | None:
    """Observation-only check before a phase starts.

    Returns None always — cost ceilings no longer halt tasks. If the phase
    bucket has already crossed its ceiling, log it to events.jsonl for the
    diagnostics CLI to surface. Quality work is never halted on cost.

    PRIOR DESIGN (removed 2026-05-08): returned a halt update setting
    status='budget_exceeded' + next_action='halt'. See
    `_record_cost_ceiling_observation` docstring for the full rationale.
    """
    budget = phase_budget_status(state, phase)
    if not budget["exceeded"]:
        return None
    print(
        f"      {YELLOW}cost ceiling observed (continuing):{RESET} {budget['phase']} "
        f"${budget['observed']:.4f} >= ${budget['ceiling']:.4f} "
        f"({budget['ceiling_field']}) — quality work not halted; observation logged"
    )
    _record_cost_ceiling_observation(
        budget["phase"], budget["observed"], budget["ceiling"], budget["ceiling_field"]
    )
    return None


def budget_exceeded_after_phase(phase: str, observed_cost_usd: float, ceiling: float | None = None) -> dict:
    """Observation-only check after a phase completes.

    Returns {} always — cost ceilings no longer halt tasks. See
    `budget_exceeded_update` and `_record_cost_ceiling_observation` for the
    full rationale (2026-05-08 quality-hierarchy directive).
    """
    phase_group = _phaseCostGroup(phase)
    ceiling_field = _phaseCostCeilingField(phase)
    observed_field = _phaseCostObservedField(phase)
    ceiling_value = ceiling if ceiling is not None else 0.0
    if not ceiling_value or observed_cost_usd < ceiling_value:
        return {}
    print(
        f"      {YELLOW}cost ceiling observed (continuing):{RESET} {phase_group} "
        f"${observed_cost_usd:.4f} >= ${ceiling_value:.4f} ({ceiling_field}) — "
        f"quality work not halted; observation logged"
    )
    _record_cost_ceiling_observation(phase_group, observed_cost_usd, ceiling_value, ceiling_field)
    return {}


def phase_budget_exceeded_update(state, phase: str, observed: float) -> dict | None:
    """Observation-only post-phase check.

    Returns None always — cost ceilings no longer halt tasks. See
    `budget_exceeded_update` and `_record_cost_ceiling_observation` for the
    full rationale (2026-05-08 quality-hierarchy directive).
    """
    budget = phase_budget_status(state, phase)
    ceiling = budget["ceiling"]
    if not ceiling or observed < ceiling:
        return None
    print(
        f"      {YELLOW}cost ceiling observed (continuing):{RESET} {budget['phase']} "
        f"${observed:.4f} >= ${ceiling:.4f} ({budget['ceiling_field']}) — "
        f"quality work not halted; observation logged"
    )
    _record_cost_ceiling_observation(
        budget["phase"], observed, ceiling, budget["ceiling_field"]
    )
    return None


def set_current_app_location(app_location: str | None) -> None:
    """Set the module-level global that ai_dougs._selectContextFilesForTask reads.

    The OLD processTask flow set this before each LLM call so per-project
    .agent-context.md files would be picked up. Our new graph nodes need to
    do the same — call this at the top of any node before invoking
    runAiRequestWithFallback.

    Without this, code/<app>/.agent-context.md is INVISIBLE to the planner.
    Bug discovered + fixed 2026-05-04 during post-migration audit.
    """
    _d._CURRENT_APP_LOCATION = app_location or None


def task_dict_from_state(state) -> dict:
    """Convert TaskState into the legacy task dict format some helpers want.

    Old helpers (_lintTaskFile, _shouldRunPlanReview, etc.) expect a dict like
    {"frontmatter": {...}, "sections": {...}}. Adapt TaskState to that shape.
    """
    fm = {}
    if "app_location" in state and state["app_location"]:
        fm["app_location"] = state["app_location"]
    if "files_allowed" in state and state["files_allowed"]:
        fm["files_allowed"] = state["files_allowed"]
    if "files_blocked" in state and state["files_blocked"]:
        fm["files_blocked"] = state["files_blocked"]
    if "build_command" in state and state["build_command"]:
        fm["build_command"] = state["build_command"]
    if "max_cost_usd" in state and state["max_cost_usd"]:
        fm["max_cost_usd"] = str(state["max_cost_usd"])
    if state.get("cost_ceiling"):
        fm["cost_ceiling"] = str(state["cost_ceiling"])
    for field in ("planning_cost_usd", "execution_cost_usd", "learning_cost_usd"):
        if field in state and state[field]:
            fm[field] = str(state[field])
    if "research_allowed" in state:
        fm["research_allowed"] = "true" if state.get("research_allowed") else "false"
    if "cost_estimate_usd" not in fm and state.get("existing_cost_usd"):
        fm["cost_estimate_usd"] = str(state["existing_cost_usd"])

    sections = {}
    if state.get("initial_prompt"):
        sections["Initial Prompt"] = {"content": state["initial_prompt"]}

    return {"frontmatter": fm, "sections": sections, "raw_lines": [], "name": state.get("task_name", "")}
