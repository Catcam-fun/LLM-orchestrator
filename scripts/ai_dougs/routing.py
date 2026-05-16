"""Model and provider routing helpers for ai_dougs."""

import json
from pathlib import Path

SCRIPT_VERSION = "1.1"  # 2026-05-10 - add file lifecycle retention defaults

RESET = "\033[0m"
DIM = "\033[2m"
YELLOW = "\033[93m"

MODEL_TIERS = {}

# Default routing/pruning parameters Ã¢â‚¬â€ overridden by logs/routing_params.json
_ROUTING_PARAMS_DEFAULTS = {
    # Ã¢â€â‚¬Ã¢â€â‚¬ Model / provider routing Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬
    "min_history_runs": 3,            # runs before history-based routing activates
    "success_threshold": 0.80,        # success rate to stay on current tier
    "escalate_threshold": 0.60,       # success rate below which tier escalates
    "cost_drift_std_devs": 2.0,       # std devs above baseline before cost drift flagged
    "provider_min_success_rate": 0.0, # providers below this rate are skipped entirely (0 = never skip)
    "backup_retention_count": 5,      # total .backups/*.tar.gz files to retain
    "cost_log_max_mb": 5,             # rotate logs/cost_log.jsonl above this size
    "log_archive_days": 7,            # move dated daemon/sharpener logs after this age
    "log_delete_days": 30,            # delete dated daemon/sharpener logs after this age
    # Ã¢â€â‚¬Ã¢â€â‚¬ Operational timeouts (seconds/minutes) Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬
    "build_timeout_sec": 300,         # subprocess timeout for build gate
    "resolver_timeout_sec": 60,       # subprocess timeout for model resolver
    "lock_timeout_execution_min": 60, # stale-lock threshold for execution phase
    "lock_timeout_other_min": 15,     # stale-lock threshold for all other phases
    "poll_active_sec": 3,             # poll interval when tasks were processed last cycle
    "poll_idle_sec": 180,             # poll interval when no tasks were found
    "max_autofix_passes": 1,          # how many auto-fix attempts after a failed build
    # Ã¢â€â‚¬Ã¢â€â‚¬ Visual validation Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬
    "vision_model": "claude-haiku-4-5-20251001",  # model used for screenshot review
    "vision_max_tokens": 300,         # max tokens for vision model response
    # Ã¢â€â‚¬Ã¢â€â‚¬ Cost ceiling default Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬
    # Default cap for each task phase bucket. Override per task with
    # planning_cost_ceiling, execution_cost_ceiling, or learning_cost_ceiling.
    # Legacy max_cost_usd is still accepted as a fallback for existing tasks.
    "max_cost_per_task_usd": 2.00,
    # Ã¢â€â‚¬Ã¢â€â‚¬ Plan review by 2nd model Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬
    # After planning completes, an independent model scores the plan for
    # scope/completeness/specificity. Per the quality hierarchy directive
    # (2026-05-08), plan review runs on every plan Ã¢â‚¬â€ no skip on success rate.
    # The right way to make it cheaper is a cheaper reviewer model, never
    # to skip the check. See VISION.md and failure_modes
    # vision/quality_subordinate_to_tokens.
    "plan_review_enabled": True,
    # NOTE: `plan_review_skip_above_success_rate` and
    # `plan_review_min_planning_runs` were REMOVED 2026-05-08. The values
    # were 0.95 and 5; both are now ignored by `_shouldRunPlanReview`. Listed
    # here only so old log/config readers know what changed.
    # Ã¢â€â‚¬Ã¢â€â‚¬ Provider re-exploration (escape exploitation lock-in) Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬
    # After the exploration floor is cleared, _rankProviders defaults to
    # data-driven exploitation forever. That locks in whatever provider
    # happened to be best EARLY and never re-checks. Periodic re-exploration:
    # every Nth call, force the next call to a non-top provider that hasn't
    # been used in the last `re_exploration_window` calls Ã¢â‚¬â€ gives lower-ranked
    # providers a chance to demonstrate improvement (e.g. claude regaining
    # parity after a model upgrade).
    "re_exploration_enabled": True,
    "re_exploration_interval": 10,    # every N calls, force re-exploration
    "re_exploration_window": 5,       # "recent" = last K calls
}
_routing_params_cache = {}
_routing_params_mtime = 0.0


