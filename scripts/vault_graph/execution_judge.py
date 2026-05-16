"""Cross-model judge for execution outputs.

After execution completes (build gate passed), an INDEPENDENT model scores
the work on quality axes. This is the execution-phase analog of plan_review.
The judge doesn't auto-revert — it just provides a quality signal that:
  - Surfaces to the human in the task state
  - Logs to cost_log with phase=execution_judge for routing evidence
  - Feeds the cross-model quality signal that's been missing from routing
    (which previously only saw build pass/fail and human approval/rejection)

Quality gate (per the 2026-05-08 quality hierarchy directive): the judge
runs on every successful execution. There is NO skip-on-success-rate.
The only honored config flag is the emergency kill-switch:
  execution_judge_enabled (default True)

REMOVED 2026-05-08:
  execution_judge_skip_above_success_rate (was 0.95)
  execution_judge_min_execution_runs (was 5)
Both were token-efficiency-over-quality anti-patterns. The right way to
make the judge cheaper is to find a cheaper provider that scores with
equivalent accuracy and route to it (the routing layer's job), NEVER to
skip the check. See VISION.md "Quality hierarchy" + failure_modes
vision/quality_subordinate_to_tokens.

The judge prompt asks for structured output:
  SCORES: 5 axes scored 1-5
  CONCERNS: list of issues (or "None")
  VERDICT: APPROVE / NEEDS_REVISION / REJECT
"""
from __future__ import annotations

from . import ported as P


EXECUTION_JUDGE_PROMPT = """\
You are reviewing the output of an automated code execution by another AI agent.
Your job is NOT to redo the work — you score the result so the human knows what to look at.

The task, plan, and what the executing agent actually did are below. The git diff
shows the actual changes made.

{project_scope}
Use the project scope above to weight the axes below appropriately. A change that
advances the project's stated goals (vault quality directives or charter) should
not be penalized for being unconventional; a change that ignores the project's
goals should be flagged in concerns.

Score on these axes (1-5 each, 5 = excellent):
1. plan_adherence — did the implementation match the agreed plan?
2. scope_discipline — were changes focused, or did the agent expand scope?
3. code_quality — readability, idioms, maintainability of the changes
4. completeness — did it actually finish the task, or are there obvious gaps?
5. risk — how likely is this change to break something? (5 = very safe, 1 = risky)

Respond in this EXACT format (no other text):

SCORES:
- plan_adherence: <1-5>
- scope_discipline: <1-5>
- code_quality: <1-5>
- completeness: <1-5>
- risk: <1-5>

CONCERNS:
- <one concern per line, or "None" if no concerns>

VERDICT: <APPROVE | NEEDS_REVISION | REJECT>

ORIGINAL TASK:
{initial_prompt}

PLAN (the agreed approach):
{plan}

EXECUTION OUTPUT (what the agent reported doing):
{execution_log}

ACTUAL DIFF (the changes that landed):
```diff
{diff}
```
"""


def should_run_execution_judge() -> bool:
    """Quality gate — runs on every successful execution.

    PRIOR DESIGN (removed 2026-05-08): adaptive skip when success rate >0.95
    over the last 5+ runs. The skip was a token-efficiency optimization that
    violated the vault's quality hierarchy:

        quality of product > efficiency of vault > token efficiency

    Skipping the judge is a quality compromise. The right way to make it
    cheaper is to find a cheaper model that judges with equivalent accuracy
    (the routing layer's job, evidence-driven), NEVER to skip the check.
    See VISION.md "Safety through observation, not arbitrary cutoffs"
    and the failure_modes.md entry tagged
    `vision/quality_subordinate_to_tokens` for the canonical anti-pattern.

    The `execution_judge_enabled` flag remains as an emergency kill-switch
    (e.g. when all judge providers are quota-banned). Default True. The
    skip-above-success-rate and min-runs params are now unused — kept in
    config for backward read-compat but ignored.
    """
    params = P._loadRoutingParams()
    return bool(params.get("execution_judge_enabled", True))


