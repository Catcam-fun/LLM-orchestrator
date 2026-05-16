"""Real execution node — Day 3.

Replaces the stubs for execution + build_gate. The build gate, auto-fix loop,
self-review on lint warnings, visual validation, and visual-fix attempt all
live INSIDE this single node since they're tightly coupled iterative work.
The graph treats execution as one phase from a state-machine perspective.

Order of operations (mirrors processTask in ai_dougs.py):
  1. Increment execution_attempts; log human_rejected if a retry
  2. Escalate provider order based on attempts
  3. Setup git branch (if app has a git repo)
  4. Write hook scope (file allow/block enforcement)
  5. Build EXECUTION_PROMPT (with scope block + research gate text)
  6. Call LLM in agent mode → makes file edits
  7. Clear hook scope
  8. Build gate + auto-fix loop (if build_command set)
  9. Self-review pass on lint warnings (if build passed but warnings present)
 10. Visual validation (if app has UI / visual_reference set)
 11. Visual-fix attempt (if visual validation flagged issues)
 12. Auto-commit (if branch + build passed)
 13. Capture diff
 14. Log cost, archive prompt, return state update
"""
from __future__ import annotations
import time
from pathlib import Path

from .. import ported as P
from ..state import TaskState
from ..vault_backup import create_backup, should_backup_for_app_location


def _provider_order():
    return P.loadConfig().get("aiProviderOrder", [{"provider": "claude", "model": "variable"}])


def _send_ai_requests():
    return P.loadConfig().get("sendAiRequests", True)


def _phase_print(state: TaskState, phase_name: str, message: str = ""):
    task = state.get("task_name", "?")
    extra = f"  {message}" if message else ""
    print(f"  [graph] {task}  →  {phase_name}{extra}", flush=True)


