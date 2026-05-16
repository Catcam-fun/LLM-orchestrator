"""vault_graph — LangGraph-based orchestration for the vault.

Replaces the previous markdown-status-driven pipeline (ai_dougs.py) with a
state-machine where:
  - state lives in sqlite checkpoints (vault/.state/checkpoints.sqlite)
  - human gates are interrupt points the human resumes via `python vault.py`
  - phases are nodes with conditional edges
  - crash recovery is automatic via the checkpointer

See VISION.md for the broader why. This package contains:
  - state.py        — TaskState TypedDict (the schema flowing through the graph)
  - checkpointer.py — sqlite persistence setup
  - nodes/*.py      — one file per phase node
  - graph.py        — graph definition, node wiring, edge logic
  - cli.py          — `vault.py` CLI implementation
  - ported.py       — helpers ported from ai_dougs.py (cost log, file locks, etc.)

Current status: every phase is REAL. See graph.py docstring for the pipeline.
"""
SCRIPT_VERSION = "0.4.0"  # 2026-05-11 - judge arbitration parity, GAP fixes
