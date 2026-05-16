"""vault.py CLI — the human's interface to the LangGraph pipeline.

Replaces the "edit a markdown file to advance status" UX. State now lives in
sqlite checkpoints; this CLI inspects and resumes graph execution.

Subcommands:
    list                       — show all tasks + their current phase
    show <task>                — print full state for one task
    inspect <task>             — print last few state snapshots from the checkpoint history
    new <task> <prompt_file>   — create a new task and start the graph
    advance <task> [--answers <text>] [--approve] [--feedback <text>]
                               — resume a task waiting at a human gate
    rerun <task>               — resume a failed task from its last successful phase
    snooze <task> --hours N    — pause daemon pickup for a task until the timestamp expires
    status                     — show daemon health, recent phase events, and stale tasks
    models                     — show provider quota-ban status and recent successful models
    delete <task>              — remove a task's checkpoint history (use with care)

Current status: full CLI (new / advance / status / list / show / delete /
models / replay / rerun / daemon / etc.) — see cmd_* functions for the
inventory.
"""
from __future__ import annotations
import argparse
import contextlib
from collections import defaultdict
from datetime import datetime, date, timedelta
import difflib
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

# Ensure UTF-8 on Windows for box-drawing chars and arrows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from .checkpointer import checkpointer, checkpoint_db_path
from . import daemonHealth
from . import ported as P
from .graph import compile_graph, INTERRUPT_BEFORE
from .log_rotation import rotate_logs
from .nodes.planning import run_planning_phase
from .state import initial_state

SCRIPT_VERSION = "0.3.10"  # 2026-05-10 - rotate logs during daemon cycles

# ANSI styles
RESET, BOLD, DIM = "\033[0m", "\033[1m", "\033[2m"
GREEN, YELLOW, CYAN, RED, MAGENTA = "\033[32m", "\033[33m", "\033[36m", "\033[31m", "\033[35m"

EXECUTABLE_PHASES = ["task_entry", "planning", "plan_review", "refinement", "execution", "learning"]
PHASE_PREDECESSORS = {
    "planning": "task_entry",
    "plan_review": "planning",
    "refinement": "wait_human_answers",
    "execution": "wait_human_approval",
    "learning": "wait_human_review",
}
DOWNSTREAM_CLEAR_FIELDS = {
    "planning": [
        "plan_text", "plan_review_text", "plan_review_provider", "plan_review_model",
        "refinements", "refinement_n", "execution_log", "execution_diff",
        "execution_model", "build_status", "build_error", "visual_status",
        "execution_judge_text", "execution_judge_model", "learning_output",
    ],
    "plan_review": [
        "plan_review_text", "plan_review_provider", "plan_review_model",
        "refinements", "refinement_n", "execution_log", "execution_diff",
        "execution_model", "build_status", "build_error", "visual_status",
        "execution_judge_text", "execution_judge_model", "learning_output",
    ],
    "refinement": [
        "refinements", "refinement_n", "execution_log", "execution_diff",
        "execution_model", "build_status", "build_error", "visual_status",
        "execution_judge_text", "execution_judge_model", "learning_output",
    ],
    "execution": [
        "execution_log", "execution_diff", "execution_model", "build_status",
        "build_error", "visual_status", "execution_judge_text",
        "execution_judge_model", "learning_output",
    ],
    "learning": ["learning_output"],
}
FIELD_DEFAULTS = {
    "refinements": [],
    "refinement_n": 0,
    "execution_attempts": 0,
}
RESUME_STATUS = {
    "task_entry": "pending_ai_planning",
    "planning": "pending_ai_planning",
    "plan_review": "pending_ai_planning",
    "refinement": "pending_ai_refinement",
    "execution": "pending_ai_execution",
    "learning": "pending_ai_learning",
}
STALE_HUMAN_GATE_HOURS = 48
STALE_HUMAN_GATE_ENV = "VAULT_STALE_HUMAN_GATE_HOURS"
HUMAN_GATE_STATUS_PREFIX = "pending_human_"
PLAN_DIFF_HEADER = "--- planned"
EXECUTION_DIFF_HEADER = "+++ executed"


def _config(task_name: str) -> dict:
    """LangGraph config dict — thread_id == task_name so each task is isolated."""
    return {"configurable": {"thread_id": task_name}}


def _list_threads(saver) -> list:
    """Best-effort enumeration of distinct thread_ids in the checkpoint DB.

    LangGraph's SqliteSaver doesn't expose a list-threads method directly, so
    we read the underlying sqlite directly — the schema is stable and
    documented.
    """
    import sqlite3
    db_path = str(checkpoint_db_path())
    if not Path(db_path).exists():
        return []
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute("SELECT DISTINCT thread_id FROM checkpoints ORDER BY thread_id")
        return [row[0] for row in cur.fetchall()]
    except sqlite3.OperationalError:
        # Schema not yet initialized — no tasks have been run
        return []
    finally:
        conn.close()


def _latest_state(saver, task_name: str) -> dict | None:
    """Return the latest state snapshot for a task, or None if not found."""
    snapshot = saver.get(_config(task_name))
    if not snapshot:
        return None
    return snapshot.get("channel_values", {}) if isinstance(snapshot, dict) else snapshot


def _snapshot_values(snapshot) -> dict:
    """Normalize LangGraph StateSnapshot/checkpoint entries to plain values."""
    if hasattr(snapshot, "values"):
        return dict(snapshot.values or {})
    if hasattr(snapshot, "checkpoint"):
        return dict(snapshot.checkpoint.get("channel_values", {}) or {})
    if isinstance(snapshot, dict):
        return dict(snapshot.get("channel_values", snapshot) or {})
    return {}


def _snapshot_checkpoint_id(snapshot) -> str:
    config = getattr(snapshot, "config", None)
    if isinstance(config, dict):
        checkpoint_id = config.get("configurable", {}).get("checkpoint_id")
        if checkpoint_id:
            return str(checkpoint_id)
    checkpoint = getattr(snapshot, "checkpoint", None)
    if isinstance(checkpoint, dict) and checkpoint.get("id"):
        return str(checkpoint["id"])
    return ""


def _is_failure_state(values: dict) -> bool:
    return values.get("status") in ("failed", "budget_exceeded") or bool(values.get("last_failure_type"))


def _successful_phase(values: dict) -> str | None:
    status = values.get("status")
    phase = values.get("current_phase")
    if status in ("failed", "budget_exceeded"):
        return None
    if status == "completed" and values.get("learning_output"):
        return "learning"
    if status == "pending_human_review" and values.get("execution_log"):
        return "execution"
    if status == "pending_human_approval" and values.get("refinement_n", 0) > 0:
        return "refinement"
    if status == "pending_human_answers" and phase == "plan_review":
        return "plan_review"
    if values.get("plan_text") and phase in ("planning", "plan_review"):
        return "planning"
    if phase == "task_entry" and values.get("next_action") == "planning":
        return "task_entry"
    return None


def _next_phase_after(phase: str) -> str | None:
    if phase not in EXECUTABLE_PHASES:
        return None
    index = EXECUTABLE_PHASES.index(phase)
    if index + 1 >= len(EXECUTABLE_PHASES):
        return None
    return EXECUTABLE_PHASES[index + 1]


def _resume_phase_for_failure(values: dict, last_successful_phase: str) -> str | None:
    failed_phase = values.get("current_phase")
    if failed_phase in EXECUTABLE_PHASES and failed_phase != "completed":
        return failed_phase
    return _next_phase_after(last_successful_phase)


def _find_rerun_checkpoint(history: list) -> tuple | None:
    """Return (failed_snapshot, success_snapshot, success_phase, resume_phase)."""
    for failed_index, failed_snapshot in enumerate(history):
        failed_values = _snapshot_values(failed_snapshot)
        if not _is_failure_state(failed_values):
            continue
        for success_snapshot in history[failed_index + 1:]:
            success_values = _snapshot_values(success_snapshot)
            success_phase = _successful_phase(success_values)
            if not success_phase:
                continue
            resume_phase = _resume_phase_for_failure(failed_values, success_phase)
            if resume_phase in EXECUTABLE_PHASES:
                return failed_snapshot, success_snapshot, success_phase, resume_phase
        return None
    return None


def _clear_value_for(field: str):
    if field in FIELD_DEFAULTS:
        default = FIELD_DEFAULTS[field]
        return list(default) if isinstance(default, list) else default
    return ""


def _prepare_rerun_update(values: dict, resume_phase: str, source_checkpoint: str) -> dict:
    prepared = dict(values)
    for field in DOWNSTREAM_CLEAR_FIELDS.get(resume_phase, []):
        prepared[field] = _clear_value_for(field)
    prepared.update({
        "current_phase": resume_phase,
        "status": RESUME_STATUS.get(resume_phase, "pending_ai_planning"),
        "next_action": resume_phase,
        "last_failure_type": "",
        "last_failure_details": "",
        "rerun_active": True,
        "rerun_from_phase": resume_phase,
        "rerun_source_checkpoint": source_checkpoint,
        "rerun_started_at": datetime.now().isoformat(timespec="seconds"),
    })
    rerun_log = list(values.get("rerun_log", []) or [])
    rerun_log.append({
        "started_at": prepared["rerun_started_at"],
        "from_phase": resume_phase,
        "source_checkpoint": source_checkpoint,
    })
    prepared["rerun_log"] = rerun_log
    return prepared


def _rerun_as_node(resume_phase: str) -> str | None:
    return PHASE_PREDECESSORS.get(resume_phase)


def cmd_list(args):
    print(f"{BOLD}vault {SCRIPT_VERSION}{RESET}  {DIM}{checkpoint_db_path()}{RESET}\n")
    with checkpointer() as saver:
        threads = _list_threads(saver)
        if not threads:
            print(f"  {DIM}(no tasks yet — use `vault.py new <name> <prompt-file>`){RESET}")
            return 0
        print(f"  {BOLD}{'TASK':<25} {'PHASE':<22} {'STATUS':<25} ATTEMPTS{RESET}")
        print(f"  {DIM}{'-' * 80}{RESET}")
        for tname in threads:
            state = _latest_state(saver, tname) or {}
            phase = state.get("current_phase", "?")
            status = state.get("status", "?")
            attempts = state.get("execution_attempts", 0)
            color = GREEN if status == "completed" else (YELLOW if "human" in status else CYAN)
            print(f"  {tname:<25} {phase:<22} {color}{status:<25}{RESET} {attempts}")
    return 0


