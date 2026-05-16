"""Cost tracking helpers for ai_dougs."""

import contextlib
import json
import os
import statistics
import time
from datetime import datetime
from pathlib import Path

SCRIPT_VERSION = "1.1"  # 2026-05-10 - delegate cost-log rotation to shared lifecycle helper

YELLOW = "\033[93m"
RESET = "\033[0m"


def _estimateTokens(text: str) -> int:
    """Approximate tokens using 1 token ~= 4 characters.
    Consistent enough for relative comparison and cost estimation."""
    return max(1, len(text) // 4)


_MODEL_COSTS = {
    # (input_per_1M, output_per_1M) - update as pricing changes
    # Claude
    "haiku": (0.80, 4.00),
    "sonnet": (3.00, 15.00),
    "opus": (15.00, 75.00),
    # Gemini - longer keys first to avoid prefix collisions
    "gemini-2.5-pro-exp": (0.00, 0.00),  # experimental/free preview
    "gemini-2.5-pro": (1.25, 10.00),
    "gemini-2.0-flash": (0.075, 0.30),
    # Codex - longer keys first to avoid prefix collisions
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1": (2.00, 8.00),
    "o3": (10.00, 40.00),
    # Cursor - seat-based pricing, no per-token cost; tracked for success rate only
    "composer-2-fast": (0.0, 0.0),
    "composer-2": (0.0, 0.0),
    "claude-4.6-opus-high-thinking": (0.0, 0.0),
}


def _callCostUsd(input_tokens: int, output_tokens: int, model: str) -> float:
    """Estimate cost of one AI call given token counts and model name.

    Sorts by key length descending so longer names (e.g. 'composer-2-fast')
    match before shorter prefixes (e.g. 'composer-2').
    """
    key = model.lower() if model else ""
    for name, (inp_rate, out_rate) in sorted(_MODEL_COSTS.items(), key=lambda x: len(x[0]), reverse=True):
        if name in key:
            return (input_tokens * inp_rate + output_tokens * out_rate) / 1_000_000
    return 0.0


_task_cost_accumulator = {
    "passes": 0,
    "input_tokens": 0,
    "output_tokens": 0,
    "cost_usd": 0.0,
    "model": "",
}
_performance_history_cache = {}  # {phase_key: {(provider, tier): stats}} - keyed by phase string (or None for all)
_performance_history_mtime = 0.0  # mtime of cost_log.jsonl - cache is invalidated on change
_phase_start_time = 0.0  # monotonic seconds when current phase began - for duration tracking in cost_log

_model_tiers = {}
_routing_params_loader = None


def _configureCostTracking(model_tiers=None, routing_params_loader=None):
    """Wire host-owned routing metadata into the extracted cost module."""
    global _model_tiers, _routing_params_loader
    if model_tiers is not None:
        _model_tiers = model_tiers
    if routing_params_loader is not None:
        _routing_params_loader = routing_params_loader


def _resetCostAccumulator():
    """Zero out the cost accumulator at the start of each task."""
    _task_cost_accumulator.clear()
    _task_cost_accumulator.update(
        {
            "passes": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cost_usd": 0.0,
            "model": "",
        }
    )


PHASE_COST_GROUPS = {
    "task_entry": "planning",
    "planning": "planning",
    "plan_review": "planning",
    "refinement": "planning",
    "execution": "execution",
    "build_gate": "execution",
    "execution_judge": "execution",
    "learning": "learning",
    "skill_curation": "learning",
}
# 2026-05-12: collapsed per-phase ceilings into a single `cost_ceiling` field.
# All phases share one ceiling; the historical planning/execution/learning
# split was bookkeeping noise (ceilings are observation-only, never halt).
PHASE_COST_CEILING_FIELDS = {
    "planning": "cost_ceiling",
    "execution": "cost_ceiling",
    "learning": "cost_ceiling",
}
PHASE_COST_OBSERVED_FIELDS = {
    "planning": "planning_cost_usd",
    "execution": "execution_cost_usd",
    "learning": "learning_cost_usd",
}


def _phaseCostGroup(phase):
    """Return the budget bucket used by a pipeline phase."""
    return PHASE_COST_GROUPS.get((phase or "").strip(), "execution")


def _phaseCostCeilingField(phase):
    """Return the frontmatter ceiling field for a phase."""
    return PHASE_COST_CEILING_FIELDS[_phaseCostGroup(phase)]


def _phaseCostObservedField(phase):
    """Return the observed-spend frontmatter field for a phase."""
    return PHASE_COST_OBSERVED_FIELDS[_phaseCostGroup(phase)]


def _parseCostValue(value, default=None):
    try:
        if value not in (None, ""):
            return float(value)
    except (ValueError, TypeError):
        pass
    return default


def _taskCostCeiling(task_frontmatter, phase=None):
    """Return the max cost (USD) allowed for a phase.

    Precedence is phase-specific field -> legacy max_cost_usd -> config default.
    Missing phase fields are valid so existing task files continue unchanged.
    """
    if phase:
        phase_override = _parseCostValue(task_frontmatter.get(_phaseCostCeilingField(phase), ""))
        if phase_override is not None:
            return phase_override

    legacy_override = _parseCostValue(task_frontmatter.get("max_cost_usd", ""))
    if legacy_override is not None:
        return legacy_override

    if _routing_params_loader:
        return float(_routing_params_loader().get("max_cost_per_task_usd", 2.00))
    return 2.00


def _startPhaseTimer():
    """Mark the start of a new phase for wall-clock duration tracking.

    Call this at the beginning of each phase (planning/refinement/execution/learning).
    `_writeCostLog` reads the elapsed time and includes it in the log entry.
    """
    global _phase_start_time
    _phase_start_time = time.monotonic()


def _phaseDurationSeconds():
    """Return seconds elapsed since `_startPhaseTimer()` was called. 0 if not started."""
    if _phase_start_time <= 0:
        return 0.0
    return round(time.monotonic() - _phase_start_time, 2)


def _recordCallCost(input_tokens: int, output_tokens: int, model: str):
    """Record cost of one AI call to the accumulator."""
    _task_cost_accumulator["passes"] += 1
    _task_cost_accumulator["input_tokens"] += input_tokens
    _task_cost_accumulator["output_tokens"] += output_tokens
    call_cost = _callCostUsd(input_tokens, output_tokens, model)
    _task_cost_accumulator["cost_usd"] += call_cost
    if not _task_cost_accumulator["model"] and model:
        _task_cost_accumulator["model"] = model


def _rotateCostLogIfNeeded(log_file: Path):
    """Archive oversized cost logs and leave a fresh active JSONL file."""
    try:
        from vault_graph.log_rotation import rotate_cost_log
    except ImportError:
        return

    max_mb = 5
    if _routing_params_loader:
        try:
            max_mb = _routing_params_loader().get("cost_log_max_mb", max_mb)
        except Exception:
            max_mb = 5
    rotate_cost_log(log_file, max_mb, datetime.utcnow().date())


@contextlib.contextmanager
def _fileLock(target_path, timeout=10.0, retry_interval=0.05):
    """Cross-platform file lock using atomic .lock-file creation."""
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
        print(f"    {YELLOW}[WARN] Lock timeout on {target_path.name} - proceeding without lock{RESET}")

    try:
        yield acquired
    finally:
        if acquired:
            try:
                lock_path.unlink()
            except (OSError, FileNotFoundError):
                pass


def _writeCostLog(task_name: str, phase: str, final_status: str, accumulator: dict, extra: dict = None):
    """Append one JSON line to logs/cost_log.jsonl for this phase.

    Args:
        extra: Optional dict of additional fields to merge into the log entry.
    """
    logs_dir = Path(__file__).parent.parent.parent / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    provider = "unknown"
    if accumulator.get("model"):
        model_lower = accumulator["model"].lower()
        # Exact match against MODEL_TIERS first - handles ambiguous names like
        # cursor's 'claude-4.6-opus-high-thinking' which looks like a claude model.
        for prov, tiers in _model_tiers.items():
            if model_lower in [t.lower() for t in tiers]:
                provider = prov
                break
        else:
            # Substring fallback for full model strings (e.g. 'claude-haiku-4-5')
            if "composer" in model_lower or model_lower.startswith("cursor"):
                provider = "cursor"
            elif model_lower.startswith("claude") or any(t in model_lower for t in ("haiku", "sonnet", "opus")):
                provider = "claude"
            elif model_lower.startswith("gemini") or any(t in model_lower for t in ("flash", "pro", "ultra")):
                provider = "gemini"
            elif model_lower.startswith("gpt") or "gpt" in model_lower:
                provider = "codex"

    entry = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "task": task_name,
        "phase": phase,
        "status": final_status,
        "provider": provider,
        "model": accumulator.get("model", "unknown"),
        "passes": accumulator.get("passes", 0),
        "input_tokens": accumulator.get("input_tokens", 0),
        "output_tokens": accumulator.get("output_tokens", 0),
        "cost_usd": accumulator.get("cost_usd", 0.0),
        "duration_seconds": _phaseDurationSeconds(),
    }

    if extra:
        entry.update(extra)

    log_file = logs_dir / "cost_log.jsonl"
    # Lock to serialise concurrent appends from multiple agent processes.
    with _fileLock(log_file, timeout=5.0):
        _rotateCostLogIfNeeded(log_file)
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")