def _loadRoutingParams():
    """Load adaptive routing/pruning parameters from logs/routing_params.json.

    Falls back to defaults if file is missing Ã¢â‚¬â€ zero behavior change on fresh installs.
    Cached by mtime so repeated calls within a task cycle are free.
    The learning agent can update this file via ##PARAMS_START## suggestions.
    """
    global _routing_params_cache, _routing_params_mtime

    logs_dir = Path(__file__).parent.parent.parent / "logs"
    params_file = logs_dir / "routing_params.json"

    if not params_file.exists():
        return dict(_ROUTING_PARAMS_DEFAULTS)

    try:
        current_mtime = params_file.stat().st_mtime
        if _routing_params_cache and current_mtime == _routing_params_mtime:
            return _routing_params_cache

        loaded = json.loads(params_file.read_text(encoding="utf-8"))
        # Merge with defaults so new keys added in future always have fallbacks
        merged = {**_ROUTING_PARAMS_DEFAULTS, **loaded}
        _routing_params_cache = merged
        _routing_params_mtime = current_mtime
        return merged
    except Exception:
        return dict(_ROUTING_PARAMS_DEFAULTS)



def _routeModel(taskText, provider, sendAiRequests, phase=""):
    """Route to a model tier based on phase-specific historical performance.

    Philosophy: no model is assumed better than another. All models in the provider's
    pool are explored fairly before any is preferred. The model with the best proven
    success rate for THIS phase wins Ã¢â‚¬â€ not the cheapest or most expensive.

    Exploration: if any model lacks enough phase-specific data, route to it (fewest runs
    first) to fill the evidence gap. Only switch to exploitation once all models have data.

    All thresholds come from logs/routing_params.json Ã¢â‚¬â€ the learning agent updates them
    over time based on actual outcomes. No hard rules live in this function.

    Returns: (model_str, routing_source) where routing_source is "history", "explore", or "default".
    """
    last_routing_source_setter = globals().get("_last_routing_source_setter")

    tiers = MODEL_TIERS.get(provider)
    if not tiers:
        if last_routing_source_setter:
            last_routing_source_setter("default")
        return "", "default"

    history_loader = globals().get("_loadPerformanceHistory")
    if history_loader is None:
        try:
            from .cost_tracking import _loadPerformanceHistory as history_loader
        except ImportError:
            from cost_tracking import _loadPerformanceHistory as history_loader
    history = history_loader(phase=phase)
    params = _loadRoutingParams()

    min_runs       = params["min_history_runs"]
    success_thresh = params["success_threshold"]
    escalate_thresh = params["escalate_threshold"]
    drift_std_devs  = params["cost_drift_std_devs"]
    phase_label    = phase or "task"

    # Ã¢â€â‚¬Ã¢â€â‚¬ EXPLORATION: route to data-starved models first Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬
    # Any model with fewer than min_runs for this phase hasn't been fairly evaluated.
    # Pick the one with the fewest runs to fill the evidence gap (round-robin across all).
    run_counts = {tier: history.get((provider, tier), {}).get("total", 0) for tier in tiers}
    data_starved = [t for t in tiers if run_counts[t] < min_runs]

    if data_starved:
        chosen = min(data_starved, key=lambda t: run_counts[t])
        runs = run_counts[chosen]
        if sendAiRequests:
            print(f"    {DIM}Ã¢â€ â€™ Model routed: {chosen} "
                  f"(exploring Ã¢â‚¬â€ {runs}/{min_runs} runs for {phase_label}){RESET}")
        if last_routing_source_setter:
            last_routing_source_setter("explore")
        return chosen, "explore"

    # Ã¢â€â‚¬Ã¢â€â‚¬ EXPLOITATION: all models have data Ã¢â‚¬â€ pick the best performer Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬
    def _score(tier):
        stats = history[(provider, tier)]
        rate = stats["success"] / stats["total"] if stats["total"] > 0 else 0.0
        # Penalise cost-drifting models Ã¢â‚¬â€ treat drift as a quality signal
        drifting = False
        if stats.get("cost_std_dev", 0) > 0 and stats.get("cost_baseline", 0) > 0:
            drift = (stats["cost_mean"] - stats["cost_baseline"]) / stats["cost_std_dev"]
            if drift > drift_std_devs:
                drifting = True
                if sendAiRequests:
                    print(f"    {YELLOW}Ã¢Å¡Â  Cost drift on {tier}: avg ${stats['cost_mean']:.4f} "
                          f"vs baseline ${stats['cost_baseline']:.4f} "
                          f"({drift:.1f} std devs){RESET}")
        # Score: success rate, penalised if drifting
        return (rate * (0.85 if drifting else 1.0))

    best_tier = max(tiers, key=_score)
    best_stats = history[(provider, best_tier)]
    best_rate = best_stats["success"] / best_stats["total"]

    if sendAiRequests:
        all_rates = {t: f"{int(history[(provider,t)]['success']/history[(provider,t)]['total']*100)}%" for t in tiers}
        print(f"    {DIM}Ã¢â€ â€™ Model routed: {best_tier} "
              f"({int(best_rate*100)}% for {phase_label} Ã¢â‚¬â€ rates: {all_rates}){RESET}")

    if best_rate < escalate_thresh and sendAiRequests:
        print(f"    {YELLOW}Ã¢Å¡Â  Best available model ({best_tier}) is underperforming "
              f"for {phase_label} ({int(best_rate*100)}%) Ã¢â‚¬â€ consider adding more providers{RESET}")

    if last_routing_source_setter:
        last_routing_source_setter("history")
    return best_tier, "history"



