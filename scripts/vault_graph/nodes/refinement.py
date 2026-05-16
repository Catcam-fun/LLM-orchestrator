"""Real refinement + learning nodes — Day 4.

refinement: takes the human's answers, refines the plan, halts at human_approval
learning:   extracts skills, project context, model performance, routing params

Both ported from ai_dougs.processTask (refinement at line 3506+, learning at 3818+).
"""
from __future__ import annotations

from .. import ported as P
from ..state import TaskState
from .execution import _synthesise_task_text


def _provider_order():
    return P.loadConfig().get("aiProviderOrder", [{"provider": "claude", "model": "variable"}])


def _send_ai_requests():
    return P.loadConfig().get("sendAiRequests", True)


def _phase_print(state: TaskState, phase_name: str, message: str = ""):
    task = state.get("task_name", "?")
    extra = f"  {message}" if message else ""
    print(f"  [graph] {task}  →  {phase_name}{extra}", flush=True)


# ─── refinement ──────────────────────────────────────────────────────────────

def refinement(state: TaskState) -> dict:
    """Refine the plan based on the human's answers."""
    if state.get("status") in ("failed", "budget_exceeded"):
        return {"current_phase": "refinement", "next_action": "halt"}

    budget_update = P.budget_exceeded_update(state, "refinement")
    if budget_update:
        return budget_update

    # 2026-05-12: refinement now runs only on the human-driven path
    # (wait_human_answers -> refinement). The judge no longer routes here
    # automatically — its concerns surface to the user at wait_human_review,
    # who decides whether to merge or send back for another refinement pass.
    #
    # The plan_review's parsed concerns DO still get injected here so this
    # refinement pass addresses both the human's answers and the peer-reviewer's
    # concerns from the original plan round.
    plan_review_verdict = state.get("plan_review_verdict") or {}
    plan_review_has_concerns = bool(plan_review_verdict.get("concerns"))
    refinement_trigger = "incorporating human answers"
    if plan_review_has_concerns:
        refinement_trigger += " + plan_review concerns"
    _phase_print(state, "refinement", refinement_trigger)
    nas_root = P.vault_root()
    task_name = state.get("task_name", "?")
    P.reset_for_phase()
    P.set_current_app_location(state.get("app_location", ""))

    next_n = state.get("refinement_n", 0) + 1
    full_task = _synthesise_task_text(state)
    prompt = P.REFINEMENT_PROMPT.format(full_task=full_task, refinement_n=next_n)
    if plan_review_has_concerns:
        pr_concerns = plan_review_verdict.get("concerns") or []
        pr_scores = plan_review_verdict.get("scores") or {}
        pr_score_lines = "\n".join(f"  - {k}: {v}/5" for k, v in pr_scores.items())
        pr_concern_lines = "\n".join(f"  - {c}" for c in pr_concerns[:10]) or "  (none parsed)"
        pr_verdict = plan_review_verdict.get("verdict") or "?"
        plan_review_block = (
            "\n\n---\n\n"
            f"## Plan-review concerns ({pr_verdict})\n\n"
            "An independent reviewer scored the original plan before any "
            "human answers and flagged the following concerns. Treat these "
            "as orthogonal to the human's feedback — both should be "
            "addressed in this refinement pass.\n\n"
            f"Scores (out of 5):\n{pr_score_lines or '  (none parsed)'}\n\n"
            f"Concerns:\n{pr_concern_lines}\n"
        )
        prompt = prompt + plan_review_block

    response, provider, model = P.runAiRequestWithFallback(
        prompt, nas_root, [nas_root],
        _provider_order(), _send_ai_requests(),
        agent_mode=False, phase="refinement",
    )

    duration = P._phaseDurationSeconds()

    if not response:
        print(f"      {P.RED}refinement LLM call failed{P.RESET}")
        P._writeCostLog(task_name, "refinement", "failed", P._d._task_cost_accumulator,
                        extra={"routing_source": P._d._last_routing_source})
        P._appendFailureMode(nas_root, task_name, "refinement", "ai_request_failed",
                             "Refinement LLM call returned no response",
                             state.get("app_location", ""))
        return {
            "current_phase": "refinement",
            "status": "failed",
            "next_action": "halt",
            "last_failure_type": "ai_request_failed",
            "last_failure_details": "refinement LLM returned no response",
            "phase_durations": {**state.get("phase_durations", {}), "refinement": duration},
        }

    P._archivePromptResponse(nas_root, task_name, "refinement", prompt, response, provider, model)
    P._writeCostLog(task_name, "refinement", "success", P._d._task_cost_accumulator,
                    extra={"routing_source": P._d._last_routing_source})
    P._logContextInjection(task_name, "refinement")

    cost_snapshot = P.cost_accumulator_snapshot()
    new_cumulative = state.get("existing_cost_usd", 0.0) + cost_snapshot.get("cost_usd", 0.0)
    new_planning_cost = state.get("planning_cost_usd", 0.0) + cost_snapshot.get("cost_usd", 0.0)
    budget_update = P.phase_budget_exceeded_update(state, "refinement", new_planning_cost) or {}

    refinements = list(state.get("refinements", []))
    refinements.append(response)

    print(f"      {P.GREEN}refinement complete{P.RESET}  "
          f"{P.DIM}{provider}:{model} | ${cost_snapshot.get('cost_usd', 0):.4f} | "
          f"{duration:.1f}s{P.RESET}")

    out: dict = {
        "current_phase": "refinement",
        "refinements": refinements,
        "refinement_n": next_n,
        "cost_accumulator": cost_snapshot,
        "existing_cost_usd": new_cumulative,
        "planning_cost_usd": new_planning_cost,
        "last_routing_source": P._d._last_routing_source,
        "phase_durations": {**state.get("phase_durations", {}), "refinement": duration},
        "status": "pending_human_approval",
        "next_action": "wait_human_approval",
        **budget_update,
    }
    # Bug T fix (2026-05-11): write status back to disk frontmatter so
    # Obsidian sees the gate transition.
    try:
        from ..frontmatter_sync import sync_task_file_frontmatter
        sync_task_file_frontmatter({**state, **out})
    except Exception:
        pass
    return out


