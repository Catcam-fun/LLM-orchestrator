"""TaskState — the schema flowing through every node in the graph.

This is the single source of truth for a task's runtime state. Everything a
node needs to read, and everything a node produces, lives here.

Design notes:
- Use TypedDict (not Pydantic) for now — simpler, fewer deps, lighter.
- Lists/dicts are *replaced* on update, not merged. If a node wants to append
  to history, it returns the full new list. (LangGraph's default reducer.)
- Field names mirror what already exists in cost_log.jsonl, failure_modes.md,
  prompt_archive — so the data shape is preserved across the migration.
"""
from __future__ import annotations

from typing import TypedDict, Literal


# Phases match the existing pipeline phase names in cost_log.jsonl
Phase = Literal[
    "task_entry", "planning", "plan_review",
    "refinement", "execution", "build_gate",
    "learning", "completed",
]

# Status mirrors today's markdown frontmatter values for human readability +
# interop with anything still reading task files
Status = Literal[
    "new",
    "pending_ai_planning", "pending_human_answers",
    "pending_ai_refinement", "pending_human_approval",
    "pending_ai_execution", "pending_human_review",
    "pending_ai_learning", "completed",
    "failed", "budget_exceeded",
]


class TaskState(TypedDict, total=False):
    """All state flowing through the graph for a single task.

    `total=False` means every field is optional — nodes only update what
    they care about. LangGraph merges updates into the running state.
    """

    # ── Identity ──────────────────────────────────────────────────────────────
    task_name: str                  # e.g. "auto_0001"
    task_file_path: str             # absolute path to source markdown spec
    app_location: str               # e.g. "code/vault-ui"
    created_at: str                 # ISO timestamp

    # ── Inputs (from the markdown task file) ──────────────────────────────────
    initial_prompt: str
    context: str                    # injected per-task (skills, failure history, digest)
    files_allowed: str              # comma-separated paths or globs
    files_blocked: str
    build_command: str
    visual_reference: str           # screenshot path for visual validation
    research_allowed: bool
    cost_ceiling: float             # single per-task cost ceiling (2026-05-12)
    max_cost_usd: float | None      # deprecated fallback ceiling for old tasks

    # ── Status / phase tracking ──────────────────────────────────────────────
    status: Status
    current_phase: Phase
    next_action: str                # routing hint for conditional edges

    # ── Outputs from each phase ──────────────────────────────────────────────
    plan_text: str
    plan_review_text: str
    plan_review_provider: str
    plan_review_model: str
    execution_judge_text: str
    execution_judge_model: str
    refinements: list[str]           # list of refinement texts (one per round)
    refinement_n: int
    human_answers: str
    human_approval: str              # "approved" / "needs_revision"
    execution_log: str
    execution_diff: str              # git diff captured after execution
    build_status: str                # "PASSED" / "FAILED" / "no build"
    build_error: str
    visual_status: str               # "PASS" / "FAIL" / "SKIP"
    learning_output: str
    human_feedback: str

    # ── Cost + time tracking ─────────────────────────────────────────────────
    cost_accumulator: dict[str, int | float | str]  # {passes, input_tokens, output_tokens, cost_usd, model}
    existing_cost_usd: float         # cumulative across runs of this task
    planning_cost_usd: float         # observed spend in planning/refinement/plan_review
    execution_cost_usd: float        # observed spend in execution
    learning_cost_usd: float         # observed spend in learning
    phase_start_time: float          # monotonic time, for duration calc
    phase_durations: dict[str, float]  # {phase_name: seconds}

    # ── Routing + provider tracking ──────────────────────────────────────────
    last_routing_source: str         # "explore" / "history" / "default" / "rejection_signal"
    planning_model: str              # "provider:model" — what produced the plan
    execution_model: str             # what produced execution
    execution_attempts: int          # increment on retries; drives escalation

    # ── Skills tracking (for effectiveness loop) ─────────────────────────────
    loaded_skills: list[str]         # skill names auto-injected into planning context

    # ── Failure / recovery ───────────────────────────────────────────────────
    last_failure_type: str
    last_failure_details: str

    # ── Verification (closing the plan→execute→measure→learn loop) ──────────
    # Populated by execution node after running the planning phase's
    # `verification:` YAML block. Shape mirrors VerificationOutcome.to_dict():
    #   {all_passed: bool, n_checks: int, n_passed: int, n_failed: int,
    #    results: [{id, type, passed, duration_sec, detail, output}, ...],
    #    parse_error: str | None}
    # terminal_complete reads this to gate completion.
    verification_outcome: dict

    # ── Cross-model judge verdict (advisor, surfaces at human gate) ─────────
    # Parsed from execution_judge_text by parse_judge_verdict(). Shape:
    #   {verdict: "APPROVE"|"NEEDS_REVISION"|"REJECT"|"", scores: {...},
    #    concerns: [str], score_avg: float}
    # 2026-05-12: judge is an advisor. Its concerns + scores surface at
    # wait_human_review alongside the diff; the user decides whether to
    # merge, refine, or reject. terminal_complete still gates on REJECT
    # as a hard stop, but NEEDS_REVISION no longer auto-routes back.
    judge_verdict: dict

    # judge_revision_attempts (auto route-back counter) removed 2026-05-12
    # with the judge-as-advisor redesign.

    # ── Plan-review verdict (GAP #4 fix, 2026-05-11) ────────────────────────
    # Parsed from plan_review_text by parse_plan_review_verdict(). Shape mirrors
    # judge_verdict so refinement.py can read one canonical concerns/scores
    # structure regardless of which judge produced it. Plan review fires BEFORE
    # any execution, so REJECT here doesn't block completion; instead the
    # verdict's concerns get injected into the refinement prompt the same way
    # execution_judge concerns do — both judges feed concerns into the same
    # refinement node, achieving arbitration parity.
    plan_review_verdict: dict

    # Prompt-variant A/B (GAP #5) removed 2026-05-12.

    # ── Rerun audit metadata ────────────────────────────────────────────────
    rerun_active: bool
    rerun_from_phase: str
    rerun_source_checkpoint: str
    rerun_started_at: str
    rerun_log: list[dict[str, str]]

    # ── Output appendage for the markdown sync layer ─────────────────────────
    # Whenever a node produces human-readable output (plan, execution log, etc.)
    # the sync layer writes it back to the markdown task file so the human can
    # still read everything in Obsidian. Sync is optional — runtime state lives
    # in sqlite regardless.
    sync_pending: list[str]          # phases whose output hasn't been mirrored yet