def _comparison_lines(text: str) -> list[str]:
    return [line.strip() for line in str(text or "").splitlines() if line.strip()]


def _planned_comparison_text(state: dict) -> str:
    parts = [state.get("plan_text", "")]
    parts.extend(state.get("refinements") or [])
    return "\n".join(part for part in parts if part)


def _executed_comparison_text(state: dict) -> str:
    return state.get("execution_diff") or state.get("execution_log") or ""


def _normalize_comparison_path(pathText: str) -> str:
    cleaned = pathText.strip().strip("`'\".,:;()[]{}<>")
    cleaned = cleaned.replace("\\", "/")
    if cleaned.startswith("a/") or cleaned.startswith("b/"):
        cleaned = cleaned[2:]
    return cleaned


def _looks_like_comparison_path(pathText: str) -> bool:
    path = _normalize_comparison_path(pathText)
    if not path or "://" in path or path.startswith("--"):
        return False
    if path in {".", ".."}:
        return False
    if "/" in path:
        return bool(re.search(r"\.[A-Za-z0-9]{1,8}$", path))
    return bool(re.fullmatch(r"[A-Za-z0-9_.-]+\.[A-Za-z0-9]{1,8}", path))


def _extract_planned_file_paths(text: str) -> set[str]:
    paths: set[str] = set()
    source = text or ""
    for match in re.findall(r"`([^`\r\n]+)`", source):
        if _looks_like_comparison_path(match):
            paths.add(_normalize_comparison_path(match))
    pathPattern = r"(?<![\w.-])(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+\.[A-Za-z0-9]{1,8}(?![\w.-])"
    for match in re.findall(pathPattern, source.replace("\\", "/")):
        if _looks_like_comparison_path(match):
            paths.add(_normalize_comparison_path(match))
    filenamePattern = r"(?<![\w./-])(?:vault\.py|[A-Za-z0-9_.-]+\.py)(?![\w./-])"
    for match in re.findall(filenamePattern, source):
        if _looks_like_comparison_path(match):
            paths.add(_normalize_comparison_path(match))
    return paths


def _extract_executed_file_paths(execution_diff: str, execution_log: str) -> set[str]:
    paths: set[str] = set()
    for line in str(execution_diff or "").splitlines():
        match = re.match(r"diff --git a/(.+?) b/(.+)$", line.strip())
        if match:
            paths.add(_normalize_comparison_path(match.group(2)))
    for line in str(execution_log or "").splitlines():
        match = re.search(r"FILE-WRITE\s*\|\s*Edit:\s*(.+)$", line.strip())
        if match and _looks_like_comparison_path(match.group(1)):
            paths.add(_normalize_comparison_path(match.group(1)))
    return paths


def _format_comparison_path_list(paths: set[str]) -> str:
    if not paths:
        return "[]"
    return "[" + ", ".join(sorted(paths)) + "]"


def _print_plan_vs_execution(task_name: str, state: dict) -> int:
    plannedText = _planned_comparison_text(state)
    executionDiff = state.get("execution_diff") or ""
    executionLog = state.get("execution_log") or ""
    executedText = _executed_comparison_text(state)

    if not plannedText.strip():
        print(f"Plan data is unavailable for '{task_name}'.")
        return 1
    if not executionDiff.strip() and not executionLog.strip():
        print(f"Execution data is unavailable for '{task_name}'.")
        return 1

    plannedPaths = _extract_planned_file_paths(plannedText)
    executedPaths = _extract_executed_file_paths(executionDiff, executionLog)
    print(f"{BOLD}{task_name}{RESET}")
    print(f"{BOLD}Plan vs execution{RESET}\n")
    print(f"Files in plan but not touched: {_format_comparison_path_list(plannedPaths - executedPaths)}")
    print(f"Files touched but not in plan: {_format_comparison_path_list(executedPaths - plannedPaths)}")
    print()

    plannedLines = _comparison_lines(plannedText)
    executedLines = _comparison_lines(executedText)
    if not plannedLines and not executedLines:
        print(f"No comparable plan or execution lines are available for '{task_name}'.")
        return 1

    print(f"{BOLD}Line-level diff{RESET}")
    diffLines = list(difflib.unified_diff(
        plannedLines,
        executedLines,
        fromfile=PLAN_DIFF_HEADER[4:],
        tofile=EXECUTION_DIFF_HEADER[4:],
        lineterm="",
    ))
    if diffLines:
        for line in diffLines:
            print(line)
    else:
        print("(no line-level differences)")
    return 0


def cmd_show(args):
    with checkpointer() as saver:
        state = _latest_state(saver, args.task)
        if state is None:
            print(f"{RED}Task '{args.task}' not found in checkpoint database.{RESET}")
            return 1
        if args.plan_vs_execution:
            return _print_plan_vs_execution(args.task, state)
        print(f"{BOLD}{args.task}{RESET}\n")
        # Print human-readable fields first, then everything else
        important = ("status", "current_phase", "next_action", "execution_attempts",
                     "planning_model", "execution_model", "last_routing_source",
                     "build_status", "visual_status")
        for k in important:
            if k in state:
                print(f"  {BOLD}{k:<22}{RESET} {state[k]}")
        print()
        # Long-text outputs
        for section, label in [
            ("plan_text", "Plan"),
            ("plan_review_text", "Plan Review"),
            ("execution_log", "Execution Log"),
            ("learning_output", "Learning"),
        ]:
            val = state.get(section)
            if val:
                print(f"{BOLD}── {label} ──{RESET}")
                print(val[:800] + ("..." if len(val) > 800 else ""))
                print()
        refinements = state.get("refinements") or []
        for index, refinement_text in enumerate(refinements, start=1):
            if refinement_text:
                print(f"{BOLD}── Plan refinement {index} ──{RESET}")
                print(refinement_text[:800] + ("..." if len(refinement_text) > 800 else ""))
                print()
    return 0


REPLAY_INPUT_FIELDS = (
    "app_location",
    "files_allowed",
    "files_blocked",
    "build_command",
    "visual_reference",
    "research_allowed",
    "cost_ceiling",
    "max_cost_usd",
)


def _replay_state_from_completed(task_name: str, completed_state: dict) -> dict:
    replay_state = initial_state(
        task_name=f"replay-of-{task_name}",
        task_file_path=completed_state.get("task_file_path", ""),
        initial_prompt=completed_state.get("initial_prompt", ""),
    )
    for field in REPLAY_INPUT_FIELDS:
        if field in completed_state:
            replay_state[field] = completed_state[field]
    replay_state.update({
        "current_phase": "planning",
        "status": "pending_ai_planning",
        "next_action": "planning",
        "existing_cost_usd": 0.0,
        "planning_cost_usd": 0.0,
        "execution_cost_usd": 0.0,
        "learning_cost_usd": 0.0,
        "phase_durations": {},
        "cost_accumulator": {
            "passes": 0, "input_tokens": 0, "output_tokens": 0,
            "cost_usd": 0.0, "model": "",
        },
    })
    return replay_state


def _print_replay_output(task_name: str, original_plan: str, replay_plan: str) -> None:
    print(replay_plan)
    print()
    print("## Unified diff vs original plan_text")
    diff_lines = difflib.unified_diff(
        str(original_plan or "").splitlines(),
        str(replay_plan or "").splitlines(),
        fromfile=f"{task_name} original plan_text",
        tofile=f"{task_name} replay plan",
        lineterm="",
    )
    diff_text = "\n".join(diff_lines)
    print(diff_text if diff_text else "(no differences)")


def cmd_replay(args):
    """Re-run planning for a completed historical task without mutating it."""
    with checkpointer() as saver:
        state = _latest_state(saver, args.task)

    if state is None:
        print(f"Error: task '{args.task}' not found.", file=sys.stderr)
        return 1
    if state.get("status") != "completed":
        print(
            f"Error: task '{args.task}' is not completed "
            f"(status: {state.get('status', '?')}).",
            file=sys.stderr,
        )
        return 1

    replay_state = _replay_state_from_completed(args.task, state)
    try:
        with contextlib.redirect_stdout(sys.stderr):
            replay_result = run_planning_phase(replay_state, dry_run=True)
    except Exception as exc:
        print(f"Error: replay planning failed for '{args.task}': {exc}", file=sys.stderr)
        return 1

    if replay_result.get("status") in ("failed", "budget_exceeded") or not replay_result.get("plan_text"):
        details = replay_result.get("last_failure_details") or "planning returned no plan_text"
        print(f"Error: replay planning failed for '{args.task}': {details}", file=sys.stderr)
        return 1

    _print_replay_output(args.task, state.get("plan_text", ""), replay_result["plan_text"])
    return 0


def cmd_inspect(args):
    """Show recent checkpoint history — lets you see how state evolved."""
    with checkpointer() as saver:
        history = list(saver.list(_config(args.task), limit=args.limit))
        if not history:
            print(f"{RED}Task '{args.task}' has no checkpoints.{RESET}")
            return 1
        print(f"{BOLD}{args.task}{RESET}  ({len(history)} checkpoints, newest first)\n")
        for i, ckpt in enumerate(history):
            ts = ckpt.metadata.get("ts", "?") if hasattr(ckpt, "metadata") else "?"
            step = ckpt.metadata.get("step", "?") if hasattr(ckpt, "metadata") else "?"
            values = ckpt.checkpoint.get("channel_values", {}) if hasattr(ckpt, "checkpoint") else {}
            phase = values.get("current_phase", "?")
            status = values.get("status", "?")
            print(f"  {DIM}[{i+1}]{RESET} step={step}  phase={phase:<20} status={status}")
    return 0


