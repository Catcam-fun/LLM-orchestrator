"""Interrupt-point + terminal nodes for the LangGraph pipeline.

Live functions (wired into the compiled graph in graph.py):
  - wait_human_answers / wait_human_approval / wait_human_review:
      interrupt-point nodes. The graph halts BEFORE these via
      `interrupt_before` in compile_graph(); the function body runs only
      when the human resumes (via `vault.py advance`).
  - terminal_complete:
      final node. Gates status=completed on verification_outcome.all_passed
      AND not judge_verdict.REJECT. Writes failure_modes entries on failure,
      records the verification block to the regression suite on success,
      refreshes STATUS.md, syncs frontmatter to disk.

Live helpers:
  - _write_sharpener_outcome_entry: writes the sharpener Phase 1 outcome
    cost-log entry so future routing can join sharpener (provider, model)
    to downstream task quality.
  - _write_verification_failure: structured failure_modes entry with
    constraint_added field.
  - _is_test_fixture_task: filters audit fixtures from real-task code paths.

Real phase implementations live in `planning.py` (task_entry, planning,
plan_review), `refinement.py` (refinement, learning), and `execution.py`
(execution). The legacy Day-1 stub functions for those phases were
removed 2026-05-12; only interrupt + terminal nodes remain here.
"""
from datetime import datetime
from pathlib import Path
import time

from .. import project_memory
from ..state import TaskState


def _phase_print(state: TaskState, phase_name: str, message: str = ""):
    """Consistent log line for each node activation."""
    task = state.get("task_name", "?")
    extra = f"  {message}" if message else ""
    print(f"  [graph] {task}  →  {phase_name}{extra}", flush=True)


# ─── task_entry — REAL even on Day 1 (lightweight checks) ────────────────────

def task_entry(state: TaskState) -> dict:
    """Pre-flight: log task entry, start phase timer, mark ready for planning.

    The task-file linting happens in cli.py before the graph runs; cost
    ceilings are observation-only (see ported.phase_budget_exceeded_update).
    This node just sets up timing and routes onward.
    """
    _phase_print(state, "task_entry")
    return {
        "current_phase": "task_entry",
        "phase_start_time": time.monotonic(),
        "next_action": "planning",
    }


# ─── wait_human_answers — interrupt point ────────────────────────────────────

def wait_human_answers(state: TaskState) -> dict:
    """Halts the graph until human runs `vault.py advance <task>`.

    The interrupt is configured at graph-compile time via interrupt_before.
    This node only runs when the human resumes — at which point state will
    contain `human_answers` populated by the CLI.
    """
    _phase_print(state, "wait_human_answers", "human resumed — proceeding to refinement")
    return {
        "current_phase": "refinement",
        "status": "pending_ai_refinement",
        "next_action": "refinement",
    }


# ─── wait_human_approval — interrupt point ───────────────────────────────────

def wait_human_approval(state: TaskState) -> dict:
    """Halts until human approves and triggers execution."""
    _phase_print(state, "wait_human_approval", "human resumed — proceeding to execution")
    return {
        "current_phase": "execution",
        "status": "pending_ai_execution",
        "next_action": "execution",
    }


# ─── wait_human_review — interrupt point ─────────────────────────────────────

def wait_human_review(state: TaskState) -> dict:
    """Halts until human reviews execution output."""
    _phase_print(state, "wait_human_review", "human resumed — proceeding to learning")
    return {
        "current_phase": "learning",
        "status": "pending_ai_learning",
        "next_action": "learning",
    }


# ─── terminal_complete — graph end ───────────────────────────────────────────