def execution(state: TaskState) -> dict:
    """Execute the plan: agent makes file changes, then build/visual gates run."""

    # Short-circuit if upstream phase failed
    if state.get("status") in ("failed", "budget_exceeded"):
        return {"current_phase": "execution", "next_action": "halt"}

    budget_update = P.budget_exceeded_update(state, "execution")
    if budget_update:
        return budget_update

    _phase_print(state, "execution", "starting agent execution")
    nas_root = P.vault_root()
    task_name = state.get("task_name", "?")
    app_location = state.get("app_location", "")
    files_blocked = state.get("files_blocked", "")
    files_allowed = state.get("files_allowed", "")
    build_command = state.get("build_command", "")
    visual_reference = state.get("visual_reference", "")
    research_allowed = bool(state.get("research_allowed", False))

    P.reset_for_phase()
    P.set_current_app_location(app_location)
    app_path = nas_root / app_location if app_location else nas_root

    # Snapshot the time when execution started. Self-test uses this in two
    # ways below to figure out what the agent actually modified:
    #   1. _writes_since(vault_root, _execution_start_iso) parses agent_trace.md
    #      FILE-WRITE entries (catches THIS Claude session's writes only —
    #      subprocess executor writes typically don't trigger the hook).
    #   2. _infra_writes_since_mtime(vault_root, _execution_start_unix) walks
    #      INFRA_PATTERNS dirs and finds files with mtime >= start. AUTHORITATIVE
    #      — works for any CLI agent, no hooks needed, no git tracking needed.
    import time as _time
    from datetime import datetime as _dt
    _execution_start_iso = _dt.now().strftime("%Y-%m-%d %H:%M:%S")
    _execution_start_unix = _time.time()

    # ── Track execution attempts + human rejection signal ────────────────────
    execution_attempts = state.get("execution_attempts", 0) + 1
    if execution_attempts > 1:
        prev_model_field = state.get("execution_model", "")
        if prev_model_field and ":" in prev_model_field:
            prev_provider, prev_model_name = prev_model_field.split(":", 1)
            P._writeCostLog(
                task_name, "execution", "human_rejected",
                {"model": prev_model_name, "passes": 0, "input_tokens": 0,
                 "output_tokens": 0, "cost_usd": 0.0},
                extra={"routing_source": "rejection_signal"},
            )
        print(f"      {P.YELLOW}attempt {execution_attempts} — previous output rejected by human{P.RESET}")
        P._appendFailureMode(
            nas_root, task_name, "execution", "human_rejected",
            f"Previous execution model: {prev_model_field}. Attempt #{execution_attempts}.",
            app_location,
        )

    # ── Escalate provider order on retries ───────────────────────────────────
    base_order = _provider_order()
    exec_provider_order = P._escalateProviderOrder(base_order, execution_attempts)

    # Back up vault infrastructure once per execution attempt before enabling
    # autonomous writes. App-scoped tasks are protected by their build command.
    if should_backup_for_app_location(app_location, nas_root):
        try:
            backup_path = create_backup(nas_root, task_name)
            print(f"      {P.DIM}vault backup: {backup_path.relative_to(nas_root)}{P.RESET}")
        except Exception as e:
            print(f"      {P.YELLOW}vault backup failed (continuing): {e}{P.RESET}")
            P._appendFailureMode(
                nas_root, task_name, "execution", "vault_backup_failed",
                f"Vault backup failed before execution and was treated as non-fatal insurance: {e}",
                app_location,
            )

    # ── Git branch setup ─────────────────────────────────────────────────────
    branch_name, base_branch = (
        P._setupGitBranch(app_path, task_name)
        if app_location and app_path.exists()
        else (None, None)
    )

    # ── Hook scope ────────────────────────────────────────────────────────────
    P._writeHookScope(task_name, files_blocked, files_allowed, app_path, nas_root)

    # ── Build prompt ──────────────────────────────────────────────────────────
    scope_block = P._buildScopeBlock(files_blocked, files_allowed, research_allowed)

    # The EXECUTION_PROMPT expects "full_task" — historically that was the entire
    # markdown task file. With LangGraph state, we synthesize an equivalent: the
    # initial prompt + plan + refinements + human answers, formatted as markdown.
    full_task = _synthesise_task_text(state)
    prompt = P.EXECUTION_PROMPT.format(full_task=full_task, scope_block=scope_block)

    # ── LLM call (agent mode — file access enabled) ──────────────────────────
    response, provider, model = P.runAiRequestWithFallback(
        prompt, nas_root, [app_path, nas_root],
        exec_provider_order, _send_ai_requests(),
        agent_mode=True, phase="execution",
    )
    P._clearHookScope()

    # ── Failure path ──────────────────────────────────────────────────────────
    if not response:
        duration = P._phaseDurationSeconds()
        print(f"      {P.RED}execution LLM call failed{P.RESET}")
        P._writeCostLog(task_name, "execution", "failed", P._d._task_cost_accumulator,
                        extra={"routing_source": P._d._last_routing_source})
        P._appendFailureMode(nas_root, task_name, "execution", "ai_request_failed",
                             "Execution LLM call returned no response", app_location)
        return {
            "current_phase": "execution",
            "execution_attempts": execution_attempts,
            "status": "failed",
            "next_action": "halt",
            "last_failure_type": "ai_request_failed",
            "last_failure_details": "execution LLM returned no response",
            "phase_durations": {**state.get("phase_durations", {}), "execution": duration},
        }

    # ── Build gate + auto-fix loop ───────────────────────────────────────────
    build_status = "no build configured"
    build_error_text = None
    build_passed = True  # default if no build_command

    if build_command:
        build_passed, build_error_text = P._runBuildGate(build_command, nas_root)
        if not build_passed:
            max_passes = P._loadRoutingParams().get("max_autofix_passes", 1)
            for fix_pass in range(1, max_passes + 1):
                print(f"      {P.YELLOW}auto-fix attempt ({fix_pass}/{max_passes}){P.RESET}")
                fix_prompt = (
                    "The build failed after your changes. Fix ONLY the build errors "
                    "shown below — do not change anything else or expand scope.\n\n"
                    f"Build error:\n```\n{build_error_text}\n```"
                )
                P._writeHookScope(task_name, files_blocked, files_allowed, app_path, nas_root)
                fix_resp, _, _ = P.runAiRequestWithFallback(
                    fix_prompt, nas_root, [app_path, nas_root],
                    exec_provider_order, _send_ai_requests(),
                    agent_mode=True, phase="execution",
                )
                P._clearHookScope()
                if fix_resp:
                    response += f"\n\n---\n**Auto-fix attempt {fix_pass}:**\n\n{fix_resp}"
                build_passed, build_error_text = P._runBuildGate(build_command, nas_root)
                if build_passed:
                    break
        build_status = "PASSED" if build_passed else "FAILED"
        if not build_passed:
            P._appendFailureMode(
                nas_root, task_name, "execution", "build_failed_after_autofix",
                f"Build command: `{build_command}`. Error: {(build_error_text or '')[:400]}",
                app_location,
            )

    # ── Self-review pass on lint warnings ────────────────────────────────────
    if build_passed and build_error_text and app_path and app_path.exists():
        lint_warnings = P._filterLintWarnings(build_error_text, app_path)
        if lint_warnings:
            diff_for_review = P._captureDiff(app_path, base_branch) or ""
            scope_note = ""
            if files_allowed:
                scope_note = f"You may only edit files under: {files_allowed}\n\n"
            review_prompt = (
                "You just completed a code execution pass. The build succeeded but "
                "your changes introduced lint warnings that need fixing.\n\n"
                f"{scope_note}"
                "ESLint warnings in files YOU changed:\n"
                f"```\n{lint_warnings[:2000]}\n```\n\n"
                "Your git diff:\n"
                f"```diff\n{diff_for_review[:4000]}\n```\n\n"
                "Fix ONLY these warnings. Do not change anything outside the diff above."
            )
            print(f"      {P.YELLOW}self-review: {len(lint_warnings.splitlines())} lint issue(s){P.RESET}")
            P._writeHookScope(task_name, files_blocked, files_allowed, app_path, nas_root)
            review_resp = P._leanAgentCall(
                review_prompt, nas_root, [app_path, nas_root],
                exec_provider_order, _send_ai_requests(),
            )
            P._clearHookScope()
            if review_resp:
                response += f"\n\n---\n**Self-review (lint fix):**\n\n{review_resp}"
            # Re-run build after self-review
            build_passed, build_error_text = P._runBuildGate(build_command, nas_root)
            build_status = "PASSED (self-review applied)" if build_passed else "FAILED after self-review"

    # ── Visual validation ────────────────────────────────────────────────────
    visual_status = "SKIP"
    visual_notes = ""
    screenshot_path = None
    if build_passed and app_path and app_path.exists():
        try:
            vis_passed, visual_notes, screenshot_path = P._runVisualValidation(
                app_path, task_name, nas_root, visual_reference=visual_reference,
            )
            visual_status = "PASS" if vis_passed else "FAIL"
            if not vis_passed and visual_notes:
                print(f"      {P.YELLOW}visual validation flagged: {visual_notes[:120]}{P.RESET}")
                vis_fix_prompt = (
                    "The build passed but visual validation detected a UI problem. "
                    "Fix the visual issue described below. Do not change logic or scope.\n\n"
                    f"Visual validation notes:\n{visual_notes}"
                )
                P._writeHookScope(task_name, files_blocked, files_allowed, app_path, nas_root)
                vis_fix_resp = P._leanAgentCall(
                    vis_fix_prompt, nas_root, [app_path, nas_root],
                    exec_provider_order, _send_ai_requests(),
                )
                P._clearHookScope()
                if vis_fix_resp:
                    response += f"\n\n---\n**Visual fix attempt:**\n\n{vis_fix_resp}"
                # Re-build + re-validate
                build_passed, build_error_text = P._runBuildGate(build_command, nas_root) if build_command else (True, None)
                if build_passed:
                    vis_passed, visual_notes, screenshot_path = P._runVisualValidation(
                        app_path, task_name, nas_root, visual_reference=visual_reference,
                    )
                    visual_status = "PASS" if vis_passed else "FAIL (visual issues remain)"
        except Exception as e:
            print(f"      {P.YELLOW}visual validation skipped: {e}{P.RESET}")
            visual_status = "SKIP"

    # ── Auto-commit on success so the branch is clean to preview ─────────────
    if build_passed and branch_name:
        try:
            P._autoCommit(app_path, task_name, branch_name)
        except Exception as e:
            print(f"      {P.YELLOW}auto-commit skipped: {e}{P.RESET}")

    # ── Capture diff for the human to review ─────────────────────────────────
    diff_text = ""
    if base_branch:
        try:
            diff_text = P._captureDiff(app_path, base_branch) or ""
        except Exception:
            diff_text = ""

    # ── Self-test trigger (vault infra changes get auto-tested + reverted) ──
    # Also captures the vault-root diff (separate from the app-path diff above)
    # since vault scripts may have been edited even when app_location is set
    self_test_passed = True
    self_test_note = ""
    try:
        from .. import self_test
        # Two complementary path sources:
        # (1) git diffs (catches tracked-file changes — useful for code/<app>)
        # (2) the post_write hook log (catches ALL claude file writes including
        #     untracked vault files; this is our reliable detector of vault
        #     infra changes since most vault files aren't in git).
        vault_diff_text = _capture_vault_diff(nas_root) or ""
        combined_diff = ((diff_text or "") + "\n" + (vault_diff_text or "")).strip()
        # File-write log + mtime walk are both authoritative for "what infra
        # did the agent touch". File-write log catches MY Claude session's
        # edits only; mtime walk catches any subprocess agent's writes too.
        # Both feed in as synthetic diff entries so _diff_paths sees them.
        # Prepend the vault prefix so the path-filter accepts them as in-vault.
        # write_paths = post_write hook log entries = ORCHESTRATOR writes
        # (the orchestrating Claude Code session, not the subprocess agent).
        # Historically these were treated as agent writes — but after the
        # LangGraph migration the agent is a subprocess, not this session.
        # We use them to SUBTRACT from the agent-write set, not inject.
        # mtime_paths = any infra file modified since execution start = agent
        # writes (or orchestrator writes — we'll subtract those next).
        orchestrator_writes = set(self_test._writes_since(nas_root, _execution_start_iso))
        mtime_paths = set(self_test._infra_writes_since_mtime(nas_root, _execution_start_unix))
        # Agent writes = mtime detections minus orchestrator writes
        all_paths = sorted(mtime_paths - orchestrator_writes)
        if all_paths:
            # Compute the prefix to add so the diff-path filter accepts them
            try:
                import subprocess as _sp
                top = _sp.run(
                    ["git", "rev-parse", "--show-toplevel"],
                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                    timeout=5, cwd=str(nas_root),
                )
                if top.returncode == 0 and top.stdout.strip():
                    git_root = Path(top.stdout.strip()).resolve()
                    rel = nas_root.resolve().relative_to(git_root)
                    prefix = "" if rel == Path(".") else rel.as_posix() + "/"
                else:
                    prefix = ""
            except Exception:
                prefix = ""
            synthetic = "\n".join(f"diff --git a/{prefix}{p} b/{prefix}{p}" for p in all_paths)
            combined_diff = (combined_diff + "\n" + synthetic).strip()
            # Bug S fix (2026-05-11): the synthetic lines above are path
            # DECLARATIONS only — no content. For vault-infra tasks where
            # `git diff` doesn't surface the changes (untracked files,
            # vault not always a git repo historically), extract pre-execution
            # content from the .backups tarball and unified-diff against the
            # current files. Judge then sees real before/after content,
            # not just "file X was touched".
            try:
                from ..content_diff import compute_content_diff_from_backup
                content_diff = compute_content_diff_from_backup(
                    nas_root, task_name, all_paths)
                if content_diff:
                    combined_diff = (combined_diff + "\n\n" + content_diff).strip()
            except Exception as e:
                print(f"      {P.YELLOW}content_diff errored (non-fatal): {e}{P.RESET}")
        self_test_passed, self_test_note = self_test.run_self_test(
            nas_root, combined_diff, task_name, app_location,
            orchestrator_writes=orchestrator_writes,
        )
        if not self_test_passed:
            print(f"      {P.RED}self-test failed: {self_test_note}{P.RESET}")
            build_passed = False
            build_status = f"FAILED — self-test: {self_test_note}"
    except Exception as e:
        print(f"      {P.YELLOW}self-test errored (non-fatal): {e}{P.RESET}")
        self_test_note = f"errored: {e}"

    # ── Cross-model execution judge (runs unconditionally per quality hierarchy) ──
    judge_text = ""
    judge_provider = ""
    judge_model = ""
    if build_passed and self_test_passed:
        try:
            from .. import execution_judge
            if execution_judge.should_run_execution_judge():
                # Bug L fix (2026-05-09): the judge was only getting `diff_text`
                # (the app-path git diff). For vault-infra refactor tasks where
                # the vault isn't a git repo or app_location is empty, that diff
                # is empty — and the judge then falsely concludes "no work
                # happened" with REJECT verdict (auto_0066's 1.8/5 false
                # rejection was the canonical case). Use the combined diff
                # which includes vault_diff_text (hook log + mtime walk) — the
                # authoritative source for vault-file changes regardless of git.
                judge_diff = (locals().get("combined_diff") or diff_text
                              or locals().get("vault_diff_text") or "")
                judge_text, judge_provider, judge_model = execution_judge.run_execution_judge(
                    state.get("initial_prompt", ""),
                    state.get("plan_text", ""),
                    response,
                    judge_diff,
                    nas_root, exec_provider_order, _send_ai_requests(),
                    provider, model, task_name,
                    refinements=state.get("refinements") or [],
                    app_location=state.get("app_location", ""),
                )
                if judge_text:
                    print(f"      {P.GREEN}execution judge complete{P.RESET}  "
                          f"{P.DIM}{judge_provider}:{judge_model}{P.RESET}")
            else:
                print(f"      {P.DIM}execution judge skipped (kill-switch disabled){P.RESET}")
        except Exception as e:
            print(f"      {P.YELLOW}execution judge errored (non-fatal): {e}{P.RESET}")

    # Parse the judge verdict for the completion gate (if judge_text exists)
    judge_verdict = {}
    if judge_text:
        try:
            from .. import execution_judge
            judge_verdict = execution_judge.parse_judge_verdict(judge_text)
            verdict = judge_verdict.get("verdict") or "?"
            score = judge_verdict.get("score_avg", 0)
            n_concerns = len(judge_verdict.get("concerns", []))
            color = (P.GREEN if verdict == "APPROVE"
                     else (P.YELLOW if verdict == "NEEDS_REVISION"
                           else P.RED if verdict == "REJECT" else P.DIM))
            print(f"      {color}judge verdict: {verdict} (avg {score}/5, "
                  f"{n_concerns} concern(s)){P.RESET}")
        except Exception as e:
            print(f"      {P.YELLOW}judge verdict parse errored (non-fatal): {e}{P.RESET}")

    # ── Logging + state update ───────────────────────────────────────────────
    duration = P._phaseDurationSeconds()
    P._archivePromptResponse(nas_root, task_name, "execution", prompt, response, provider, model)
    P._writeCostLog(
        task_name, "execution", "success", P._d._task_cost_accumulator,
        extra={
            "routing_source": P._d._last_routing_source,
            "visual_validation": visual_status,
            "screenshot": str(screenshot_path) if screenshot_path else None,
            "build_status": build_status,
        },
    )
    P._logContextInjection(task_name, "execution")

    cost_snapshot = P.cost_accumulator_snapshot()
    new_cumulative = state.get("existing_cost_usd", 0.0) + cost_snapshot.get("cost_usd", 0.0)
    new_execution_cost = state.get("execution_cost_usd", 0.0) + cost_snapshot.get("cost_usd", 0.0)
    budget_update = P.phase_budget_exceeded_update(state, "execution", new_execution_cost) or {}

    print(f"      {P.GREEN}execution complete{P.RESET}  "
          f"{P.DIM}{provider}:{model} | build={build_status} | visual={visual_status} | "
          f"${cost_snapshot.get('cost_usd', 0):.4f} | {duration:.1f}s{P.RESET}")

    # ── VERIFICATION + ITERATION LOOP ──────────────────────────────────────
    # The verification YAML block from planning runs after execution. If it
    # fails AND verification_policy.max_iterations allows, the agent gets a
    # follow-up "fix" prompt with the failed-check details. Loop until pass
    # or iterations exhausted. This is the "self-correcting" half of the
    # vision (the user explicitly called this out 2026-05-08: agents should
    # check their work and fix it, not just be one-shot graded).
    verification_outcome = {}
    try:
        from ..verification import (verify_plan, print_outcome,
                                     extract_verification_block,
                                     run_verification_block,
                                     has_any_behavioral_check,
                                     extract_verification_blocks_from_refinements)
        plan_text = state.get("plan_text", "") or ""
        if plan_text:
            # Get max_iterations from policy (default 1 = no iteration / one-shot)
            checks_for_policy, policy, _err = extract_verification_block(plan_text)
            max_iter = int((policy or {}).get("max_iterations", 1) or 1)
            max_iter = max(1, min(max_iter, 3))  # safety cap: 1..3

            # ── Behavioral classifier Phase 2 (2026-05-09) ──
            # Per the planner-prompt requirement (every verification block must
            # include ≥1 behavioral check), check whether the parsed block
            # actually has behavioral coverage. If not, log a warning and
            # surface a `verification_structural_only` symptom for the
            # supervisor to pick up. Don't BLOCK — the human still owns
            # whether to ship a structural-only task — but make the gap
            # visible. See VISION.md "Safety through observation" + Bug E
            # DESC PRESERVE for why this matters.
            try:
                _refinement_extras = extract_verification_blocks_from_refinements(
                    state.get("refinements") or []
                )
                _all_checks = (checks_for_policy or []) + (_refinement_extras or [])
                if _all_checks and not has_any_behavioral_check(_all_checks):
                    print(f"      {P.YELLOW}⚠ verification block is STRUCTURAL-ONLY "
                          f"(no behavioral check detected). Bug E DESC PRESERVE-class "
                          f"failures could pass undetected. Consider adding a check "
                          f"that exercises the actual code path end-to-end.{P.RESET}")
                    # Append an observation to logs/events.jsonl for the
                    # diagnostics CLI to surface.
                    try:
                        from datetime import datetime as _dt
                        log_path = nas_root / "logs" / "events.jsonl"
                        log_path.parent.mkdir(parents=True, exist_ok=True)
                        import json as _json
                        entry = {
                            "timestamp": _dt.now().isoformat(timespec="seconds"),
                            "event": "verification_structural_only",
                            "task": task_name,
                            "n_checks": len(_all_checks),
                            "details": (
                                f"task {task_name!r} verification block has "
                                f"{len(_all_checks)} check(s), 0 classified as "
                                f"behavioral. Plan author / refinement should add a "
                                f"check that exercises real code (call a function, "
                                f"mutate state, read back, assert)."
                            ),
                        }
                        with open(log_path, "a", encoding="utf-8") as _f:
                            _f.write(_json.dumps(entry) + "\n")
                    except Exception:
                        pass
            except Exception:
                pass  # heuristic must never break execution

            iteration = 0
            # Bug Z fix (2026-05-11): verification commands need to run in
            # the project's working directory when app_location is set, so
            # that paths like `src/styles/tokens.css` resolve correctly.
            # Without this, external-project tasks (e.g. warcraftlogs-tracker)
            # had verification fail 20/20 because every file path was
            # interpreted relative to vault root, not project root.
            verify_cwd = app_path if app_location else nas_root
            outcome = verify_plan(plan_text, cwd=verify_cwd, task_id=task_name,
                                  refinements=state.get("refinements") or [])
            print_outcome(outcome)
            try:
                P._writeCostLog(
                    task_name, "verification",
                    "success" if outcome.all_passed else "failed",
                    {"passes": 1, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0},
                    extra={
                        "verification_status": "passed" if outcome.all_passed else "failed",
                        "verification_n_checks": outcome.n_checks,
                        "verification_n_failed": outcome.n_failed,
                        "verification_parse_error": outcome.parse_error,
                        "iteration": iteration,
                    },
                )
            except Exception:
                pass

            # Iteration loop: if verification failed AND policy allows retries,
            # hand the failures back to the agent + re-execute + re-verify.
            while not outcome.all_passed and iteration < (max_iter - 1):
                iteration += 1
                print(f"      {P.YELLOW}[iteration {iteration}/{max_iter-1}] "
                      f"feeding failures back to agent for self-correction...{P.RESET}")
                # Build the fix prompt from the failed checks
                failed_summary_lines = []
                for r in outcome.failed:
                    snippet = (r.output[:600] if r.output else "").rstrip()
                    failed_summary_lines.append(
                        f"- check `{r.id}` ({r.type}): {r.detail}"
                        + (f"\n  output:\n  {snippet}" if snippet else "")
                    )
                # Supervisor symptom injection removed 2026-05-12 with the
                # supervisor rewrite (symptom rules retired; supervisor is
                # now just a watchdog that tree-kills wedged subprocs).
                fix_prompt = (
                    f"{prompt}\n\n"
                    f"---\n\n"
                    f"## Verification iteration {iteration}/{max_iter-1}\n\n"
                    f"Your previous execution attempt completed but the "
                    f"verification block did not pass:\n\n"
                    + "\n".join(failed_summary_lines)
                    + "\n\n"
                    + f"Two failure modes are possible:\n"
                    f"1. The work itself is wrong / incomplete — fix the work.\n"
                    f"2. The verification BLOCK in the plan has a bug (e.g. "
                    f"non-portable shell syntax, wrong path, broken assertion). "
                    f"In that case, edit the plan's verification YAML to fix "
                    f"it, then make sure the underlying work meets the corrected check.\n\n"
                    f"Verification commands run via `subprocess.run(shell=True)` — "
                    f"that's `cmd.exe` on Windows. Don't use PowerShell-only "
                    f"syntax (Get-ChildItem, @'...'@ heredoc, etc). Prefer "
                    f"inline `python -c '...'` for cross-platform checks.\n\n"
                    f"Apply your fix now."
                )
                # Re-execute via the same provider/model as the original.
                # Bug K fix (2026-05-09): the previous code referenced an
                # undefined variable in the add-dirs argument here, which
                # crashed the iteration loop with NameError every time it
                # tried to fire. Replicate the same call shape used by the
                # initial execution call above (line 142): cwd=nas_root,
                # add-dirs=[app_path, nas_root]. This is what the agent saw
                # on its first pass; iteration must use the same setup or
                # scope hooks won't apply consistently.
                P._writeHookScope(task_name, files_blocked, files_allowed, app_path, nas_root)
                fix_response, fix_provider, fix_model = P.runAiRequestWithFallback(
                    fix_prompt, nas_root, [app_path, nas_root],
                    exec_provider_order, _send_ai_requests(),
                    agent_mode=True, phase="execution",
                )
                P._clearHookScope()
                if not fix_response:
                    print(f"      {P.RED}iteration {iteration} got no response from agent{P.RESET}")
                    break
                # Capture new diff (since BEFORE the iteration started would be
                # ideal, but cheap version: capture again from execution start)
                from .. import vault_backup as _vb
                # Re-verify — Bug Z fix: same cwd logic as initial call
                outcome = verify_plan(plan_text, cwd=verify_cwd, task_id=task_name,
                                  refinements=state.get("refinements") or [])
                print_outcome(outcome)
                try:
                    P._writeCostLog(
                        task_name, "verification",
                        "success" if outcome.all_passed else "failed",
                        {"passes": 1, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0},
                        extra={
                            "verification_status": "passed" if outcome.all_passed else "failed",
                            "verification_n_checks": outcome.n_checks,
                            "verification_n_failed": outcome.n_failed,
                            "iteration": iteration,
                        },
                    )
                except Exception:
                    pass

            verification_outcome = outcome.to_dict()
            verification_outcome["iterations_used"] = iteration
            verification_outcome["max_iterations"] = max_iter
    except Exception as e:
        print(f"      {P.YELLOW}verification runner errored (non-fatal): {e}{P.RESET}")

    # 2026-05-12: judge is an advisor. Whatever verdict it returned, route
    # to wait_human_review — the concerns and scores live on judge_verdict
    # and the user decides at the gate. (Auto route-back removed; previously
    # NEEDS_REVISION sent the task back to refinement automatically.)
    next_action = "wait_human_review"
    new_status: str = "pending_human_review"

    out: dict = {
        "current_phase": "execution",
        "execution_log": response,
        "execution_diff": diff_text,
        "execution_model": f"{provider}:{model}" if provider and model else "",
        "execution_attempts": execution_attempts,
        "build_status": build_status,
        "build_error": (build_error_text or "")[:1000] if not build_passed else "",
        "visual_status": visual_status,
        "execution_judge_text": judge_text,
        "execution_judge_model": f"{judge_provider}:{judge_model}" if judge_provider and judge_model else "",
        "judge_verdict": judge_verdict,
        "verification_outcome": verification_outcome,
        "cost_accumulator": cost_snapshot,
        "existing_cost_usd": new_cumulative,
        "execution_cost_usd": new_execution_cost,
        "last_routing_source": P._d._last_routing_source,
        "phase_durations": {**state.get("phase_durations", {}), "execution": duration},
        "next_action": next_action,
        "status": new_status,
        **budget_update,
    }
    # Bug T fix (2026-05-11): write status + judge verdict back to disk so
    # Obsidian sees the gate (or the judge-driven refinement route-back).
    try:
        from ..frontmatter_sync import sync_task_file_frontmatter
        sync_task_file_frontmatter({**state, **out})
    except Exception:
        pass
    return out


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _capture_vault_diff(vault_root) -> str:
    """Get a git diff covering changes inside the vault root.

    Distinct from the app-path diff in the main flow — the agent may have
    edited vault scripts (which is what triggers self-test). Returns empty
    string if vault isn't a git repo or there are no changes.

    CRITICAL: uses `--relative` so the diff paths are relative to vault_root
    rather than the git repo's actual root. Without this, when the vault sits
    inside a parent git repo (e.g. C:/Users/gigga/), git emits paths like
    'Documents/wclapp/vault/scripts/vault_graph/cli.py' — which never matches
    INFRA_PATTERNS in self_test.py (those expect 'scripts/vault_graph/...').
    Result: self-test silently said "no infra touched" on every infra change.
    Bug discovered + fixed 2026-05-04 mid-run.
    """
    import subprocess as _sp
    try:
        # Check if vault is a git repo
        check = _sp.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5, cwd=str(vault_root),
        )
        if check.returncode != 0:
            return ""
        # `--relative` rewrites diff paths to be relative to cwd (vault_root)
        # so they match INFRA_PATTERNS like 'scripts/vault_graph/'.
        result = _sp.run(
            ["git", "diff", "--relative", "HEAD"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15, cwd=str(vault_root),
        )
        # Guard against stdout being None (e.g. encoding errors before our
        # utf-8 patch, or unusual subprocess states). str() coerces, so the
        # downstream `str + str` concat in run_self_test never sees None.
        if result.returncode == 0 and result.stdout is not None:
            return result.stdout
        return ""
    except Exception:
        return ""


def _synthesise_task_text(state: TaskState) -> str:
    """Build the markdown blob the EXECUTION_PROMPT expects as `full_task`.

    Historically this was the entire task .md file. We synthesise an equivalent
    from TaskState fields so the agent gets the same context.
    """
    parts = [f"# {state.get('task_name', 'task')}", ""]

    if state.get("app_location"):
        parts.append(f"**App location:** {state['app_location']}")
    if state.get("build_command"):
        parts.append(f"**Build command:** `{state['build_command']}`")
    if state.get("files_allowed"):
        parts.append(f"**Files allowed:** {state['files_allowed']}")
    if state.get("files_blocked"):
        parts.append(f"**Files blocked:** {state['files_blocked']}")
    parts.append("")

    if state.get("initial_prompt"):
        parts += ["## Initial Prompt", "", state["initial_prompt"], ""]

    if state.get("plan_text"):
        parts += ["## Plan", "", state["plan_text"], ""]

    if state.get("plan_review_text"):
        parts += [
            f"## Plan Review (independent — {state.get('plan_review_provider','?')}:{state.get('plan_review_model','?')})",
            "",
            state["plan_review_text"],
            "",
        ]

    if state.get("human_answers"):
        parts += ["## Human Answers", "", state["human_answers"], ""]

    refinements = state.get("refinements", [])
    for i, ref in enumerate(refinements, start=1):
        parts += [f"## Plan refinement {i}", "", ref, ""]

    if state.get("human_approval"):
        parts += [f"## Approval: {state['human_approval']}", ""]

    return "\n".join(parts)
