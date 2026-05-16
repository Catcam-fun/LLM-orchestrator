"""Sqlite checkpointer setup.

LangGraph automatically persists state at every node boundary via the
checkpointer. We use sqlite at vault/.state/checkpoints.sqlite so:
  - state survives crashes / process restarts
  - one task per thread_id (the task_name)
  - inspectable via standard sqlite tools if needed
  - machine-local (gitignored, not synced — runtime state shouldn't sync)
"""
from contextlib import contextmanager
from pathlib import Path
import sqlite3

from langgraph.checkpoint.sqlite import SqliteSaver


def _vault_root() -> Path:
    """Resolve vault root from this file's location."""
    return Path(__file__).resolve().parent.parent.parent


def checkpoint_db_path() -> Path:
    """Where the sqlite checkpoint database lives. Created on first use."""
    db_dir = _vault_root() / ".state"
    db_dir.mkdir(parents=True, exist_ok=True)
    return db_dir / "checkpoints.sqlite"


@contextmanager
def checkpointer():
    """Context manager yielding a SqliteSaver bound to the vault checkpoint DB.

    Usage:
        with checkpointer() as saver:
            graph = build_graph().compile(checkpointer=saver)
            graph.invoke(state, config={"configurable": {"thread_id": task_name}})
    """
    db_path = str(checkpoint_db_path())
    # check_same_thread=False because LangGraph may access the connection
    # from multiple threads (e.g. when streaming). Sqlite is single-writer
    # but multi-reader; this is the standard pattern for SqliteSaver.
    conn = sqlite3.connect(db_path, check_same_thread=False)
    try:
        yield SqliteSaver(conn)
    finally:
        conn.close()
