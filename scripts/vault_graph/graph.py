"""Graph definition — wires nodes + edges into a compiled state machine.

Pipeline shape (matches the existing vault flow):

    task_entry
        │
        ▼
    planning ─────────► plan_review ──────► wait_human_answers
                                               │ (interrupt)
                                               ▼ (human resumes)
                                            refinement
                                               │
                                               ▼
                                        wait_human_approval
                                               │ (interrupt)
                                               ▼ (human resumes)
                                           execution
                                               │
                                               ▼
                                          build_gate
                                               │
                                               ▼
                                        wait_human_review
                                               │ (interrupt)
                                               ▼ (human resumes)
                                            learning
                                               │
                                               ▼
                                       terminal_complete

The interrupt points (wait_*) halt graph execution. State is checkpointed.
The human resumes via `python vault.py advance <task>` which calls graph.invoke
with the existing thread_id — LangGraph picks up exactly where it left off.

GAP #2 (2026-05-11): execution → refinement is now a valid conditional
edge. When execution_judge returns NEEDS_REVISION (and the cap hasn't been
hit), the graph routes back to refinement with judge concerns injected.
After JUDGE_REVISION_CAP attempts it falls through to wait_human_review.

All nodes are REAL. The stubs.py module retains a few live helpers (the
wait_human_* nodes + terminal_complete + sharpener-outcome writer) plus
some legacy Day-1 stub functions that are NOT imported anywhere (queued
for dead-code cleanup).
"""
from langgraph.graph import StateGraph, START, END

from .state import TaskState
from .nodes import (
    stubs,
    planning as planning_module,
    execution as execution_module,
    refinement as refinement_module,
)


def _route_after_task_entry(state: TaskState) -> str:
    """If pre-flight failed, route directly to terminal_complete (halt)."""
    if state.get("status") in ("failed", "budget_exceeded"):
        return "terminal_complete"
    return "planning"


def _route_after_planning(state: TaskState) -> str:
    """If planning failed, halt. Otherwise proceed to plan_review (which
    will internally decide whether to actually run or skip)."""
    if state.get("status") in ("failed", "budget_exceeded"):
        return "terminal_complete"
    return "plan_review"


# Cap on judge-driven refinement loops. Two revisions has surfaced as a
# sensible default: it lets the judge ask for one concrete fix, then one
# follow-up if the fix didn't fully land, then hands off to human review.
# Higher caps would risk infinite loops in adversarial cases where the
# judge's complaint and the agent's interpretation cannot converge.
# JUDGE_REVISION_CAP removed 2026-05-12. Auto route-back on NEEDS_REVISION
# was retired with the judge-as-advisor redesign: the judge's concerns now
# surface to the user at the wait_human_review gate, and the user decides
# whether to merge, route back, or reject. See VISION.md and ai_main.md.


def _route_after_execution(state: TaskState) -> str:
    """Route after execution + judge.

    Status semantics:
      1. failed / budget_exceeded → terminal_complete
      2. anything else → wait_human_review

    2026-05-12: auto route-back on NEEDS_REVISION removed. The judge is an
    advisor; its concerns + scores live on state and surface to the user at
    wait_human_review alongside the diff and verification result. The user
    routes the work — to learning (ship it), to refinement (try again), or
    to terminal_complete (reject).
    """
    if state.get("status") in ("failed", "budget_exceeded"):
        return "terminal_complete"
    return "wait_human_review"


def _route_after_refinement(state: TaskState) -> str:
    """If refinement failed, halt. Otherwise proceed to human approval."""
    if state.get("status") in ("failed", "budget_exceeded"):
        return "terminal_complete"
    return "wait_human_approval"


def build_graph() -> StateGraph:
    """Build (but do NOT compile) the task pipeline graph.

    Returns the StateGraph builder. Call .compile(checkpointer=...) to get
    a runnable graph.

    Every phase is REAL:
      - task_entry: pre-flight linter + cost ceiling
      - planning + plan_review (arbitrating, score-weighted variant A/B):
          LLM with full helpers
      - execution: agent mode + build gate + auto-fix + visual val + judge
      - refinement: LLM refines plan from human answers OR judge concerns
      - learning: extracts skills (additive + judge-arbitrated), project
          context, model perf, routing params
      - wait_*: interrupt points (no-op nodes that halt the graph)
      - terminal_complete: marks task done; gates on verification + judge
    """
    g = StateGraph(TaskState)

    # ── Nodes ─────────────────────────────────────────────────────────────────
    g.add_node("task_entry", planning_module.task_entry)
    g.add_node("planning", planning_module.planning)
    g.add_node("plan_review", planning_module.plan_review)
    g.add_node("refinement", refinement_module.refinement)
    g.add_node("execution", execution_module.execution)
    g.add_node("learning", refinement_module.learning)

    # Interrupt placeholders — minimal stubs that print + advance state
    g.add_node("wait_human_answers", stubs.wait_human_answers)
    g.add_node("wait_human_approval", stubs.wait_human_approval)
    g.add_node("wait_human_review", stubs.wait_human_review)
    g.add_node("terminal_complete", stubs.terminal_complete)

    # ── Edges ─────────────────────────────────────────────────────────────────
    g.add_edge(START, "task_entry")

    g.add_conditional_edges("task_entry", _route_after_task_entry, {
        "planning": "planning",
        "terminal_complete": "terminal_complete",
    })

    g.add_conditional_edges("planning", _route_after_planning, {
        "plan_review": "plan_review",
        "terminal_complete": "terminal_complete",
    })

    g.add_edge("plan_review", "wait_human_answers")
    g.add_edge("wait_human_answers", "refinement")

    # Conditional: halt if refinement failed
    g.add_conditional_edges("refinement", _route_after_refinement, {
        "wait_human_approval": "wait_human_approval",
        "terminal_complete": "terminal_complete",
    })

    g.add_edge("wait_human_approval", "execution")

    # Conditional after execution: halt on failure, otherwise human review.
    # (Auto route-back on judge NEEDS_REVISION removed 2026-05-12; judge is
    # an advisor at the wait_human_review gate, not a router.)
    g.add_conditional_edges("execution", _route_after_execution, {
        "wait_human_review": "wait_human_review",
        "terminal_complete": "terminal_complete",
    })

    g.add_edge("wait_human_review", "learning")
    g.add_edge("learning", "terminal_complete")
    g.add_edge("terminal_complete", END)

    return g


# ── Interrupt points ─────────────────────────────────────────────────────────
# These are the nodes where the graph halts and waits for the human.
# Listed here so the CLI can show the human what they're approving/blocking.
INTERRUPT_BEFORE = ["wait_human_answers", "wait_human_approval", "wait_human_review"]


def compile_graph(checkpointer):
    """Build + compile the graph with the given checkpointer + interrupt points."""
    return build_graph().compile(
        checkpointer=checkpointer,
        interrupt_before=INTERRUPT_BEFORE,
    )