def _loadPerformanceHistory(phase=None):
    """Load performance history from cost_log.jsonl, filtered by phase and cached by mtime.

    phase=None  -> all entries across every phase
    phase="execution" -> only execution-phase entries (used for execution routing decisions)

    Returns: dict keyed by (provider, tier) with {total, success, failed, cost_mean, cost_std_dev, cost_baseline}.
    Returns {} if file doesn't exist or no matching entries.
    """
    global _performance_history_cache, _performance_history_mtime

    logs_dir = Path(__file__).parent.parent.parent / "logs"
    log_file = logs_dir / "cost_log.jsonl"

    if not log_file.exists():
        return {}

    try:
        current_mtime = log_file.stat().st_mtime
        if current_mtime != _performance_history_mtime:
            # File changed - invalidate entire cache.
            _performance_history_cache = {}
            _performance_history_mtime = current_mtime

        cache_key = phase  # None or a phase string.
        if cache_key in _performance_history_cache:
            return _performance_history_cache[cache_key]

        history = {}
        with open(log_file, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    entry = json.loads(line)

                    if phase and entry.get("phase", "") != phase:
                        continue

                    provider = entry.get("provider", "unknown")
                    model = entry.get("model", "unknown")

                    model_lower = model.lower()
                    tier = None
                    provider_tiers = sorted(_model_tiers.get(provider, ()), key=len, reverse=True)
                    for tier_name in provider_tiers:
                        if tier_name in model_lower:
                            tier = tier_name
                            break
                    if not tier:
                        for tier_name in sorted(_MODEL_COSTS.keys(), key=len, reverse=True):
                            if tier_name in model_lower:
                                tier = tier_name
                                break

                    if not tier:
                        continue

                    key = (provider, tier)
                    if key not in history:
                        history[key] = {"total": 0, "success": 0, "failed": 0, "costs": [], "input_tokens": []}

                    history[key]["total"] += 1
                    if entry.get("status") == "success":
                        history[key]["success"] += 1
                    else:
                        history[key]["failed"] += 1

                    cost = entry.get("cost_usd", 0.0)
                    if cost:
                        history[key]["costs"].append(cost)

                    input_tok = entry.get("input_tokens", 0)
                    if input_tok:
                        history[key]["input_tokens"].append(input_tok)
                except (json.JSONDecodeError, KeyError):
                    continue

        for key in history:
            costs = history[key].pop("costs", [])
            input_toks = history[key].pop("input_tokens", [])
            history[key]["avg_cost_usd"] = sum(costs) / len(costs) if costs else 0.0
            history[key]["avg_input_tokens"] = sum(input_toks) / len(input_toks) if input_toks else 0
            # Keep raw cost data for drift detection - std dev needs the distribution.
            history[key]["cost_mean"] = history[key]["avg_cost_usd"]
            history[key]["cost_std_dev"] = statistics.stdev(costs) if len(costs) >= 2 else 0.0
            history[key]["cost_baseline"] = sum(costs[:3]) / len(costs[:3]) if costs else 0.0

        _performance_history_cache[cache_key] = history
        return history
    except Exception:
        return {}