def cmd_new(args):
    """Create a new task and start the graph running.

    The graph runs until the first interrupt (wait_human_answers) and halts.
    """
    prompt_file = Path(args.prompt_file)
    if not prompt_file.exists():
        print(f"{RED}Prompt file not found: {prompt_file}{RESET}")
        return 1

    initial_prompt = prompt_file.read_text(encoding="utf-8").strip()
    state = initial_state(
        task_name=args.task,
        task_file_path=str(prompt_file.resolve()),
        initial_prompt=initial_prompt,
    )

    print(f"{BOLD}Starting task: {args.task}{RESET}\n")
    with checkpointer() as saver:
        graph = compile_graph(saver)
        # Stream so we see node activations as they happen
        for event in graph.stream(state, config=_config(args.task)):
            pass  # Node print statements show progress

    # Show where it halted
    with checkpointer() as saver:
        final = _latest_state(saver, args.task) or {}
    print(f"\n{GREEN}Halted at:{RESET} {final.get('current_phase', '?')} "
          f"(status: {final.get('status', '?')})")
    print(f"\n  Next: {DIM}python vault.py advance {args.task} --answers \"<your answers>\"{RESET}")
    return 0


# Map of (current human gate) → (input flag the user should be providing).
# Used by cmd_advance to validate that the flag matches the gate before
# resuming. Mismatches caused Bug V (auto_0074): advance --answers was
# invoked when the task was already at wait_human_approval; the resume
# streamed past wait_human_approval without halting, modifying vault-infra
# files without orchestrator review of the refined plan.
_GATE_TO_EXPECTED_INPUT = {
    # next_action / status → set of human-input flags that legitimately apply
    "wait_human_answers": {"answers", "feedback"},
    "pending_human_answers": {"answers", "feedback"},
    "wait_human_approval": {"approve", "feedback"},
    "pending_human_approval": {"approve", "feedback"},
    "wait_human_review": {"approve", "feedback"},
    "pending_human_review": {"approve", "feedback"},
}


def _current_gate(state: dict) -> str:
    """Return the current human gate name from state, or '' if not at a gate."""
    # next_action is the authoritative routing hint set by the previous node
    na = state.get("next_action", "") or ""
    if na.startswith("wait_human_"):
        return na
    # Fall back to status (e.g. "pending_human_approval")
    st = state.get("status", "") or ""
    if st.startswith("pending_human_"):
        return st
    return ""


def cmd_advance(args):
    """Resume a task halted at a human gate.

    Validates that the provided input flag matches the current gate before
    resuming (Bug V mitigation, 2026-05-11). A mismatch like `--answers`
    when the task is at wait_human_approval is refused, because resuming
    in that state can stream past the wrong interrupt and modify the work
    product without proper review.
    """
    with checkpointer() as saver:
        state = _latest_state(saver, args.task)
        if state is None:
            print(f"{RED}Task '{args.task}' not found.{RESET}")
            return 1

        # ── Bug V defense: validate flag-vs-gate before any state mutation ──
        gate = _current_gate(state)
        provided = set()
        if args.answers:
            provided.add("answers")
        if args.approve:
            provided.add("approve")
        if args.feedback:
            provided.add("feedback")
        # If at a known gate AND user provided at least one flag, refuse mismatches
        if gate and provided and gate in _GATE_TO_EXPECTED_INPUT:
            expected = _GATE_TO_EXPECTED_INPUT[gate]
            mismatched = provided - expected
            if mismatched:
                # `feedback` is always allowed (it's orthogonal); the issue is
                # answers-at-approval or approve-at-answers
                hard_mismatch = mismatched - {"feedback"}
                if hard_mismatch:
                    print(
                        f"{RED}REFUSED: task '{args.task}' is at gate "
                        f"'{gate}' but you provided {sorted(hard_mismatch)}.{RESET}\n"
                        f"  Expected input for this gate: "
                        f"{sorted(expected - {'feedback'})}\n"
                        f"  Resuming with the wrong flag can stream past the "
                        f"interrupt without halting (Bug V).\n"
                        f"  If you meant to advance from a different gate, "
                        f"check `python vault.py status {args.task}` first."
                    )
                    return 2

        # Inject the human input into state based on what gate we're at
        update: dict = {}
        if args.answers:
            update["human_answers"] = args.answers
        if args.approve:
            update["human_approval"] = "approved"
        if args.feedback:
            update["human_feedback"] = args.feedback

        # Apply the update + resume the graph
        graph = compile_graph(saver)
        if update:
            graph.update_state(_config(args.task), update)

        print(f"{BOLD}Resuming {args.task}...{RESET}\n")
        for event in graph.stream(None, config=_config(args.task)):
            pass

    with checkpointer() as saver:
        final = _latest_state(saver, args.task) or {}
    print(f"\n{GREEN}Halted at:{RESET} {final.get('current_phase', '?')} "
          f"(status: {final.get('status', '?')})")
    return 0


def cmd_rerun(args):
    """Resume a failed task from the latest successful checkpoint before failure."""
    with checkpointer() as saver:
        graph = compile_graph(saver)
        latest = graph.get_state(_config(args.task))
        if not latest or not getattr(latest, "values", None):
            print(f"{RED}Task '{args.task}' has no checkpoint history; use vault.py new to start it.{RESET}")
            return 1

        history = list(graph.get_state_history(_config(args.task), limit=200))
        if not history:
            print(f"{RED}Task '{args.task}' has no checkpoint history; use vault.py new to start it.{RESET}")
            return 1

        selection = _find_rerun_checkpoint(history)
        if selection is None:
            print(f"{RED}Task '{args.task}' has no failed checkpoint to rerun from.{RESET}")
            return 1

        failed_snapshot, success_snapshot, success_phase, resume_phase = selection
        source_checkpoint = _snapshot_checkpoint_id(success_snapshot)
        success_values = _snapshot_values(success_snapshot)
        prepared = _prepare_rerun_update(success_values, resume_phase, source_checkpoint)
        as_node = _rerun_as_node(resume_phase)
        if as_node is None:
            print(f"{RED}Cannot rerun '{args.task}' from phase '{resume_phase}'.{RESET}")
            return 1

        failed_values = _snapshot_values(failed_snapshot)
        failed_phase = failed_values.get("current_phase", "?")
        print(f"{BOLD}Rerunning {args.task} from phase: {resume_phase}{RESET}")
        print(f"  Last successful phase: {success_phase}")
        print(f"  Failed checkpoint phase: {failed_phase}")
        if source_checkpoint:
            print(f"  Source checkpoint: {source_checkpoint}")
        print()

        resume_config = graph.update_state(
            success_snapshot.config,
            prepared,
            as_node=as_node,
        )
        for _ in graph.stream(None, config=resume_config):
            pass

    with checkpointer() as saver:
        final = _latest_state(saver, args.task) or {}
    print(f"\n{GREEN}Halted at:{RESET} {final.get('current_phase', '?')} "
          f"(status: {final.get('status', '?')})")
    return 0


def cmd_delete(args):
    """Remove a task's entire checkpoint history. Destructive — confirms first."""
    if not args.yes:
        print(f"{YELLOW}This will permanently delete all checkpoints for {args.task}.{RESET}")
        print(f"Re-run with --yes to confirm.")
        return 1
    import sqlite3
    conn = sqlite3.connect(str(checkpoint_db_path()))
    try:
        cur = conn.execute("DELETE FROM checkpoints WHERE thread_id = ?", (args.task,))
        # Also delete related tables LangGraph maintains
        for tbl in ("writes", "checkpoint_blobs"):
            try:
                conn.execute(f"DELETE FROM {tbl} WHERE thread_id = ?", (args.task,))
            except sqlite3.OperationalError:
                pass
        conn.commit()
        print(f"{GREEN}Deleted {cur.rowcount} checkpoint(s) for {args.task}.{RESET}")
    finally:
        conn.close()
    return 0


# ─── Daemon: watch task_files/ for new tasks routed by sharpener ─────────────

def _vault_root_path() -> Path:
    return checkpoint_db_path().parent.parent


def _tail_jsonl(path: Path, maxEntries: int, chunkSize: int = 8192) -> list[dict]:
    if not path.exists():
        return []
    buffer = b""
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            position = f.tell()
            while position > 0 and buffer.count(b"\n") <= maxEntries * 3:
                readSize = min(chunkSize, position)
                position -= readSize
                f.seek(position)
                buffer = f.read(readSize) + buffer
    except OSError:
        return []

    events = []
    for rawLine in reversed(buffer.splitlines()):
        line = rawLine.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            events.append(entry)
            if len(events) >= maxEntries:
                break
    return events


def _formatMoney(value) -> str:
    try:
        amount = float(value)
    except (TypeError, ValueError):
        amount = 0.0
    return f"${amount:.4f}"


def _formatDuration(value) -> str:
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return "duration=?"
    return f"duration={amount:.1f}s"


def _formatPhaseEvent(entry: dict) -> str:
    timestamp = entry.get("timestamp") or entry.get("date") or "timestamp=?"
    details = [
        f"task={entry.get('task', '?')}",
        f"phase={entry.get('phase', '?')}",
        f"status={entry.get('status', '?')}",
        f"provider={entry.get('provider', '?')}",
        f"model={entry.get('model', '?')}",
        _formatDuration(entry.get("duration_seconds")),
        f"cost={_formatMoney(entry.get('cost_usd'))}",
    ]
    return f"{timestamp}  {' '.join(details)}"


def _formatDaemonStatus(label: str, entry: dict | None, staleAfterSeconds: int) -> str:
    if not isinstance(entry, dict):
        return f"{label}: not running, heartbeat missing"

    try:
        staleAfterSeconds = int(entry.get("unresponsiveAfterSeconds", staleAfterSeconds))
    except (TypeError, ValueError):
        pass

    heartbeatAge = daemonHealth.secondsSince(entry.get("lastHeartbeat", ""))
    heartbeatText = "heartbeat missing"
    if heartbeatAge is not None:
        heartbeatText = f"last heartbeat {heartbeatAge}s ago"

    pid = entry.get("pid")
    isRunning = daemonHealth.isProcessRunning(pid)
    if not isRunning:
        return f"{label}: not running, {heartbeatText}"

    if heartbeatAge is None:
        return f"{label}: PID {pid}, heartbeat missing"
    if heartbeatAge > staleAfterSeconds:
        return f"{label}: PID {pid}, heartbeat stale/unresponsive: {heartbeatAge}s ago"
    return f"{label}: PID {pid}, heartbeat {heartbeatAge}s ago"


