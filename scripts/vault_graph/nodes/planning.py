"""Real planning + plan_review nodes — Day 2.

Replaces the stubs in nodes/stubs.py for these two phases:
- task_entry  → pre-flight linter + cost ceiling check
- planning    → build prompt, route model, call LLM, archive, log cost
- plan_review → independent reviewer on the plan (runs unconditionally
                per the 2026-05-08 quality hierarchy directive; skipping
                a quality gate to save tokens is an anti-pattern)

All other phases still use the stubs from stubs.py until later days port them.
"""
from __future__ import annotations
import time
from datetime import datetime

from .. import ported as P
from .. import project_memory
from ..state import TaskState


# Provider order for graph runs. Pulled from the loaded config so user can
# override via ai_dougs_config.json. We keep using the same config file during
# migration for consistency.
def _provider_order():
    cfg = P.loadConfig()
    return cfg.get("aiProviderOrder", [{"provider": "claude", "model": "variable"}])


def _send_ai_requests():
    return P.loadConfig().get("sendAiRequests", True)


def _phase_print(state: TaskState, phase_name: str, message: str = ""):
    task = state.get("task_name", "?")
    extra = f"  {message}" if message else ""
    print(f"  [graph] {task}  →  {phase_name}{extra}", flush=True)


# ─── task_entry — REAL (linter + budget gate) ────────────────────────────────

def task_entry(state: TaskState) -> dict:
    """Pre-flight checks before any LLM work begins.

    Runs in this order (cheapest first, fail fast):
    1. Pre-flight task linter — structural validation of the task
    2. Cost ceiling check — abort if previous runs already exceeded budget
    3. Phase timer start

    On failure, returns a state update that flips status to a halted state.
    The graph still proceeds to planning, but planning will see the failure
    state and short-circuit. (Conditional edges to skip planning come later.)
    """
    _phase_print(state, "task_entry")
    nas_root = P.vault_root()
    app_location = state.get("app_location", "")

    # ── 1. Pre-flight linter ──────────────────────────────────────────────────
    task_dict = P.task_dict_from_state(state)
    lint_errors = P._lintTaskFile(task_dict, nas_root)
    if lint_errors:
        for err in lint_errors:
            print(f"      {P.RED}lint:{P.RESET} {err}")
        return {
            "current_phase": "task_entry",
            "status": "failed",
            "next_action": "halt",
            "last_failure_type": "preflight_lint",
            "last_failure_details": " | ".join(lint_errors),
        }

    # ── 2. Cost ceiling ───────────────────────────────────────────────────────
    memory_dir = project_memory.bootstrap_project_memory(app_location, nas_root)
    if memory_dir is not None:
        print(f"      {P.DIM}project memory: {memory_dir.relative_to(nas_root)}{P.RESET}")

    budget_update = P.budget_exceeded_update(state, "planning")
    if budget_update:
        return {**budget_update, "current_phase": "task_entry"}

    # ── 3. Reset cost accumulator + start phase timer ─────────────────────────
    P.reset_for_phase()

    return {
        "current_phase": "task_entry",
        "phase_start_time": time.monotonic(),
        "next_action": "planning",
    }


# ─── planning — REAL ─────────────────────────────────────────────────────────

