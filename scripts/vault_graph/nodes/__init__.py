"""Phase nodes for the vault graph.

Every node is REAL. One module per phase:
  - planning.py    — task_entry, planning, plan_review (with variant A/B)
  - refinement.py  — refinement (human-driven OR judge-driven), learning
  - execution.py   — execution + verification + iteration loop + judge
  - stubs.py       — wait_human_* interrupt nodes + terminal_complete
                     (plus a few orphan Day-1 stub functions retained for
                      historical reference; not imported anywhere)

Each node is a function that takes TaskState and returns a partial dict of
updates. LangGraph merges the updates into the running state.
"""