# ─── learning ────────────────────────────────────────────────────────────────

def learning(state: TaskState) -> dict:
    """Extract reusable knowledge from the completed task."""
    if state.get("status") in ("failed", "budget_exceeded"):
        # Even failed tasks could be learned from in principle, but mirror
        # ai_dougs.py current behaviour: skip learning if execution failed.
        return {"current_phase": "learning", "next_action": "complete", "status": "completed"}

    budget_update = P.budget_exceeded_update(state, "learning")
    if budget_update:
        return budget_update

    _phase_print(state, "learning", "extracting skills + project context + model perf")
    nas_root = P.vault_root()
    task_name = state.get("task_name", "?")
    app_location = state.get("app_location", "")
    P.reset_for_phase()
    P.set_current_app_location(app_location)

    full_task = _synthesise_task_text(state)
    if state.get("execution_log"):
        full_task += "\n\n## Execution Output\n\n" + state["execution_log"]
    if state.get("execution_diff"):
        full_task += f"\n\n## Diff\n\n```diff\n{P.truncate_observed(state['execution_diff'], 4000, 'learning.execution_diff', state.get('task_name', ''))}\n```"
    if state.get("human_feedback"):
        full_task += f"\n\n## Feedback from Human\n\n{state['human_feedback']}"

    prompt = P.LEARNING_PROMPT.format(
        full_task=P.truncate_observed(full_task, 10000, "learning.full_task", state.get("task_name", "")),
    )

    response, provider, model = P.runAiRequestWithFallback(
        prompt, nas_root, [nas_root],
        _provider_order(), _send_ai_requests(),
        agent_mode=False, phase="learning",
    )

    duration = P._phaseDurationSeconds()

    if not response:
        print(f"      {P.RED}learning LLM call failed (non-fatal — task still completes){P.RESET}")
        P._writeCostLog(task_name, "learning", "failed", P._d._task_cost_accumulator,
                        extra={"routing_source": P._d._last_routing_source})
        # Learning failure is non-fatal — task still completes
        return {
            "current_phase": "learning",
            "status": "completed",
            "next_action": "complete",
            "phase_durations": {**state.get("phase_durations", {}), "learning": duration},
        }

    P._archivePromptResponse(nas_root, task_name, "learning", prompt, response, provider, model)
    P._writeCostLog(task_name, "learning", "success", P._d._task_cost_accumulator,
                    extra={"routing_source": P._d._last_routing_source})
    P._logContextInjection(task_name, "learning")

    # ── Parse the structured learning output ─────────────────────────────────
    # parseLearningOutput returns (model_content, params_content) — the only
    # learning signals still consumed (auto-curator, project-context auto-write,
    # and context-effectiveness pruning were all removed 2026-05-12).
    model_content, params_content = P.parseLearningOutput(response)
    written_skill = None  # auto-curator removed; skills are co-authored

    if model_content:
        try:
            P.updateModelPerformance(nas_root, model_content)
        except Exception as e:
            print(f"      {P.YELLOW}model performance update failed: {e}{P.RESET}")

    if params_content:
        try:
            param_suggestions = P._parseParamSuggestions(params_content)
            if param_suggestions:
                P._updateRoutingParams(param_suggestions)
                print(f"      {P.DIM}routing params updated: {param_suggestions}{P.RESET}")
        except Exception as e:
            print(f"      {P.YELLOW}routing params update failed: {e}{P.RESET}")

    # ── Skill effectiveness tracking ─────────────────────────────────────────
    # Parse what the agent reported about which loaded skills it actually used.
    # Always log the loaded set even if `used`/`helpful` are empty — that's
    # important data: "skill X got loaded but never used" → eventually rank down.
    loaded = state.get("loaded_skills", []) or []
    if loaded:
        try:
            from .. import skills as skills_module
            used, helpful = skills_module.parse_skills_used_from_learning(response)
            skills_module.log_skill_usage(task_name, loaded, used, helpful)
            note = f"used {len(used)}/{len(loaded)}"
            if helpful:
                note += f", helpful {len(helpful)}"
            print(f"      {P.DIM}skill effectiveness logged: {note}{P.RESET}")
        except Exception as e:
            print(f"      {P.YELLOW}skill effectiveness log failed: {e}{P.RESET}")

    # Core memory append removed 2026-05-12 (file was source-coupled; agent
    # learning lives in skills now, co-authored).

    cost_snapshot = P.cost_accumulator_snapshot()
    new_cumulative = state.get("existing_cost_usd", 0.0) + cost_snapshot.get("cost_usd", 0.0)
    new_learning_cost = state.get("learning_cost_usd", 0.0) + cost_snapshot.get("cost_usd", 0.0)
    budget_update = P.phase_budget_exceeded_update(state, "learning", new_learning_cost) or {}

    skill_note = f" + skill: {written_skill}" if written_skill else ""
    print(f"      {P.GREEN}learning complete{P.RESET}  "
          f"{P.DIM}{provider}:{model} | ${cost_snapshot.get('cost_usd', 0):.4f}{skill_note}{P.RESET}")

    # Post-learning re-verify (Fix B) REMOVED 2026-05-12.
    # Original purpose: catch curator regressions of verified content.
    # The curator was disabled then removed 2026-05-11 / 2026-05-12;
    # learning phase no longer mutates skill content, so there's nothing
    # to re-verify defensively. The verification_outcome from execution
    # is the final word.
    updated_verification = None

    return_dict = {
        "current_phase": "learning",
        "learning_output": response,
        "cost_accumulator": cost_snapshot,
        "existing_cost_usd": new_cumulative,
        "learning_cost_usd": new_learning_cost,
        "phase_durations": {**state.get("phase_durations", {}), "learning": duration},
        "status": "completed",
        "next_action": "complete",
        **budget_update,
    }
    if updated_verification is not None:
        return_dict["verification_outcome"] = updated_verification
    return return_dict