def parse_judge_verdict(review_text: str) -> dict:
    """Parse the structured judge response into {verdict, scores, concerns}.

    Returns:
        {
            "verdict": "APPROVE" | "NEEDS_REVISION" | "REJECT" | "" (unparseable),
            "scores": {plan_adherence, scope_discipline, code_quality, completeness, risk},
            "concerns": [str, ...],
            "score_avg": float (0 if unparseable),
        }

    Used by terminal_complete to gate completion: REJECT blocks completion
    even if the verification block passed (defense-in-depth — the judge sees
    the diff + plan, the verification block doesn't have visibility into
    whether the change matches the *intent*).
    """
    import re as _re
    if not review_text:
        return {"verdict": "", "scores": {}, "concerns": [], "score_avg": 0.0}
    # Verdict
    verdict = ""
    m = _re.search(r"^VERDICT:\s*(APPROVE|NEEDS_REVISION|REJECT)\b",
                   review_text, _re.M)
    if m:
        verdict = m.group(1)
    # Scores
    scores: dict = {}
    for axis in ("plan_adherence", "scope_discipline", "code_quality",
                 "completeness", "risk"):
        m = _re.search(rf"^-\s*{axis}:\s*(\d+)", review_text, _re.M)
        if m:
            try:
                scores[axis] = int(m.group(1))
            except ValueError:
                pass
    # Concerns
    concerns: list[str] = []
    m = _re.search(r"^CONCERNS:\s*\n((?:^- .*\n?)+)", review_text, _re.M)
    if m:
        for line in m.group(1).splitlines():
            line = line.strip().lstrip("- ").strip()
            if line and line.lower() != "none":
                concerns.append(line[:300])
    score_avg = sum(scores.values()) / max(len(scores), 1) if scores else 0.0
    return {
        "verdict": verdict,
        "scores": scores,
        "concerns": concerns,
        "score_avg": round(score_avg, 2),
    }


# Plan-review verdict shape mirrors the execution judge for parity (GAP #4
# fix, 2026-05-11). The PLAN_REVIEW_PROMPT uses NEEDS_REFINEMENT instead of
# NEEDS_REVISION historically — we accept both spellings on parse so the
# downstream consumers can rely on one canonical value.
_PLAN_REVIEW_AXES = ("scope_clarity", "completeness", "specificity",
                     "risk_awareness", "verifiability")


def parse_plan_review_verdict(review_text: str) -> dict:
    """Parse the plan_review response into {verdict, scores, concerns, score_avg}.

    Parallels parse_judge_verdict for the execution judge so plan_review can
    arbitrate downstream phases the same way (GAP #4 fix, 2026-05-11). Accepts
    both NEEDS_REFINEMENT (plan_review's historical token) and NEEDS_REVISION
    (execution_judge's token); both are normalized to NEEDS_REVISION on output
    so refinement.py can read one canonical value.

    Returns:
        {
            "verdict": "APPROVE" | "NEEDS_REVISION" | "REJECT" | "",
            "scores": {scope_clarity, completeness, specificity, risk_awareness, verifiability},
            "concerns": [str, ...],
            "score_avg": float,
        }
    """
    import re as _re
    if not review_text:
        return {"verdict": "", "scores": {}, "concerns": [], "score_avg": 0.0}
    # Verdict — accept both spellings
    verdict = ""
    m = _re.search(r"^VERDICT:\s*(APPROVE|NEEDS_REFINEMENT|NEEDS_REVISION|REJECT)\b",
                   review_text, _re.M)
    if m:
        raw = m.group(1)
        verdict = "NEEDS_REVISION" if raw == "NEEDS_REFINEMENT" else raw
    # Scores
    scores: dict = {}
    for axis in _PLAN_REVIEW_AXES:
        m = _re.search(rf"^-\s*{axis}:\s*(\d+)", review_text, _re.M)
        if m:
            try:
                scores[axis] = int(m.group(1))
            except ValueError:
                pass
    # Concerns
    concerns: list[str] = []
    m = _re.search(r"^CONCERNS:\s*\n((?:^- .*\n?)+)", review_text, _re.M)
    if m:
        for line in m.group(1).splitlines():
            line = line.strip().lstrip("- ").strip()
            if line and line.lower() != "none":
                concerns.append(line[:300])
    score_avg = sum(scores.values()) / max(len(scores), 1) if scores else 0.0
    return {
        "verdict": verdict,
        "scores": scores,
        "concerns": concerns,
        "score_avg": round(score_avg, 2),
    }


# Bug BB tuning constants (2026-05-11): adaptive diff budget for the judge.
# Small diffs go through verbatim; large diffs get a head+tail sandwich plus
# a file-list summary so the judge knows the full scope even when content is
# truncated.
_JUDGE_DIFF_BUDGET_SMALL = 6000   # ≤ this size: include verbatim
_JUDGE_DIFF_BUDGET_MEDIUM = 24000  # ≤ this size: include verbatim (4x previous cap)
_JUDGE_DIFF_HEAD_TAIL = 10000      # for large diffs: include head + tail of this size each