# Per-provider exploration safety floor for the EXECUTOR phases. This is the
# MINIMUM number of successful calls before the variance-based gate is even
# considered Ã¢â‚¬â€ protects against acting on tiny samples (e.g. 1/1 = 100%
# success looks great but the CI is enormous). Above this floor the
# variance-based gate (`_should_explore_provider`) takes over and decides
# whether enough evidence has accumulated to stop exploring.
#
# 2026-05-09 threshold-audit conversion: replaced the previous "5 successes
# = stop exploring" rule with a Wilson-score-CI-lower-bound check (true
# adaptive criterion). The floor is now a safety net, not the primary
# decision boundary. See VISION.md "No hardcoded thresholds" Ã¢â‚¬â€ the right
# question is "do we have enough evidence?" not "did we count to 5?"
_PROVIDER_EXPLORATION_FLOOR = 3  # was 5; lowered because Wilson does the heavy lifting now


def _wilson_lower_bound(success: int, total: int, z: float = 1.96) -> float:
    """Wilson score interval lower bound for a binomial success rate.

    Returns a conservative lower estimate of the true success rate given
    `success` out of `total` observations. As `total` grows the lower
    bound approaches the observed rate; for tiny samples the lower bound
    is far below the point estimate (= we shouldn't trust the point
    estimate yet).

    z=1.96 corresponds to a 95% confidence interval. Why Wilson over
    naive (mean - 1.96*stderr): Wilson handles the edge cases (0 or
    100% observed rate) correctly, where naive degenerates to 0 width.

    Per the 2026-05-09 threshold audit, this replaces the arbitrary
    `_PROVIDER_EXPLORATION_FLOOR = 5` check with a true adaptive criterion.
    """
    if total <= 0:
        return 0.0
    p_hat = success / total
    z2 = z * z
    denom = 1 + z2 / total
    numer = p_hat + z2 / (2 * total) - z * (
        ((p_hat * (1 - p_hat) + z2 / (4 * total)) / total) ** 0.5
    )
    return max(0.0, numer / denom)