def initial_state(task_name: str, task_file_path: str = "", initial_prompt: str = "", **kwargs: object) -> TaskState:
    """Create a fresh TaskState with sensible defaults."""
    from datetime import datetime
    base: TaskState = {
        "task_name": task_name,
        "task_file_path": task_file_path,
        "initial_prompt": initial_prompt,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "status": "pending_ai_planning",
        "current_phase": "task_entry",
        "next_action": "begin_planning",
        "refinements": [],
        "refinement_n": 0,
        "execution_attempts": 0,
        "existing_cost_usd": 0.0,
        "planning_cost_usd": 0.0,
        "execution_cost_usd": 0.0,
        "learning_cost_usd": 0.0,
        "cost_accumulator": {
            "passes": 0, "input_tokens": 0, "output_tokens": 0,
            "cost_usd": 0.0, "model": "",
        },
        "phase_durations": {},
        "sync_pending": [],
        "rerun_active": False,
        "rerun_log": [],
        "research_allowed": False,
        # Fields added by 2026-05-11 architectural fixes — defaults below
        # are also what callers use via state.get(..., default), so this
        # is explicit-not-required, but it makes initial state observable
        # and matches the documented schema.
        "plan_review_verdict": {},      # GAP #4 parsed verdict
    }
    base.update(kwargs)
    return base