def _extract_file_list_from_diff(diff_text: str) -> list[str]:
    """Parse `diff --git a/<path> b/<path>` headers + per-file diff headers
    out of a unified-diff blob. Used to summarize a truncated diff for the
    judge so it knows which files were touched even when content is cut."""
    import re as _re
    paths: set[str] = set()
    # `diff --git a/foo b/foo` form
    for m in _re.finditer(r"^diff --git a/(\S+) b/(\S+)", diff_text, _re.M):
        paths.add(m.group(2))
    # `+++ b/foo` form (in case of unified diff without git wrapper)
    for m in _re.finditer(r"^\+\+\+ b/(\S+)", diff_text, _re.M):
        paths.add(m.group(1))
    # Bug S content-diff blocks header: "Content diff vs pre-execution backup
    # (...) — N file(s)" followed by `+++ b/<path> (post-execution)` lines
    for m in _re.finditer(r"^\+\+\+ b/(\S+)\s+\(post-execution\)", diff_text, _re.M):
        paths.add(m.group(1))
    return sorted(paths)


def _build_diff_for_judge(diff_text: str, task_name: str) -> str:
    """Return a diff string sized for the judge prompt. Adaptive: full text
    for small diffs, head + tail + file-list summary for large ones.

    Bug BB fix (2026-05-11): the previous fixed 6000-char truncation hid
    most of a multi-file redesign from the judge, which then invented
    phantom concerns about files it couldn't see. The new policy keeps
    the judge's signal honest at small diff sizes and gives it a full
    file inventory at large sizes."""
    if not diff_text:
        return ""
    n = len(diff_text)
    # Small: pass through verbatim
    if n <= _JUDGE_DIFF_BUDGET_SMALL:
        return diff_text
    files = _extract_file_list_from_diff(diff_text)
    # Medium: still verbatim, but warn the judge in a header
    if n <= _JUDGE_DIFF_BUDGET_MEDIUM:
        try:
            P.truncate_observed(diff_text, _JUDGE_DIFF_BUDGET_MEDIUM,
                               "execution_judge.diff_medium", task_name)
        except Exception:
            pass
        header = (f"<diff: {n} chars across {len(files)} file(s) — within "
                  f"judge medium budget, full content below>\n\n")
        return header + diff_text
    # Large: head + tail + file-list summary
    head = diff_text[:_JUDGE_DIFF_HEAD_TAIL]
    tail = diff_text[-_JUDGE_DIFF_HEAD_TAIL:]
    file_list_md = "\n".join(f"  - {p}" for p in files[:80])
    if len(files) > 80:
        file_list_md += f"\n  - ... and {len(files) - 80} more"
    try:
        P.truncate_observed(diff_text, _JUDGE_DIFF_HEAD_TAIL * 2,
                           "execution_judge.diff_large_truncated", task_name)
    except Exception:
        pass
    return (
        f"<diff: {n} chars across {len(files)} file(s) — exceeds judge "
        f"budget, showing head + tail with file-list summary>\n\n"
        f"FILES MODIFIED IN THIS DIFF:\n{file_list_md or '  (none parsed)'}\n\n"
        f"--- DIFF HEAD ({len(head)} chars) ---\n{head}\n\n"
        f"--- ... {n - 2 * _JUDGE_DIFF_HEAD_TAIL} chars omitted from middle ... ---\n\n"
        f"--- DIFF TAIL ({len(tail)} chars) ---\n{tail}\n"
    )