def run_planning_phase(state: TaskState, dry_run: bool = False) -> dict:
    """Generate a plan via the LLM.

    dry_run reuses the normal planner/prompt path but suppresses persistence
    side effects so replay can compare historical prompts without mutation.
    """
    message = "building prompt + routing model"
    if dry_run:
        message += " (dry run)"
    _phase_print(state, "planning", message)
    nas_root = P.vault_root()
    task_name = state.get("task_name", "?")
    initial_prompt = state.get("initial_prompt", "")
    app_location = state.get("app_location", "")

    # If task_entry already failed, short-circuit
    if state.get("status") in ("failed", "budget_exceeded"):
        return {"current_phase": "planning", "next_action": "halt"}

    budget_update = P.budget_exceeded_update(state, "planning")
    if budget_update:
        return budget_update

    P.reset_for_phase()

    # CRITICAL: set the global that _selectContextFilesForTask reads so that
    # per-project .agent-context.md gets loaded. Without this, app-specific
    # context (tech stack, conventions, gotchas) is invisible to the planner.
    P.set_current_app_location(app_location)

    # Resolve any @file: references in the prompt (expands them inline)
    initial_prompt = P.resolveFilePrompt(initial_prompt, nas_root)

    # ── Build planning context ────────────────────────────────────────────────
    # Mirrors what processTask does in ai_dougs lines ~3330: scout + failures
    # + session digest + base context
    context = "No additional context provided."

    app_path = nas_root / app_location if app_location else nas_root
    if app_location and app_path.exists():
        try:
            compact = P.scoutProject(app_path)
            if compact:
                context += "\n\n## Current Project Files (auto-scouted)\n\n" + compact
                print(f"      {P.DIM}scout: {len(compact):,} chars{P.RESET}")

            # Full snapshot to disk for execution phase to use later
            if not dry_run:
                full_snap = P.scoutProjectFull(app_path)
                if full_snap:
                    snap_file = app_path / ".project-snapshot.md"
                    snap_file.write_text(full_snap, encoding="utf-8")
        except Exception as e:
            print(f"      {P.YELLOW}scout failed: {e}{P.RESET}")

    # Failure-modes injection REMOVED 2026-05-12. failure_modes.md is data
    # (append-only audit log), not prescriptive guidance. Injecting it into
    # the planning prompt biased the planner toward defensive over-engineering
    # based on stale / one-off / false-positive entries. The right layers:
    #   - "What to avoid for this domain"  →  Skills (co-authored)
    #   - "Watch for this symptom live"    →  Supervisor symptom rules
    #   - "What went wrong on THIS project" →  Per-project failures.md in
    #                                          vault/.agent_memory/<slug>/
    # The catalog is still written (failure logging is preserved); it's just
    # no longer pre-loaded into planning context.

    project_memory_context = project_memory.load_project_memory_context(app_location, nas_root)
    if project_memory_context:
        context += "\n\n" + project_memory_context
        print(f"      {P.DIM}injected Project Agent Memory ({len(project_memory_context)} chars){P.RESET}")

    # Inject latest session digest
    session_digest = P._loadLatestSessionDigest(nas_root, max_chars=2500)
    if session_digest:
        context += "\n\n" + session_digest
        print(f"      {P.DIM}injected latest session digest{P.RESET}")

    # ── Skills auto-load (relevance-scored from ai_skills/) ──────────────────
    from .. import skills as skills_module
    relevant_skills = skills_module.select_relevant_skills(initial_prompt, max_skills=3)
    skills_block = skills_module.build_skills_context_block(relevant_skills)
    loaded_skill_names: list[str] = [s["name"] for s in relevant_skills]
    if skills_block:
        context += "\n\n" + skills_block
        print(f"      {P.DIM}injected {len(relevant_skills)} skill(s): {', '.join(loaded_skill_names)}{P.RESET}")

    # Core memory injection removed 2026-05-12. core_memory.md accumulated
    # source-coupled history (auto_NNNN entries) — exactly the muddle-zone
    # we said shouldn't exist. Skills carry prescriptive guidance now.

    # ── Build the prompt ──────────────────────────────────────────────────────
    prompt = P.PLANNING_PROMPT.format(initial_prompt=initial_prompt, context=context)

    # ── Call the LLM with phase-aware routing ─────────────────────────────────
    provider_order = _provider_order()
    response, provider, model = P.runAiRequestWithFallback(
        prompt, nas_root, [app_path, nas_root],
        provider_order, _send_ai_requests(),
        agent_mode=False, phase="planning",
    )

    duration = P._phaseDurationSeconds()

    # ── Failure path ──────────────────────────────────────────────────────────
    if not response:
        print(f"      {P.RED}planning LLM call failed{P.RESET}")
        if not dry_run:
            P._writeCostLog(task_name, "planning", "failed", P._d._task_cost_accumulator,
                            extra={"routing_source": P._d._last_routing_source})
            P._appendFailureMode(nas_root, task_name, "planning", "ai_request_failed",
                                 "Planning LLM call returned no response", app_location)
        return {
            "current_phase": "planning",
            "status": "failed",
            "next_action": "halt",
            "last_failure_type": "ai_request_failed",
            "last_failure_details": "planning LLM returned no response",
            "phase_durations": {**state.get("phase_durations", {}), "planning": duration},
        }

    # ── Success path ──────────────────────────────────────────────────────────
    if not dry_run:
        P._archivePromptResponse(nas_root, task_name, "planning", prompt, response, provider, model)
        P._writeCostLog(task_name, "planning", "success", P._d._task_cost_accumulator,
                        extra={"routing_source": P._d._last_routing_source})
        # Telemetry: which context files were injected this phase (effectiveness loop)
        P._logContextInjection(task_name, "planning")

    # Snapshot the cost accumulator into TaskState so it persists in checkpoint
    cost_snapshot = P.cost_accumulator_snapshot()
    new_cumulative = state.get("existing_cost_usd", 0.0) + cost_snapshot.get("cost_usd", 0.0)
    new_planning_cost = state.get("planning_cost_usd", 0.0) + cost_snapshot.get("cost_usd", 0.0)
    budget_update = P.phase_budget_exceeded_update(state, "planning", new_planning_cost) or {}

    print(f"      {P.GREEN}planning complete{P.RESET}  "
          f"{P.DIM}{provider}:{model} | ${cost_snapshot.get('cost_usd', 0):.4f} | "
          f"{duration:.1f}s{P.RESET}")

    return {
        "current_phase": "planning",
        "plan_text": response,
        "planning_model": f"{provider}:{model}" if provider and model else "",
        "cost_accumulator": cost_snapshot,
        "existing_cost_usd": new_cumulative,
        "planning_cost_usd": new_planning_cost,
        "last_routing_source": P._d._last_routing_source,
        "phase_durations": {**state.get("phase_durations", {}), "planning": duration},
        "loaded_skills": loaded_skill_names,  # for learning phase to track effectiveness
        "next_action": "plan_review_check",
        **budget_update,
    }