def terminal_complete(state: TaskState) -> dict:
    """Final node — marks task complete IFF verification passed, else fails.

    Closes the plan→execute→measure→learn loop. Step 3 (measure) is the
    `verification_outcome` field in state, populated by the execution node
    after running the planning phase's `verification:` block.

    Three outcomes:
      - verification all_passed  → status=completed (the original behavior)
      - verification any failed  → status=failed, write failure_modes entry
      - verification missing     → status=failed, parse_error reported

    The "verification block missing" case is a hard failure on purpose: the
    planning prompt now requires it, so missing means the plan was malformed
    and the task cannot be allowed to silently complete.

    Refreshes STATUS.md regardless of outcome so defense events stay visible.
    """
    outcome = state.get("verification_outcome") or {}
    all_passed = outcome.get("all_passed")
    parse_error = outcome.get("parse_error")
    n_checks = outcome.get("n_checks", 0)
    n_failed = outcome.get("n_failed", 0)

    # Cross-model judge verdict (defense-in-depth gate, see Cluster B.#6).
    # The judge sees plan-vs-diff intent — it can catch "verification passed
    # but the agent built the wrong thing". REJECT blocks completion even if
    # the verification block passed.
    judge_verdict = state.get("judge_verdict") or {}
    judge_v = judge_verdict.get("verdict", "")  # "" / APPROVE / NEEDS_REVISION / REJECT
    judge_score = judge_verdict.get("score_avg", 0.0)
    judge_concerns = judge_verdict.get("concerns", [])

    judge_blocks = (judge_v == "REJECT")

    if all_passed is True and n_checks > 0 and not judge_blocks:
        _phase_print(state, "terminal_complete", "task done (verification passed)")
        new_status = "completed"
        new_phase = "completed"
        # Regression-block recording removed 2026-05-12. The self-extending
        # regression suite caught zero real bugs in practice — every failure
        # was an intentional behavior change that required manually disabling
        # the stale block. Net friction without net catch. state/regression_blocks.yaml
        # is kept as a historical artifact but no new entries get added.
    else:
        # Failure path
        if judge_blocks:
            reason = (f"cross-model judge REJECTED (avg {judge_score}/5, "
                      f"{len(judge_concerns)} concern(s))")
        elif parse_error:
            reason = f"plan missing verification block ({parse_error})"
        elif n_checks == 0:
            reason = "verification block had zero checks"
        else:
            reason = f"{n_failed}/{n_checks} verification check(s) failed"
        _phase_print(state, "terminal_complete",
                     f"task FAILED — {reason}")
        new_status = "failed"
        new_phase = "verification_failed" if not judge_blocks else "judge_rejected"
        # Write failure_modes entry — REQUIRED constraint_added field
        try:
            _write_verification_failure(state, outcome, reason)
        except Exception:
            pass

    try:
        if not _is_test_fixture_task(state.get("task_name", "?")):
            project_memory.append_project_task_log(
                state.get("app_location", ""),
                state,
                vault_root=Path(__file__).resolve().parent.parent.parent.parent,
                outcome=new_status,
            )
    except Exception:
        pass

    # ── Sharpener downstream-quality propagation (2026-05-09) ──
    # Write back the task's quality signals to a new cost-log entry so
    # sharpener routing can learn which sharpener (provider, model) produces
    # prompts that lead to successful tasks — not just prompts the human
    # accepted at staging. The link entry was written by routeDougs() at
    # routing time with phase=sharpener_route. This phase=sharpener_outcome
    # entry closes the loop. See PLAN.md "Sharpener routing — downstream-
    # quality signal" item.
    try:
        if not _is_test_fixture_task(state.get("task_name", "?")):
            _write_sharpener_outcome_entry(state, new_status, outcome, judge_verdict)
    except Exception:
        pass

    # STATUS.md regeneration removed 2026-05-12. STATUS.md was a gitignored
    # auto-generated dashboard neither user nor orchestrator read; the
    # `vault.py diagnostics` CLI replaces it. docs_refresh.py is retained only
    # for _detect_skill_quality_issues (used by prune-skills).

    # Bug T fix (2026-05-11): final status + verdict write-back so Obsidian
    # reflects completed / failed tasks accurately.
    try:
        from ..frontmatter_sync import sync_task_file_frontmatter
        sync_task_file_frontmatter({**state, "current_phase": new_phase, "status": new_status})
    except Exception:
        pass

    return {
        "current_phase": new_phase,
        "status": new_status,
    }


def _is_test_fixture_task(task_name: str) -> bool:
    """True if task_name is a synthetic audit / e2e fixture, NOT a real task.

    Such fixtures run through terminal_complete during the audit suite and
    repeatedly trigger _write_verification_failure (since they have no real
    plan + no verification block). Without this guard, every audit run
    appends 1+ noise entries to failure_modes.md.
    """
    return any(task_name.startswith(p) for p in (
        "audit_dummy", "audit_test", "e2e_test_", "synthetic-",
    ))