def run_execution_judge(initial_prompt: str, plan: str, execution_log: str,
                        diff: str, vault_root, provider_order, send_ai_requests: bool,
                        executing_provider: str, executing_model: str,
                        task_name: str,
                        refinements: list[str] | None = None,
                        app_location: str = "") -> tuple[str, str, str]:
    """Run an independent model to score the execution output.

    Returns (review_text, judge_provider, judge_model). All None on failure.

    `app_location` (GAP #1 fix, 2026-05-11): the project this task belongs to.
    Used to load project-scoped context into the judge prompt so the 5 generic
    axes get weighted appropriately for the project type (vault internals,
    CLI tool, UI redesign, etc.). Empty string → vault-internal default scope.
    """
    from .verification import (extract_verification_block,
                               extract_verification_blocks_from_refinements)
    from .project_memory import load_judge_scope_block

    checks, policy, err = extract_verification_block(plan)
    extra_checks = extract_verification_blocks_from_refinements(refinements)

    if err is not None:
        if extra_checks:
            checks = extra_checks
            policy = {}
    elif extra_checks:
        # Build id index of existing checks for dedupe/replace semantics
        id_to_idx = {c.get("id"): i for i, c in enumerate(checks) if isinstance(c, dict) and c.get("id")}
        for extra in extra_checks:
            extra_id = extra.get("id")
            if extra_id and extra_id in id_to_idx:
                checks[id_to_idx[extra_id]] = extra  # replace by id
            else:
                checks.append(extra)  # append new

    effective_plan = plan
    if checks:
        try:
            import yaml
            effective_plan = "```yaml\n" + yaml.safe_dump(
                {
                    "verification": checks,
                    "verification_policy": policy or {},
                },
                sort_keys=False,
                default_flow_style=False,
            ) + "```"
        except Exception:
            effective_plan = str({
                "verification": checks,
                "verification_policy": policy or {},
            })

    # GAP #1 fix (2026-05-11): load project-scope block so the judge sees
    # what project this task is for, and can weight axes appropriately.
    # Defensive: never let a scope-load failure block the judge call.
    try:
        project_scope = load_judge_scope_block(app_location, vault_root)
    except Exception:
        project_scope = ""
    if not project_scope:
        project_scope = "PROJECT SCOPE: (unavailable — using default axis weights)\n"

    # Bug BB fix (2026-05-11): scale the diff budget with diff size so multi-
    # file work doesn't get truncated to a misleading snippet. The old fixed
    # 6000-char budget caused the judge to hallucinate phantom concerns about
    # files that weren't in the truncated view (auto_0076 case: "AppHeader
    # removed from App.js" — App.js wasn't even in the modified set, the
    # judge just didn't see it in the 6000-char window).
    #
    # New policy:
    #   - small diffs (≤ 6000): unchanged, full diff goes through
    #   - medium diffs (6000-24000): allow up to 24000 chars
    #   - large diffs (> 24000): include head + tail + a "<diff truncated>"
    #     marker, plus a file-list summary so the judge knows what was touched
    diff_text = diff or ""
    diff_for_judge = _build_diff_for_judge(diff_text, task_name)

    # Truncation observability (2026-05-09): truncate_observed logs when a
    # cropping actually happened so future symptom rules can fire. See
    # PLAN.md "Threshold audit" + ported.truncate_observed docstring.
    judge_prompt = EXECUTION_JUDGE_PROMPT.format(
        project_scope=P.truncate_observed(project_scope, 2000, "execution_judge.project_scope", task_name),
        initial_prompt=P.truncate_observed(initial_prompt, 3000, "execution_judge.initial_prompt", task_name),
        plan=P.truncate_observed(effective_plan, 5000, "execution_judge.plan", task_name),
        execution_log=P.truncate_observed(execution_log, 5000, "execution_judge.execution_log", task_name),
        diff=diff_for_judge if diff_text else "(no diff captured)",
    )

    # Build a provider order that prefers something different from the executor.
    # If routing happens to pick the same model, that's fine — it's a degenerate
    # case that still adds value as self-review.
    diverse_order = []
    for entry in provider_order:
        new_entry = dict(entry)
        if (entry.get("provider") == executing_provider
                and entry.get("model") == executing_model
                and (entry.get("model", "").lower() not in ("variable", "decide"))):
            new_entry["model"] = "variable"
        diverse_order.append(new_entry)

    print(f"      {P.DIM}running execution judge (independent model)...{P.RESET}")
    review_response, j_provider, j_model = P.runAiRequestWithFallback(
        judge_prompt, vault_root, [vault_root], diverse_order, send_ai_requests,
        agent_mode=False, phase="execution_judge",
    )
    if review_response:
        P._archivePromptResponse(vault_root, task_name, "execution_judge",
                                 judge_prompt, review_response, j_provider, j_model)
        P._writeCostLog(task_name, "execution_judge", "success",
                        P._d._task_cost_accumulator,
                        extra={"routing_source": P._d._last_routing_source,
                               "judge_model": f"{j_provider}:{j_model}"})
    return review_response or "", j_provider or "", j_model or ""