def _recentPhaseEvents(vaultRoot: Path, limit: int = 10) -> list[dict]:
    return _tail_jsonl(vaultRoot / "logs" / "cost_log.jsonl", limit)


def _staleHumanGateThresholdHours() -> int:
    try:
        return max(1, int(os.environ.get(STALE_HUMAN_GATE_ENV, STALE_HUMAN_GATE_HOURS)))
    except (TypeError, ValueError):
        return STALE_HUMAN_GATE_HOURS


def _isHumanGateStatus(status: str) -> bool:
    return isinstance(status, str) and status.startswith(HUMAN_GATE_STATUS_PREFIX)


def _parseCheckpointTimestamp(value) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None


def _checkpointTupleTimestamp(checkpointTuple) -> datetime | None:
    checkpoint = getattr(checkpointTuple, "checkpoint", None)
    if isinstance(checkpoint, dict):
        parsed = _parseCheckpointTimestamp(checkpoint.get("ts"))
        if parsed:
            return parsed

    metadata = getattr(checkpointTuple, "metadata", None)
    if isinstance(metadata, dict):
        return _parseCheckpointTimestamp(metadata.get("ts"))
    return None


def _waitHoursSince(startedAt: datetime, now: datetime | None = None) -> int:
    if now is None:
        now = datetime.now(startedAt.tzinfo) if startedAt.tzinfo else datetime.now()
    elif startedAt.tzinfo and now.tzinfo:
        now = now.astimezone(startedAt.tzinfo)
    elif startedAt.tzinfo and not now.tzinfo:
        now = now.replace(tzinfo=startedAt.tzinfo)
    elif not startedAt.tzinfo and now.tzinfo:
        now = now.replace(tzinfo=None)

    elapsedSeconds = max(0, (now - startedAt).total_seconds())
    return int(elapsedSeconds // 3600)


def _formatStaleTaskAge(waitHours: int) -> str:
    return f"STALE waiting {waitHours}h ({waitHours} hours)"


def _findStaleHumanGateTasksForThreads(
    saver,
    threads: list[str],
    now: datetime | None = None,
    thresholdHours: int | None = None,
) -> list[dict]:
    thresholdHours = thresholdHours if thresholdHours is not None else _staleHumanGateThresholdHours()
    staleTasks = []

    for taskName in threads:
        checkpointTuple = saver.get_tuple(_config(taskName))
        if not checkpointTuple:
            continue

        values = _snapshot_values(checkpointTuple)
        status = values.get("status", "")
        if not _isHumanGateStatus(status):
            continue

        checkpointTimestamp = _checkpointTupleTimestamp(checkpointTuple)
        if not checkpointTimestamp:
            continue

        waitHours = _waitHoursSince(checkpointTimestamp, now)
        if waitHours <= thresholdHours:
            continue

        staleTasks.append({
            "task_name": taskName,
            "current_phase": values.get("current_phase", "?"),
            "wait_hours": waitHours,
        })

    return sorted(staleTasks, key=lambda item: (-item["wait_hours"], item["task_name"]))


def _findStaleHumanGateTasks(
    saver,
    now: datetime | None = None,
    thresholdHours: int | None = None,
) -> list[dict]:
    return _findStaleHumanGateTasksForThreads(
        saver,
        _list_threads(saver),
        now=now,
        thresholdHours=thresholdHours,
    )


def cmd_status(args):
    vaultRoot = _vault_root_path()
    health = daemonHealth.readHealthState(vaultRoot)
    sharpener = health.get("sharpener")
    executor = health.get("executor")
    events = _recentPhaseEvents(vaultRoot, 10)
    with checkpointer() as saver:
        staleTasks = _findStaleHumanGateTasks(saver)

    print(f"{BOLD}Vault Status{RESET}\n")
    print(f"{BOLD}Daemons{RESET}")
    print(f"  {_formatDaemonStatus('Sharpener daemon', sharpener, 130)}")
    print(f"  {_formatDaemonStatus('Executor daemon', executor, 30)}")
    print()
    print(f"{BOLD}Recent Phase Events{RESET}")
    if not events:
        print("  (no phase events found)")
    else:
        for event in events:
            print(f"  {_formatPhaseEvent(event)}")
    print()
    print(f"{BOLD}Stale Tasks{RESET}")
    if not staleTasks:
        print("  (no stale human-gate tasks found)")
    else:
        print(f"  {BOLD}{'TASK':<25} {'PHASE':<22} AGE{RESET}")
        print(f"  {DIM}{'-' * 72}{RESET}")
        for task in staleTasks:
            taskName = task["task_name"]
            phase = task["current_phase"]
            age = _formatStaleTaskAge(task["wait_hours"])
            print(f"  {taskName:<25} {phase:<22} {YELLOW}{age}{RESET}")
    return 0


def _cost_log_path() -> Path:
    return _vault_root_path() / "logs" / "cost_log.jsonl"


def _model_pool_path() -> Path:
    return _vault_root_path() / "state" / "model_pool.json"


def _load_model_pool_providers() -> list[str]:
    try:
        data = json.loads(_model_pool_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    providers = data.get("providers", {})
    if not isinstance(providers, dict):
        return []
    return sorted(str(provider) for provider in providers if str(provider).strip())


def _quota_banned_until_map() -> dict[str, float]:
    dougsModule = getattr(P, "_d", None)
    bannedUntil = getattr(dougsModule, "_quota_banned_until", {})
    return bannedUntil if isinstance(bannedUntil, dict) else {}


def _format_ban_remaining(untilTimestamp: float, nowTimestamp: float | None = None) -> str:
    nowTimestamp = time.time() if nowTimestamp is None else nowTimestamp
    remainingSeconds = max(0, int(round(float(untilTimestamp) - nowTimestamp)))
    if remainingSeconds <= 0:
        return "expired"
    hours, remainder = divmod(remainingSeconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {seconds:02d}s"
    return f"{seconds}s"


def _recent_successful_models_by_provider(limit: int = 5000) -> dict[str, str]:
    recentModels: dict[str, str] = {}
    for entry in _read_cost_entries(_cost_log_path(), limit):
        if entry.get("status") != "success":
            continue
        provider = str(entry.get("provider") or "").strip()
        model = str(entry.get("model") or "").strip()
        if provider and model and provider not in recentModels:
            recentModels[provider] = model
    return recentModels


def cmd_models(args):
    providers = _load_model_pool_providers()
    recentModels = _recent_successful_models_by_provider()
    quotaBannedUntil = _quota_banned_until_map()
    nowTimestamp = time.time()

    print(f"{BOLD}vault models {SCRIPT_VERSION}{RESET}")
    print()
    if not providers:
        print("  (no providers found in model_pool.json)")
        return 1

    print(f"  {BOLD}{'Provider':<12} {'Status':<14} {'Ban lifts in':<14} Recent success{RESET}")
    print(f"  {DIM}{'-' * 66}{RESET}")
    for provider in providers:
        untilTimestamp = _coerceFloat(quotaBannedUntil.get(provider), 0.0)
        isBanned = untilTimestamp > nowTimestamp
        status = "quota-banned" if isBanned else "active"
        remaining = _format_ban_remaining(untilTimestamp, nowTimestamp) if isBanned else "-"
        recentModel = recentModels.get(provider, "-")
        color = YELLOW if isBanned else GREEN
        print(f"  {provider:<12} {color}{status:<14}{RESET} {remaining:<14} {recentModel}")
    return 0


def _skills_root_path() -> Path:
    return _vault_root_path() / "ai_skills"


def _load_canonical_skill_categories(skillsRoot: Path) -> list[str]:
    categoriesPath = skillsRoot / "_categories.json"
    try:
        data = json.loads(categoriesPath.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []

    categories = data.get("categories", {})
    if isinstance(categories, dict):
        return sorted(str(name) for name in categories if str(name).strip())
    if isinstance(categories, list):
        return sorted(str(name) for name in categories if str(name).strip())
    return []


def _active_skill_names(skillsRoot: Path) -> list[str]:
    if not skillsRoot.exists():
        return []
    names: list[str] = []
    for path in sorted(skillsRoot.iterdir()):
        if not path.is_dir() or path.name == "_archive":
            continue
        if (path / "SKILL.md").is_file():
            names.append(path.name)
    return names


def _count_skill_lines(skillPath: Path) -> int:
    try:
        text = skillPath.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    return len(text.splitlines())


def _skill_modified_date(skillPath: Path) -> str:
    try:
        modifiedTime = datetime.fromtimestamp(skillPath.stat().st_mtime)
    except OSError:
        return "unknown"
    return modifiedTime.strftime("%Y-%m-%d")


def _skill_file_size(skillPath: Path) -> int:
    try:
        return skillPath.stat().st_size
    except OSError:
        return 0


def _curation_log_paths(skillsRoot: Path) -> list[Path]:
    return [
        skillsRoot / "_curation_log.jsonl",
        skillsRoot / "curation_log.jsonl",
    ]


def _curation_entry_names_category(entry: dict, categoryName: str) -> bool:
    if entry.get("category") == categoryName:
        return True
    return any(value == categoryName for value in entry.values() if isinstance(value, str))


def _curation_log_counts(skillsRoot: Path, skillNames: list[str]) -> dict[str, int]:
    counts = {name: 0 for name in skillNames}
    for logPath in _curation_log_paths(skillsRoot):
        if not logPath.exists():
            continue
        try:
            lines = logPath.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                for name in skillNames:
                    if name in line:
                        counts[name] += 1
                continue
            if not isinstance(entry, dict):
                continue
            for name in skillNames:
                if _curation_entry_names_category(entry, name):
                    counts[name] += 1
    return counts


def _archive_destination(skillsRoot: Path, skillName: str) -> Path:
    archiveRoot = skillsRoot / "_archive"
    destination = archiveRoot / skillName
    if not destination.exists():
        return destination
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    destination = archiveRoot / f"{skillName}_{timestamp}"
    suffix = 2
    while destination.exists():
        destination = archiveRoot / f"{skillName}_{timestamp}_{suffix}"
        suffix += 1
    return destination


def _archive_skill(skillsRoot: Path, skillName: str) -> Path:
    source = skillsRoot / skillName
    if not source.is_dir():
        raise FileNotFoundError(f"active skill folder not found: {source}")
    destination = _archive_destination(skillsRoot, skillName)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(destination))
    return destination


def _skill_prune_rows() -> tuple[Path, list[dict]]:
    from .docs_refresh import _detect_skill_quality_issues

    skillsRoot = _skills_root_path()
    canonicalNames = _load_canonical_skill_categories(skillsRoot)
    canonicalSet = set(canonicalNames)
    activeNames = _active_skill_names(skillsRoot)
    curationCounts = _curation_log_counts(skillsRoot, activeNames)
    qualityIssues = dict(_detect_skill_quality_issues(skillsRoot, activeNames))

    rows: list[dict] = []
    for name in activeNames:
        skillPath = skillsRoot / name / "SKILL.md"
        qualityIssue = qualityIssues.get(name)
        rows.append({
            "name": name,
            "kind": "canonical" if name in canonicalSet else "pre-canonical",
            "modified": _skill_modified_date(skillPath),
            "bytes": _skill_file_size(skillPath),
            "lines": _count_skill_lines(skillPath),
            "curation_count": curationCounts.get(name, 0),
            "quality": f"DAMAGED: {qualityIssue}" if qualityIssue else "PASS",
            "damaged": bool(qualityIssue),
        })
    return skillsRoot, rows


def cmd_skill_stats(args):
    """Report skill load/use/helpful statistics from logs/skill_usage.jsonl.

    Replaces the old auto-curator proposal-staging mechanism (removed
    2026-05-12). Instead of an LLM proposing changes to skill files,
    this command surfaces the data — which skills loaded into tasks,
    how often they were marked used, how often they were marked helpful.
    The user reads the data and decides if a skill needs revision.
    """
    import json as _j
    from collections import defaultdict, Counter
    vault_root = _vault_root_path()
    log = vault_root / "logs" / "skill_usage.jsonl"
    if not log.exists():
        print(f"{DIM}(no logs/skill_usage.jsonl yet — run a task to start collecting data){RESET}")
        return 0

    per_skill = defaultdict(lambda: Counter())
    tasks_per_skill = defaultdict(set)
    n_rows = 0
    n_bad = 0
    for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = _j.loads(line)
        except (_j.JSONDecodeError, ValueError):
            n_bad += 1
            continue
        if not isinstance(rec, dict):
            n_bad += 1
            continue
        n_rows += 1
        skill = rec.get("skill") or rec.get("skill_name") or "?"
        task = rec.get("task") or rec.get("task_name") or "?"
        tasks_per_skill[skill].add(task)
        if rec.get("loaded"):
            per_skill[skill]["loaded"] += 1
        if rec.get("used"):
            per_skill[skill]["used"] += 1
        if rec.get("helpful") is True:
            per_skill[skill]["helpful"] += 1
        elif rec.get("helpful") is False:
            per_skill[skill]["unhelpful"] += 1

    if not per_skill:
        print(f"{DIM}(no skill usage events parsed from {n_rows} rows; {n_bad} malformed){RESET}")
        return 0

    # Active skill set for "loaded but skill no longer exists" detection
    skills_dir = vault_root / "ai_skills"
    active = {d.name for d in skills_dir.iterdir()
              if d.is_dir() and not d.name.startswith("_")}

    print(f"{BOLD}Skill effectiveness — {n_rows} rows across {len(per_skill)} skills{RESET}")
    if n_bad:
        print(f"  {YELLOW}({n_bad} malformed row(s) skipped){RESET}")
    print()
    print(f"  {BOLD}{'Skill':<35} {'Tasks':>6} {'Loaded':>7} {'Used':>6} {'Helpful':>8} {'Unhelpful':>10} {'Status'}{RESET}")
    print(f"  {DIM}{'-' * 105}{RESET}")
    rows = sorted(per_skill.items(), key=lambda x: -x[1]["loaded"])
    for skill, c in rows:
        is_active = skill in active
        status = "active" if is_active else f"{YELLOW}archived/missing{RESET}"
        loaded = c["loaded"]
        used = c["used"]
        helpful = c["helpful"]
        unhelpful = c["unhelpful"]
        # Surface "load-but-never-helpful" as low-value
        if loaded >= 3 and helpful == 0 and used > 0:
            status = f"{RED}LOW VALUE{RESET} (loaded but never helpful)"
        elif loaded >= 3 and used == 0:
            status = f"{YELLOW}LOAD-ONLY{RESET} (loaded but never used)"
        print(f"  {skill:<35} {len(tasks_per_skill[skill]):>6} {loaded:>7} {used:>6} {helpful:>8} {unhelpful:>10} {status}")
    print()
    print(f"  {DIM}Use this data to decide which skills need human re-authoring.{RESET}")
    print(f"  {DIM}Skill content is user + orchestrator co-authored only as of 2026-05-11.{RESET}")
    return 0


def cmd_prune_skills(args):
    if args.apply and args.dry_run:
        print(f"{RED}Error:{RESET} choose either --dry-run or --apply, not both")
        return 2

    applyChanges = bool(args.apply)
    skillsRoot, rows = _skill_prune_rows()
    damagedRows = [row for row in rows if row["damaged"]]

    mode = "APPLY" if applyChanges else "DRY RUN"
    print(f"{BOLD}vault prune-skills {SCRIPT_VERSION}{RESET}  {DIM}{mode} {skillsRoot}{RESET}")
    print()
    if not rows:
        print("  (no active SKILL.md files found)")
        return 0

    print(f"  {BOLD}{'Category':<34} {'Type':<14} {'Modified':<12} {'Bytes':>8} {'Lines':>7} {'Log':>5} Quality{RESET}")
    print(f"  {DIM}{'-' * 108}{RESET}")
    for row in rows:
        color = RED if row["damaged"] else GREEN
        print(
            f"  {row['name']:<34} {row['kind']:<14} {row['modified']:<12} "
            f"{row['bytes']:>8} {row['lines']:>7} {row['curation_count']:>5} "
            f"{color}{row['quality']}{RESET}"
        )

    print()
    if applyChanges:
        if not damagedRows:
            print(f"{GREEN}No damaged skills to archive.{RESET}")
            return 0
        for row in damagedRows:
            try:
                destination = _archive_skill(skillsRoot, row["name"])
            except OSError as exc:
                print(f"{RED}Failed to archive {row['name']}: {exc}{RESET}")
                return 1
            print(f"{YELLOW}Archived damaged skill:{RESET} {row['name']} -> {destination.relative_to(_vault_root_path())}")
    else:
        print(f"Dry run: {len(damagedRows)} damaged skill(s) would be archived with --apply.")
    return 0


def _coerceFloat(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_budget_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD, got {value!r}") from e


def _entry_datetime(entry: dict) -> datetime | None:
    value = entry.get("timestamp") or entry.get("date")
    if not value:
        return None
    if isinstance(value, str):
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%d"):
            try:
                return datetime.strptime(value[:26], fmt)
            except ValueError:
                pass
    return None


def _entry_date(entry: dict) -> date | None:
    parsed = _entry_datetime(entry)
    if parsed:
        return parsed.date()
    value = entry.get("date")
    if isinstance(value, str):
        try:
            return datetime.strptime(value[:10], "%Y-%m-%d").date()
        except ValueError:
            return None
    return None


def _read_cost_entries(log_path: Path, limit: int) -> list[dict]:
    return _tail_jsonl(log_path, max(1, int(limit)))


def _filter_budget_entries(
    entries: list[dict],
    since: date | None,
    until: date | None,
    days: int | None,
) -> tuple[list[dict], date | None, date | None, str]:
    entryDates = [d for d in (_entry_date(entry) for entry in entries) if d is not None]
    windowLabel = "all available entries"
    if days:
        anchor = max(entryDates) if entryDates else date.today()
        since = anchor - timedelta(days=max(1, days) - 1)
        until = anchor if until is None else until
        windowLabel = f"last {max(1, days)} day(s), anchored at {anchor.isoformat()}"
    elif since or until:
        windowLabel = f"{since.isoformat() if since else 'beginning'} to {until.isoformat() if until else 'latest'}"

    filtered = []
    for entry in entries:
        entryDate = _entry_date(entry)
        if entryDate is None:
            if since or until:
                continue
        elif since and entryDate < since:
            continue
        elif until and entryDate > until:
            continue
        filtered.append(entry)
    return filtered, since, until, windowLabel


def _blank_budget_bucket() -> dict:
    return {"calls": 0, "spend": 0.0, "estimated": 0, "duration": 0.0}


def _add_budget_entry(bucket: dict, entry: dict) -> None:
    bucket["calls"] += 1
    bucket["spend"] += _coerceFloat(entry.get("cost_usd"))
    bucket["duration"] += _coerceFloat(entry.get("duration_seconds"))
    if entry.get("estimated"):
        bucket["estimated"] += 1


def _finalize_budget_bucket(bucket: dict) -> dict:
    calls = bucket["calls"]
    spend = bucket["spend"]
    return {
        "calls": calls,
        "spend": round(spend, 6),
        "estimated": bucket["estimated"],
        "duration_seconds": round(bucket["duration"], 2),
        "avg_cost": round(spend / calls, 6) if calls else 0.0,
    }


def _phase_group_for_budget_report(phase: str) -> str:
    if phase in ("planning", "plan_review", "refinement"):
        return "planning"
    if phase == "learning":
        return "learning"
    return "execution"


def _load_task_budgets(vaultRoot: Path) -> dict[str, dict[str, float]]:
    budgets = {}
    taskDir = vaultRoot / "task_files"
    if not taskDir.exists():
        return budgets
    for taskPath in taskDir.glob("*.md"):
        try:
            spec = _parse_task_file(taskPath)
        except (OSError, ValueError):
            continue
        budgets[spec["name"]] = {
            "planning": spec["resolved_cost_ceilings"]["planning"],
            "execution": spec["resolved_cost_ceilings"]["execution"],
            "learning": spec["resolved_cost_ceilings"]["learning"],
        }
    return budgets


def _summarize_budget(entries: list[dict], taskBudgets: dict[str, float]) -> dict:
    providerBuckets = defaultdict(_blank_budget_bucket)
    modelBuckets = defaultdict(_blank_budget_bucket)
    phaseBuckets = defaultdict(_blank_budget_bucket)
    taskBuckets = defaultdict(_blank_budget_bucket)
    dayBuckets = defaultdict(_blank_budget_bucket)
    weekBuckets = defaultdict(_blank_budget_bucket)
    monthBuckets = defaultdict(_blank_budget_bucket)

    total = _blank_budget_bucket()
    datedEntries = []
    calls = []
    for entry in entries:
        _add_budget_entry(total, entry)
        provider = entry.get("provider") or "unknown"
        model = entry.get("model") or "unknown"
        phase = entry.get("phase") or "unknown"
        task = entry.get("task") or "unknown"
        entryDate = _entry_date(entry)

        _add_budget_entry(providerBuckets[provider], entry)
        _add_budget_entry(modelBuckets[f"{provider}/{model}"], entry)
        _add_budget_entry(phaseBuckets[phase], entry)
        _add_budget_entry(taskBuckets[task], entry)
        if entryDate:
            datedEntries.append(entryDate)
            _add_budget_entry(dayBuckets[entryDate.isoformat()], entry)
            isoYear, isoWeek, _ = entryDate.isocalendar()
            _add_budget_entry(weekBuckets[f"{isoYear}-W{isoWeek:02d}"], entry)
            _add_budget_entry(monthBuckets[entryDate.strftime("%Y-%m")], entry)

        calls.append({
            "timestamp": entry.get("timestamp") or entry.get("date") or "",
            "task": task,
            "phase": phase,
            "provider": provider,
            "model": model,
            "status": entry.get("status") or "",
            "spend": round(_coerceFloat(entry.get("cost_usd")), 6),
            "duration_seconds": round(_coerceFloat(entry.get("duration_seconds")), 2),
            "estimated": bool(entry.get("estimated")),
        })

    def finalizeMap(source: dict) -> list[dict]:
        rows = []
        for name, bucket in source.items():
            row = {"name": name}
            row.update(_finalize_budget_bucket(bucket))
            rows.append(row)
        return sorted(rows, key=lambda row: (-row["spend"], -row["calls"], row["name"]))

    overview = _finalize_budget_bucket(total)
    overview["date_range"] = {
        "start": min(datedEntries).isoformat() if datedEntries else None,
        "end": max(datedEntries).isoformat() if datedEntries else None,
    }

    summary = {
        "overview": overview,
        "by_provider": finalizeMap(providerBuckets),
        "by_provider_model": finalizeMap(modelBuckets),
        "by_phase": finalizeMap(phaseBuckets),
        "by_task": finalizeMap(taskBuckets),
        "by_day": finalizeMap(dayBuckets),
        "by_week": finalizeMap(weekBuckets),
        "by_month": finalizeMap(monthBuckets),
        "top_calls": sorted(calls, key=lambda row: (-row["spend"], -row["duration_seconds"])),
        "warnings": [],
    }
    _add_budget_warnings(summary, taskBudgets)
    return summary


def _add_budget_warnings(summary: dict, taskBudgets: dict[str, dict[str, float]]) -> None:
    warnings = summary["warnings"]
    providerRows = summary["by_provider"]
    providerAverage = (
        sum(row["spend"] for row in providerRows) / len(providerRows)
        if providerRows else 0.0
    )
    for row in providerRows:
        if providerAverage > 0 and row["spend"] > providerAverage:
            warnings.append(
                f"Provider {row['name']} is above average provider spend "
                f"({_formatMoney(row['spend'])} > {_formatMoney(providerAverage)})."
            )

    phaseSpendByTask = defaultdict(lambda: defaultdict(float))
    for call in summary["top_calls"]:
        phaseGroup = _phase_group_for_budget_report(call["phase"])
        phaseSpendByTask[call["task"]][phaseGroup] += _coerceFloat(call["spend"])

    for taskName, phaseSpend in phaseSpendByTask.items():
        budgets = taskBudgets.get(taskName, {})
        for phaseGroup, spend in phaseSpend.items():
            budget = budgets.get(phaseGroup, 0.0)
            if not budget or spend < budget:
                continue
            warnings.append(
                f"Task {taskName} reached its cost_ceiling on phase {phaseGroup} "
                f"({_formatMoney(spend)} >= {_formatMoney(budget)})."
            )

    overallAverage = summary["overview"]["avg_cost"]
    highAverageThreshold = overallAverage * 2
    if highAverageThreshold > 0:
        for label, rows in (("Phase", summary["by_phase"]), ("Model", summary["by_provider_model"])):
            for row in rows:
                if row["avg_cost"] >= highAverageThreshold:
                    warnings.append(
                        f"{label} {row['name']} average call cost is at least 2x overall "
                        f"({_formatMoney(row['avg_cost'])} >= {_formatMoney(highAverageThreshold)})."
                    )


def _print_budget_table(title: str, rows: list[dict], columns: list[tuple[str, str, int]], top: int | None = None) -> None:
    print(f"{BOLD}{title}{RESET}")
    if not rows:
        print("  (none)")
        print()
        return
    selectedRows = rows[:top] if top else rows
    header = "  " + " ".join(f"{label:<{width}}" for _, label, width in columns)
    print(f"{BOLD}{header}{RESET}")
    print(f"  {DIM}{'-' * max(60, len(header) - 2)}{RESET}")
    for row in selectedRows:
        values = []
        for key, _, width in columns:
            value = row.get(key, "")
            if key in ("spend", "avg_cost"):
                value = _formatMoney(value)
            elif key == "duration_seconds":
                value = f"{_coerceFloat(value):.1f}s"
            else:
                value = str(value)
            if len(value) > width:
                value = value[:max(0, width - 3)] + "..."
            values.append(f"{value:<{width}}")
        print("  " + " ".join(values))
    print()


def _print_budget_report(summary: dict, windowLabel: str, limit: int, top: int) -> None:
    overview = summary["overview"]
    dateRange = overview["date_range"]
    rangeText = "no dated entries"
    if dateRange["start"] and dateRange["end"]:
        rangeText = f"{dateRange['start']} to {dateRange['end']}"

    print(f"{BOLD}Vault Budget{RESET}  {DIM}{windowLabel}; newest {limit} log entries scanned{RESET}\n")
    print(f"{BOLD}Overview{RESET}")
    print(f"  Total spend: {_formatMoney(overview['spend'])}")
    print(f"  Calls: {overview['calls']}  Estimated entries: {overview['estimated']}")
    print(f"  Duration: {overview['duration_seconds']:.1f}s  Average call: {_formatMoney(overview['avg_cost'])}")
    print(f"  Date range: {rangeText}\n")

    _print_budget_table("By Provider", summary["by_provider"], [
        ("name", "PROVIDER", 18),
        ("calls", "CALLS", 8),
        ("spend", "SPEND", 12),
        ("avg_cost", "AVG", 12),
        ("duration_seconds", "DURATION", 12),
    ])
    _print_budget_table("By Provider / Model", summary["by_provider_model"], [
        ("name", "PROVIDER/MODEL", 34),
        ("calls", "CALLS", 8),
        ("spend", "SPEND", 12),
        ("avg_cost", "AVG", 12),
    ], top)
    _print_budget_table("By Phase", summary["by_phase"], [
        ("name", "PHASE", 20),
        ("calls", "CALLS", 8),
        ("spend", "SPEND", 12),
        ("avg_cost", "AVG", 12),
    ])
    _print_budget_table("By Day", summary["by_day"], [
        ("name", "DAY", 14),
        ("calls", "CALLS", 8),
        ("spend", "SPEND", 12),
        ("duration_seconds", "DURATION", 12),
    ], top)
    _print_budget_table("Top Tasks", summary["by_task"], [
        ("name", "TASK", 24),
        ("calls", "CALLS", 8),
        ("spend", "SPEND", 12),
        ("avg_cost", "AVG", 12),
    ], top)
    _print_budget_table("Top Calls", summary["top_calls"], [
        ("timestamp", "TIMESTAMP", 20),
        ("task", "TASK", 18),
        ("phase", "PHASE", 14),
        ("provider", "PROVIDER", 12),
        ("model", "MODEL", 20),
        ("spend", "SPEND", 12),
    ], top)

    if summary["warnings"]:
        print(f"{YELLOW}{BOLD}Warnings{RESET}")
        for warning in summary["warnings"]:
            print(f"  {YELLOW}!{RESET} {warning}")
        print()


def cmd_budget(args):
    logPath = _cost_log_path()
    if not logPath.exists():
        payload = {"entries": 0, "message": f"no cost log found: {logPath}"}
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            print(f"{YELLOW}No cost log found:{RESET} {logPath}")
        return 0

    since = _parse_budget_date(args.since)
    until = _parse_budget_date(args.until)
    if since and until and since > until:
        print(f"{RED}--since must be on or before --until.{RESET}")
        return 1

    entries = _read_cost_entries(logPath, args.limit)
    filteredEntries, since, until, windowLabel = _filter_budget_entries(entries, since, until, args.days)
    if not filteredEntries:
        payload = {
            "entries": 0,
            "window": {"since": since.isoformat() if since else None, "until": until.isoformat() if until else None},
            "message": "no cost entries matched filter",
        }
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            print(f"{YELLOW}No cost entries matched filter:{RESET} {windowLabel}")
        return 0

    summary = _summarize_budget(filteredEntries, _load_task_budgets(_vault_root_path()))
    summary["window"] = {
        "label": windowLabel,
        "since": since.isoformat() if since else None,
        "until": until.isoformat() if until else None,
        "limit": args.limit,
        "matched_entries": len(filteredEntries),
    }
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        _print_budget_report(summary, windowLabel, args.limit, args.top)
    return 0


def _parse_task_file(path: Path) -> dict:
    """Read a markdown task file and extract frontmatter + Initial Prompt section.

    Returns dict with: name, status, app_location, files_allowed, files_blocked,
    build_command, visual_reference, research_allowed, cost ceilings/spend,
    snooze_until, initial_prompt.
    """
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    frontmatter: dict = {}
    body_start = 0
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                body_start = i + 1
                break
            if ":" in lines[i]:
                k, _, v = lines[i].partition(":")
                frontmatter[k.strip()] = v.strip()

    # Extract Initial Prompt section
    body = "\n".join(lines[body_start:])
    initial_prompt = ""
    import re as _re
    m = _re.search(r"##\s*Initial Prompt\s*\n(.+?)(?=\n##|\Z)", body, _re.DOTALL | _re.IGNORECASE)
    if m:
        initial_prompt = m.group(1).strip()

    # 2026-05-12: single `cost_ceiling` (legacy `max_cost_usd` still honored).
    default_cost_ceiling = 2.0
    cost_ceiling = (
        _coerceFloat(frontmatter.get("cost_ceiling"), default_cost_ceiling)
        if frontmatter.get("cost_ceiling", "") not in (None, "")
        else (
            _coerceFloat(frontmatter.get("max_cost_usd"), default_cost_ceiling)
            if frontmatter.get("max_cost_usd", "") not in (None, "")
            else default_cost_ceiling
        )
    )

    return {
        "name": path.stem,
        "status": frontmatter.get("status", ""),
        "app_location": frontmatter.get("app_location", ""),
        "files_allowed": frontmatter.get("files_allowed", ""),
        "files_blocked": frontmatter.get("files_blocked", ""),
        "build_command": frontmatter.get("build_command", ""),
        "visual_reference": frontmatter.get("visual_reference", ""),
        "research_allowed": frontmatter.get("research_allowed", "").lower() in ("true", "yes", "1"),
        "cost_ceiling": cost_ceiling,
        "max_cost_usd": (
            _coerceFloat(frontmatter.get("max_cost_usd"), default_cost_ceiling)
            if frontmatter.get("max_cost_usd", "") not in (None, "")
            else None
        ),
        "resolved_cost_ceilings": {
            "planning": cost_ceiling,
            "execution": cost_ceiling,
            "learning": cost_ceiling,
        },
        "planning_cost_usd": _coerceFloat(frontmatter.get("planning_cost_usd"), 0.0),
        "execution_cost_usd": _coerceFloat(frontmatter.get("execution_cost_usd"), 0.0),
        "learning_cost_usd": _coerceFloat(frontmatter.get("learning_cost_usd"), 0.0),
        "existing_cost_usd": _coerceFloat(frontmatter.get("cost_estimate_usd"), 0.0),
        "snooze_until": frontmatter.get("snooze_until", ""),
        "initial_prompt": initial_prompt,
        "frontmatter": frontmatter,
    }


def _find_task_file(task_name: str, task_files_dir: Path | None = None) -> Path | None:
    """Find a task markdown file by task name, where task name is the file stem."""
    if not task_name or any(separator in task_name for separator in ("/", "\\")):
        return None

    task_dir = task_files_dir or (_vault_root_path() / "task_files")
    direct_path = task_dir / f"{task_name}.md"
    if direct_path.is_file():
        return direct_path

    if not task_dir.exists():
        return None
    for task_path in sorted(task_dir.glob("*.md")):
        if task_path.stem == task_name:
            return task_path
    return None


def _set_frontmatter_value(path: Path, key: str, value: str) -> None:
    """Update or insert a top-level frontmatter key while preserving task body."""
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    has_trailing_newline = text.endswith("\n")

    if lines and lines[0].strip() == "---":
        end_index = None
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                end_index = i
                break
        if end_index is not None:
            updated = False
            for i in range(1, end_index):
                current_key, separator, _ = lines[i].partition(":")
                if separator and current_key.strip() == key:
                    lines[i] = f"{key}: {value}"
                    updated = True
                    break
            if not updated:
                lines.insert(end_index, f"{key}: {value}")
            output = "\n".join(lines)
            if has_trailing_newline:
                output += "\n"
            path.write_text(output, encoding="utf-8")
            return

    output = f"---\n{key}: {value}\n---\n"
    if text:
        output += text
        if has_trailing_newline and not output.endswith("\n"):
            output += "\n"
    path.write_text(output, encoding="utf-8")


def _parse_snooze_until(raw_value: str, task_name: str = "") -> datetime | None:
    if not raw_value:
        return None
    try:
        snooze_until = datetime.fromisoformat(raw_value)
    except ValueError:
        label = f" for {task_name}" if task_name else ""
        print(f"{YELLOW}[daemon] malformed snooze_until{label}: {raw_value}{RESET}")
        return None
    if snooze_until.tzinfo is None:
        label = f" for {task_name}" if task_name else ""
        print(f"{YELLOW}[daemon] snooze_until lacks timezone{label}: {raw_value}{RESET}")
        return None
    return snooze_until


def _active_snooze_until(spec: dict, now: datetime | None = None) -> datetime | None:
    current_time = now or datetime.now().astimezone()
    snooze_until = _parse_snooze_until(spec.get("snooze_until", ""), spec.get("name", ""))
    if snooze_until and snooze_until > current_time:
        return snooze_until
    return None


def _snoozed_tasks_summary(snoozed_tasks: list[tuple[str, datetime]]) -> str:
    task_count = len(snoozed_tasks)
    plural = "" if task_count == 1 else "s"
    entries = ", ".join(f"{name} until {snooze_until.strftime('%H:%M')}"
                        for name, snooze_until in snoozed_tasks)
    next_wake = min(snooze_until for _, snooze_until in snoozed_tasks).strftime("%H:%M")
    return f"{task_count} snoozed task{plural}: {entries}; next wake {next_wake}"


def cmd_snooze(args):
    """Persist a task snooze timestamp so daemon pickup skips it until expiration."""
    if args.hours <= 0:
        print(f"{RED}Error:{RESET} hours must be positive")
        return 1

    task_path = _find_task_file(args.task)
    if task_path is None:
        print(f"{RED}Error:{RESET} task not found: {args.task}")
        return 1

    snooze_until = datetime.now().astimezone() + timedelta(hours=args.hours)
    _set_frontmatter_value(task_path, "snooze_until", snooze_until.isoformat(timespec="seconds"))
    print(f"Task '{task_path.stem}' snoozed until {snooze_until.strftime('%H:%M')}")
    return 0


def _start_task_from_file(task_path: Path) -> bool:
    """Launch the graph for a new task picked up from task_files/.

    Returns True if we actually started the task; False if it was skipped
    (e.g. already running, malformed, etc).
    """
    spec = _parse_task_file(task_path)
    task_name = spec["name"]

    # Skip if already in checkpoint DB (already started)
    with checkpointer() as saver:
        if _latest_state(saver, task_name) is not None:
            return False

    # Build initial state from the task file
    state = initial_state(
        task_name=task_name,
        task_file_path=str(task_path.resolve()),
        initial_prompt=spec["initial_prompt"],
        app_location=spec["app_location"],
        files_allowed=spec["files_allowed"],
        files_blocked=spec["files_blocked"],
        build_command=spec["build_command"],
        visual_reference=spec["visual_reference"],
        research_allowed=spec["research_allowed"],
        planning_cost_usd=spec["planning_cost_usd"],
        execution_cost_usd=spec["execution_cost_usd"],
        learning_cost_usd=spec["learning_cost_usd"],
        existing_cost_usd=spec["existing_cost_usd"],
    )
    for ceiling_field in ("cost_ceiling", "max_cost_usd"):
        if spec.get(ceiling_field) is not None:
            state[ceiling_field] = spec[ceiling_field]

    print(f"{CYAN}[daemon]{RESET} Starting {task_name}  {DIM}from {task_path.name}{RESET}")
    with checkpointer() as saver:
        graph = compile_graph(saver)
        try:
            for _ in graph.stream(state, config=_config(task_name)):
                pass
        except Exception as e:
            print(f"{RED}[daemon] {task_name} crashed: {e}{RESET}")
            return False

    with checkpointer() as saver:
        final = _latest_state(saver, task_name) or {}
    print(f"{GREEN}[daemon] {task_name} halted at:{RESET} "
          f"{final.get('current_phase', '?')} ({final.get('status', '?')})")
    return True


def _crash_recovery_cleanup(vault_root: Path):
    """Clean up any stale state from a previous crashed run.

    Runs once at daemon startup. Catches:
      - Stale hook scope file (hooks/.scope.json) — leftover from a crashed
        execution; would otherwise enforce wrong restrictions on next task
      - Orphaned .lock files — file locks not released because the holder
        died. Anything older than 60 seconds is fair game to clean.
      - Stale .project-snapshot.md files in app dirs (less critical, but tidy)

    Idempotent — safe to run on every startup.
    """
    import time as _time
    cleaned = []

    # 1. Clear hook scope (no task is in_progress when daemon starts)
    hook_scope = vault_root / "scripts" / "hooks" / ".scope.json"
    if hook_scope.exists():
        try:
            text = hook_scope.read_text(encoding="utf-8").strip()
            if text and text != "{}":
                hook_scope.write_text("{}", encoding="utf-8")
                cleaned.append("hooks/.scope.json (cleared)")
        except Exception:
            pass

    # 2. Remove stale lock files orphaned by crashes.
    # We ONLY touch locks our `_fileLock` helper creates — these have a sibling
    # file with the same name minus `.lock` (e.g. `cost_log.jsonl.lock` next to
    # `cost_log.jsonl`). This excludes package manager lock files (yarn.lock,
    # flake.lock, package-lock.json) which are standalone files.
    # Scope: vault root + scripts/ + logs/ + .state/ — never code/ (user apps).
    now = _time.time()
    scan_dirs = [vault_root, vault_root / "scripts", vault_root / "logs", vault_root / ".state"]
    seen = set()
    for scan_dir in scan_dirs:
        if not scan_dir.exists():
            continue
        for lock_file in scan_dir.rglob("*.lock"):
            if lock_file in seen:
                continue
            seen.add(lock_file)
            # Skip user app code
            try:
                rel = lock_file.relative_to(vault_root)
            except ValueError:
                continue
            if str(rel).replace("\\", "/").startswith("code/"):
                continue
            # Only touch locks that have a sibling file (our _fileLock pattern)
            sibling = lock_file.with_suffix("")
            if not sibling.exists():
                continue
            try:
                age = now - lock_file.stat().st_mtime
                if age > 60:
                    lock_file.unlink()
                    cleaned.append(f"{rel} (age {int(age)}s)")
            except (OSError, ValueError):
                pass

    if cleaned:
        print(f"{DIM}  startup cleanup: removed {len(cleaned)} stale artifact(s){RESET}")
        for item in cleaned[:5]:
            print(f"    {DIM}↳ {item}{RESET}")
        if len(cleaned) > 5:
            print(f"    {DIM}↳ ... and {len(cleaned) - 5} more{RESET}")


def cmd_daemon(args):
    """Poll task_files/ continuously, launching graph for new pending tasks.

    Replaces the old ai_dougs.py daemon role. Sharpener still routes complex
    prompts to task_files/auto_NNNN.md as before; this daemon picks them up.
    Runs crash-recovery cleanup once on startup.
    """
    vault_root = _vault_root_path()
    task_files_dir = vault_root / "task_files"
    poll_interval = args.poll_interval

    print(f"{BOLD}vault daemon {SCRIPT_VERSION}{RESET}")
    print(f"  watching: {task_files_dir}")
    print(f"  poll interval: {poll_interval}s")

    unresponsiveAfterSeconds = max(30, int(poll_interval) * 3)
    daemonHealth.recordHeartbeat(
        vault_root, "executor", "Executor daemon", os.getpid(), unresponsiveAfterSeconds
    )

    _crash_recovery_cleanup(vault_root)

    # Watchdog: tree-kills wedged CLI subprocs (runs in background thread)
    try:
        from . import watchdog
        watchdog.start_watchdog_thread()
        print(f"  {DIM}watchdog thread started (checks every {watchdog.POLL_INTERVAL_SEC}s){RESET}")
    except Exception as e:
        print(f"  {DIM}watchdog failed to start (non-fatal): {e}{RESET}")

    print(f"  Ctrl+C to stop\n")

    import time as _time
    last_snoozed_snapshot = ()
    try:
        while True:
            daemonHealth.recordHeartbeat(
                vault_root, "executor", "Executor daemon", os.getpid(), unresponsiveAfterSeconds
            )
            try:
                rotate_logs(vault_root)
            except Exception as e:
                print(f"{YELLOW}[daemon] log rotation failed (non-fatal): {e}{RESET}")
            snoozed_tasks = []
            if task_files_dir.exists():
                for task_path in sorted(task_files_dir.glob("*.md")):
                    spec = _parse_task_file(task_path)
                    if spec["status"] == "pending_ai_planning":
                        snooze_until = _active_snooze_until(spec)
                        if snooze_until:
                            snoozed_tasks.append((spec["name"], snooze_until))
                            continue
                        _start_task_from_file(task_path)
            snoozed_snapshot = tuple((name, snooze_until.isoformat())
                                     for name, snooze_until in snoozed_tasks)
            if snoozed_snapshot != last_snoozed_snapshot:
                if snoozed_tasks:
                    print(f"{DIM}[daemon] {_snoozed_tasks_summary(snoozed_tasks)}{RESET}")
                elif last_snoozed_snapshot:
                    print(f"{DIM}[daemon] no active snoozed tasks{RESET}")
                last_snoozed_snapshot = snoozed_snapshot
            _time.sleep(poll_interval)
    except KeyboardInterrupt:
        print(f"\n{DIM}daemon stopped.{RESET}")
        return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="vault",
        description=f"vault {SCRIPT_VERSION} — LangGraph-backed task orchestration",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="show all tasks").set_defaults(fn=cmd_list)

    p_show = sub.add_parser("show", help="print full state for one task")
    p_show.add_argument("task")
    p_show.add_argument("--plan-vs-execution", action="store_true",
                        help="compare planned changes with execution output")
    p_show.set_defaults(fn=cmd_show)

    p_inspect = sub.add_parser("inspect", help="show recent checkpoint history")
    p_inspect.add_argument("task")
    p_inspect.add_argument("--limit", type=int, default=15)
    p_inspect.set_defaults(fn=cmd_inspect)

    p_new = sub.add_parser("new", help="create + start a new task")
    p_new.add_argument("task")
    p_new.add_argument("prompt_file")
    p_new.set_defaults(fn=cmd_new)

    p_adv = sub.add_parser("advance", help="resume a halted task")
    p_adv.add_argument("task")
    p_adv.add_argument("--answers", help="human answers (used at wait_human_answers)")
    p_adv.add_argument("--approve", action="store_true",
                       help="approve plan (used at wait_human_approval)")
    p_adv.add_argument("--feedback", help="human feedback (used at wait_human_review)")
    p_adv.set_defaults(fn=cmd_advance)

    p_rerun = sub.add_parser("rerun", help="resume a failed task from last successful checkpoint")
    p_rerun.add_argument("task")
    p_rerun.set_defaults(fn=cmd_rerun)

    p_replay = sub.add_parser("replay",
                              help="re-run planning for a completed historical task to test prompt changes",
                              description="re-run planning for a completed historical task to test prompt changes")
    p_replay.add_argument("task")
    p_replay.set_defaults(fn=cmd_replay)

    p_snooze = sub.add_parser(
        "snooze",
        help="pause daemon pickup for a task",
        description="pause daemon pickup for a task until the timestamp expires",
    )
    p_snooze.add_argument("task", metavar="<task>")
    p_snooze.add_argument("--hours", type=int, required=True,
                          help="positive integer hours to snooze")
    p_snooze.set_defaults(fn=cmd_snooze)

    p_del = sub.add_parser("delete", help="delete all checkpoints for a task")
    p_del.add_argument("task")
    p_del.add_argument("--yes", action="store_true", help="skip confirmation")
    p_del.set_defaults(fn=cmd_delete)

    p_budget = sub.add_parser("budget", help="show spending analytics from cost_log.jsonl")
    p_budget.add_argument("--days", type=int, help="rolling window in days, anchored at the newest log entry")
    p_budget.add_argument("--since", help="start date, YYYY-MM-DD")
    p_budget.add_argument("--until", help="end date, YYYY-MM-DD")
    p_budget.add_argument("--top", type=int, default=5, help="rows to show in top sections (default 5)")
    p_budget.add_argument("--limit", type=int, default=5000, help="newest cost-log entries to scan (default 5000)")
    p_budget.add_argument("--json", action="store_true", help="print machine-readable JSON")
    p_budget.set_defaults(fn=cmd_budget)

    sub.add_parser("status", help="show daemon health, recent phase events, and stale tasks").set_defaults(fn=cmd_status)
    sub.add_parser("models", help="show provider quota-ban status and recent successful models").set_defaults(fn=cmd_models)

    sub.add_parser("skill-stats",
                   help="report skill load/use/helpful stats from logs/skill_usage.jsonl"
                   ).set_defaults(fn=cmd_skill_stats)

    p_prune_skills = sub.add_parser("prune-skills",
                                    help="summarize active SKILL.md curation fitness")
    pruneMode = p_prune_skills.add_mutually_exclusive_group()
    pruneMode.add_argument("--dry-run", action="store_true",
                           help="report damaged skills without archiving them (default)")
    pruneMode.add_argument("--apply", action="store_true",
                           help="archive damaged active skills under ai_skills/_archive/")
    p_prune_skills.set_defaults(fn=cmd_prune_skills)

    # refresh-docs subcommand removed 2026-05-12 with STATUS.md.

    # health: one-shot honest report on adaptive routing, self-test efficacy,
    # skill accumulation, failure-mode growth, and known open issues.
    from .health import cmd_health as _cmd_health
    sub.add_parser("health",
                   help="print honest report on what's working / what's broken").set_defaults(fn=_cmd_health)

    # diagnostics: read-only inspector for cost_log, events, failure_modes,
    # skill_usage, and checkpoint state. Four sub-commands: summary (default),
    # task <name>, search <query>, live.
    from .diagnostics import cmd_diagnostics as _cmd_diagnostics
    p_diag = sub.add_parser("diagnostics",
                            help="read-only inspector for vault state")
    diag_sub = p_diag.add_subparsers(dest="sub")
    p_diag_summary = diag_sub.add_parser("summary", help="one-screen overview (default)")
    p_diag_summary.add_argument("--days", type=int, default=7,
                                help="window in days (default 7)")
    p_diag_task = diag_sub.add_parser("task", help="deep dive on one task")
    p_diag_task.add_argument("task_name", help="task name (e.g. auto_0076)")
    p_diag_search = diag_sub.add_parser("search", help="pattern search across sources")
    p_diag_search.add_argument("query", help="substring to search for")
    diag_sub.add_parser("live", help="snapshot of in-flight tasks")
    p_diag.set_defaults(fn=_cmd_diagnostics, sub=None)

    p_daemon = sub.add_parser("daemon", help="poll task_files/ and launch new tasks")
    p_daemon.add_argument("--poll-interval", type=int, default=10,
                          help="seconds between polls (default 10)")
    p_daemon.set_defaults(fn=cmd_daemon)

    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