def _write_sharpener_outcome_entry(state: TaskState, new_status: str,
                                    outcome, judge_verdict: dict) -> None:
    """Write a phase=sharpener_outcome cost-log entry tying task quality to sharpener.

    Lookup logic: read the task file's frontmatter to get the
    sharpener_model that produced the originating prompt. If absent (older
    tasks, or sharpener didn't record it), no entry is written. The entry
    pairs (sharpener_provider, sharpener_model) with the downstream task's
    quality signals (verification_passed, judge_verdict, judge_score_avg)
    so future routing analytics can join sharpener_route + sharpener_outcome
    by task_id and rank sharpener choices by downstream task quality.

    See PLAN.md "Sharpener routing — downstream-quality signal" item.
    """
    task_name = state.get("task_name", "")
    if not task_name:
        return
    task_file_path = state.get("task_file_path", "")
    if not task_file_path:
        return
    try:
        from pathlib import Path as _P
        text = _P(task_file_path).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return
    # Extract sharpener_model from frontmatter
    sharpener_model = ""
    for line in text.splitlines()[:30]:  # frontmatter is at the top
        ls = line.strip()
        if ls.startswith("sharpener_model:"):
            sharpener_model = ls.split(":", 1)[1].strip()
            break
    if not sharpener_model or ":" not in sharpener_model:
        return  # task wasn't routed via sharpener (older task) or model unrecorded
    provider, model = sharpener_model.split(":", 1)

    verification_passed = bool(getattr(outcome, "all_passed", False))
    judge_data = judge_verdict if isinstance(judge_verdict, dict) else {}
    entry = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "task": task_name,
        "phase": "sharpener_outcome",
        "status": "success" if (new_status == "completed" and verification_passed) else "failed",
        "provider": provider,
        "model": model,
        "passes": 1,
        "input_tokens": 0,
        "output_tokens": 0,
        "cost_usd": 0.0,
        "estimated": False,
        "downstream_task_id": task_name,
        "downstream_task_status": new_status,
        "verification_passed": verification_passed,
        "judge_verdict": judge_data.get("verdict", ""),
        "judge_score_avg": judge_data.get("score_avg", 0.0),
    }
    try:
        log_path = _P(__file__).resolve().parent.parent.parent.parent / "logs" / "cost_log.jsonl"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


# Imports for the helper above (kept local to minimize risk of import order issues)
from datetime import datetime
import json


def _write_verification_failure(state: TaskState, outcome: dict, reason: str) -> None:
    """Append a structured failure_modes entry. Per the iteration discipline
    (harness research 2026-05-07), the entry includes a constraint_added
    field. For verification failures the constraint is the verification block
    itself — future tasks will be checked against the same gate, so the
    failure is preventable in the same way again.

    Synthetic audit / e2e fixture tasks are skipped — see
    `_is_test_fixture_task`. They trip the same code path repeatedly during
    audit runs and have no real signal to record.
    """
    from datetime import datetime
    from pathlib import Path
    task = state.get("task_name", "?")
    if _is_test_fixture_task(task):
        return
    root = Path(__file__).resolve().parent.parent.parent.parent
    fm = root / "ai_context" / "failure_modes.md"
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    failed = outcome.get("results", []) or []
    failed_summary = "; ".join(
        f"{r.get('id','?')}: {r.get('detail','?')}"
        for r in failed if not r.get("passed")
    )[:600]
    block = (
        f"\n## {ts} — {task} / verification\n\n"
        "- **Type**: verification_block_failed\n"
        "- **Where**: `scripts/vault_graph/verification.py` (gate)\n"
        "- **Source**: automated (terminal_complete refused completion)\n"
        f"- **Details**: {reason}. Failed checks: {failed_summary or 'n/a'}\n"
        "- **constraint_added**: The verification: YAML block in the plan IS "
        "the constraint. Same check will block any future task that makes "
        "the same promise. To prevent recurrence, refine the planning prompt "
        "to surface what kind of verification block this task should have "
        "produced.\n\n---\n"
    )
    try:
        with open(fm, "a", encoding="utf-8") as f:
            f.write(block)
    except OSError:
        pass