def _should_explore_provider(success: int, total: int,
                             confidence_width: float = 0.15) -> bool:
    """Decide whether a provider needs more exploration.

    Returns True if either:
      (a) `total` is below the safety floor `_PROVIDER_EXPLORATION_FLOOR`
          (need at least N samples before any statistical claim is meaningful)
      (b) The Wilson 95% CI is wider than `confidence_width` (i.e., the
          observed success rate could be off by more than Ã‚Â±confidence_width
          and we shouldn't trust it yet).

    `confidence_width` of 0.15 Ã¢â€°Ë† "the true rate could be Ã‚Â±15% off the
    observed rate; we don't know enough to commit." Tightens the gate as
    samples accumulate. Tunable via routing_params.json.
    """
    if total < _PROVIDER_EXPLORATION_FLOOR:
        return True
    p_hat = success / total
    lower = _wilson_lower_bound(success, total)
    # Explore while the gap between observed rate and CI lower bound is large
    return (p_hat - lower) > confidence_width


def _recently_used_providers(window: int) -> list[str]:
    """Read the last `window` provider entries from cost_log.jsonl (across
    all phases / tasks). Returns the providers in chronological order, oldest
    first. Used by _rankProviders to decide whether to force re-exploration.
    """
    log = Path(__file__).resolve().parent.parent.parent / "logs" / "cost_log.jsonl"
    if not log.exists() or window <= 0:
        return []
    try:
        # Cheap tail without loading the whole file: read last ~64KB
        sz = log.stat().st_size
        with open(log, "rb") as f:
            f.seek(max(0, sz - 64 * 1024))
            tail = f.read().decode("utf-8", errors="replace")
        lines = [l for l in tail.splitlines() if l.strip()]
        recents = []
        for line in lines[-(window * 4):]:  # over-read; filter below
            try:
                rec = json.loads(line)
            except Exception:
                continue
            # Only count actual LLM calls (skip probe / verification / sharpener
            # internal entries that don't represent a real provider choice)
            phase = rec.get("phase", "")
            if phase in ("verification", "probe", "model-validity-check"):
                continue
            p = rec.get("provider", "")
            if p and p != "unknown":
                recents.append(p)
        return recents[-window:]
    except Exception:
        return []