# ─── plan_review — REAL (adaptive) ───────────────────────────────────────────

def planning(state: TaskState) -> dict:
    """Generate a plan via the LLM, with all the bells and whistles."""
    return run_planning_phase(state, dry_run=False)


def plan_review(state: TaskState) -> dict:
    """Independent model reviews the plan. Skipped when planning has been
    reliable (the gating logic in _shouldRunPlanReview decides)."""

    # If planning short-circuited, skip review too
    if state.get("status") in ("failed", "budget_exceeded"):
        return {"current_phase": "plan_review", "next_action": "halt"}

    budget_update = P.budget_exceeded_update(state, "plan_review")
    if budget_update:
        return budget_update

    if not P._shouldRunPlanReview():
        _phase_print(state, "plan_review", "skipped (kill-switch disabled)")
        skip_out: dict = {
            "current_phase": "plan_review",
            "next_action": "wait_human_answers",
            "status": "pending_human_answers",
        }
        try:
            from ..frontmatter_sync import sync_task_file_frontmatter
            sync_task_file_frontmatter({**state, **skip_out})
        except Exception:
            pass
        return skip_out

    _phase_print(state, "plan_review", "running independent reviewer")
    nas_root = P.vault_root()
    task_name = state.get("task_name", "?")
    initial_prompt = state.get("initial_prompt", "")
    plan_text = state.get("plan_text", "")
    planning_model_str = state.get("planning_model", "")

    P.reset_for_phase()
    P.set_current_app_location(state.get("app_location", ""))

    # Parse "provider:model" back out of the planning_model string
    if planning_model_str and ":" in planning_model_str:
        planning_provider, planning_model = planning_model_str.split(":", 1)
    else:
        planning_provider, planning_model = "", ""

    review_text, rev_provider, rev_model = P._runPlanReview(
        initial_prompt, plan_text, nas_root, [P.vault_root()],
        _provider_order(), _send_ai_requests(),
        planning_provider, planning_model, task_name,
    )

    duration = P._phaseDurationSeconds()

    if not review_text:
        # Plan review failure is non-fatal — proceed to human gate without a review
        print(f"      {P.YELLOW}plan review failed — proceeding without review{P.RESET}")
        fail_out: dict = {
            "current_phase": "plan_review",
            "plan_review_text": "(plan review failed; proceeding to human gate)",
            "plan_review_verdict": {},
            "next_action": "wait_human_answers",
            "status": "pending_human_answers",
            "phase_durations": {**state.get("phase_durations", {}), "plan_review": duration},
        }
        try:
            from ..frontmatter_sync import sync_task_file_frontmatter
            sync_task_file_frontmatter({**state, **fail_out})
        except Exception:
            pass
        return fail_out

    # GAP #4 fix (2026-05-11): parse plan_review into a structured verdict so
    # refinement can arbitrate the same way it does for execution_judge.
    # Until now plan_review was advisory-only; the verdict line was generated
    # but never read. Now parsed concerns flow through to the refinement node.
    plan_review_verdict = {}
    try:
        from ..execution_judge import parse_plan_review_verdict
        plan_review_verdict = parse_plan_review_verdict(review_text)
        verdict = plan_review_verdict.get("verdict") or "?"
        score = plan_review_verdict.get("score_avg", 0)
        n_concerns = len(plan_review_verdict.get("concerns", []))
        color = (P.GREEN if verdict == "APPROVE"
                 else (P.YELLOW if verdict == "NEEDS_REVISION"
                       else P.RED if verdict == "REJECT" else P.DIM))
        print(f"      {color}plan_review verdict: {verdict} (avg {score}/5, "
              f"{n_concerns} concern(s)){P.RESET}")
    except Exception as e:
        print(f"      {P.YELLOW}plan_review verdict parse errored (non-fatal): {e}{P.RESET}")

    cost_snapshot = P.cost_accumulator_snapshot()
    new_cumulative = state.get("existing_cost_usd", 0.0) + cost_snapshot.get("cost_usd", 0.0)
    new_planning_cost = state.get("planning_cost_usd", 0.0) + cost_snapshot.get("cost_usd", 0.0)
    budget_update = P.phase_budget_exceeded_update(state, "plan_review", new_planning_cost) or {}

    print(f"      {P.GREEN}plan review complete{P.RESET}  "
          f"{P.DIM}{rev_provider}:{rev_model} | {duration:.1f}s{P.RESET}")

    # Prompt-variant A/B recording removed 2026-05-12 (no signal yet).

    out: dict = {
        "current_phase": "plan_review",
        "plan_review_text": review_text,
        "plan_review_provider": rev_provider or "",
        "plan_review_model": rev_model or "",
        "plan_review_verdict": plan_review_verdict,
        "cost_accumulator": cost_snapshot,
        "existing_cost_usd": new_cumulative,
        "planning_cost_usd": new_planning_cost,
        "phase_durations": {**state.get("phase_durations", {}), "plan_review": duration},
        "next_action": "wait_human_answers",
        "status": "pending_human_answers",
        **budget_update,
    }
    # Bug T fix (2026-05-11): sync the task file's frontmatter status BEFORE
    # the interrupt fires, so Obsidian (Bases / Dataview) sees the gate.
    try:
        from ..frontmatter_sync import sync_task_file_frontmatter
        sync_task_file_frontmatter({**state, **out})
    except Exception:
        pass
    return out