def _rankProviders(providerOrder):
    """Sort available providers Ã¢â‚¬â€ exploration first, then exploitation.

    Two-stage logic so providers get FAIR EVIDENCE before any is preferred.
    Exploration gate is variance-based, NOT a hardcoded sample count:

    1. EXPLORATION: any provider whose Wilson-CI-lower-bound is wide
       (i.e., we lack confidence in its observed success rate) is sorted
       FIRST. The most-uncertain wins ties. See `_should_explore_provider`.
       This replaces the old `success < 5` check (2026-05-09 threshold
       audit).
    2. EXPLOITATION: once a provider's CI has narrowed below
       `confidence_width`, rank by success rate descending, then by avg
       cost ascending as tiebreaker.

    Excludes providers below min_success_rate threshold once they have enough
    history. No pre-baked opinions about which provider is "better" Ã¢â‚¬â€ data
    decides, and the variance gate decides when there's enough data.
    """
    history_loader = globals().get("_loadPerformanceHistory")
    if history_loader is None:
        try:
            from .cost_tracking import _loadPerformanceHistory as history_loader
        except ImportError:
            from cost_tracking import _loadPerformanceHistory as history_loader
    history = history_loader()

    params = _loadRoutingParams()
    min_rate = params["provider_min_success_rate"]
    min_runs = params["min_history_runs"]
    confidence_width = float(params.get("provider_exploration_confidence_width", 0.15))

    def _provider_success_count(provider: str) -> int:
        tiers = MODEL_TIERS.get(provider, ())
        return sum(history.get((provider, t), {}).get("success", 0) for t in tiers)

    def _score(entry):
        provider = entry.get("provider", "")
        tiers = MODEL_TIERS.get(provider, ())
        total   = sum(history.get((provider, t), {}).get("total",   0)   for t in tiers)
        success = sum(history.get((provider, t), {}).get("success", 0)   for t in tiers)
        cost    = sum(history.get((provider, t), {}).get("avg_cost_usd", 0.0) for t in tiers)

        # Stage 1 Ã¢â‚¬â€ variance-based exploration gate. Sort uncertainty-first:
        # the WIDER a provider's confidence interval, the higher it ranks
        # (= the more we want another sample to tighten the estimate).
        if _should_explore_provider(success, total, confidence_width):
            ci_width = (success / total - _wilson_lower_bound(success, total)) if total > 0 else 1.0
            # Tier 2 = exploration; second key = NEGATIVE width so widest sorts highest;
            # third key = NEGATIVE total so fewer-samples wins ties (tiny samples explored first)
            return (2, -ci_width, -total)

        if total == 0:
            return (0, 0.0, 0.0)  # shouldn't happen given the check above, defensive

        rate = success / total

        # Stage exclusion: providers with enough history but failing
        if total >= min_runs and min_rate > 0 and rate < min_rate:
            return (-1, rate, 0.0)

        # Stage 2 Ã¢â‚¬â€ exploitation: data-driven ranking
        return (1, rate, -cost)

    ranked = sorted(providerOrder, key=_score, reverse=True)
    # Strip out providers that ranked below zero (underperforming, excluded by threshold)
    excluded = [e.get("provider") for e in ranked if _score(e)[0] < 0]
    if excluded:
        print(f"    {YELLOW}Ã¢Å¡Â  Providers excluded (below min success rate): {excluded}{RESET}")
    ranked = [e for e in ranked if _score(e)[0] >= 0]

    # Periodic re-exploration: every Nth call, if all providers have cleared
    # the floor (so we're in pure exploitation), force a non-recent provider
    # to the front. Counts come from cost_log via _recently_used_providers.
    if len(ranked) > 1 and params.get("re_exploration_enabled", True):
        re_interval = max(1, int(params.get("re_exploration_interval", 10)))
        re_window = max(1, int(params.get("re_exploration_window", 5)))
        recent = _recently_used_providers(re_window + re_interval)
        # Trigger only when (a) we have enough call history to count cycles,
        # (b) all providers are post-exploration (no entry has score[0] == 2),
        # (c) the call count modulo interval == 0
        all_post_floor = all(_score(e)[0] != 2 for e in ranked)
        if all_post_floor and len(recent) >= re_interval and len(recent) % re_interval == 0:
            # Pick a provider NOT in the most-recent window
            recent_set = set(recent[-re_window:])
            non_recent = [e for e in ranked if e.get("provider") not in recent_set]
            if non_recent:
                # Promote the first non-recent provider to the front
                forced = non_recent[0]
                others = [e for e in ranked if e is not forced]
                ranked = [forced] + others
                print(f"    {YELLOW}Ã¢â€ â€™ Re-exploration triggered: forcing "
                      f"{forced.get('provider')!r} (not in last "
                      f"{re_window} calls){RESET}")
                return ranked

    if len(ranked) > 1:
        top = ranked[0].get("provider", "?")
        score = _score(ranked[0])
        if score[0] == 1:
            print(f"    {DIM}Ã¢â€ â€™ Provider order (data-driven): {[e.get('provider') for e in ranked]}{RESET}")
        else:
            print(f"    {DIM}Ã¢â€ â€™ Provider order (no history yet, using config order){RESET}")

    return ranked

