"""Watchdog — tree-kills wedged subprocess CLI calls.

Renamed from supervisor.py 2026-05-12. The historical "supervisor" had 8+
symptom detection rules that fed into agent retries; that entire mechanism
was retired in the dial-back. What remains is a focused watchdog: if a task's
active phase has been running past KILL_STUCK_MIN with a CLI subprocess alive
past SUBPROC_GRACE_MIN, tree-kill the subprocess so the daemon can recover.

This is the standard subprocess-watchdog pattern. It would have stopped the
rogue codex during auto_0033.
"""
from __future__ import annotations
import json
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

from . import ported as P


# Tunables
KILL_STUCK_MIN = 30      # phase older than this → kill in-flight CLI subprocs
POLL_INTERVAL_SEC = 300  # check every 5 minutes
SUBPROC_GRACE_MIN = 12   # only kill CLI subprocs alive longer than this

# Phases where halting is expected (waiting for human) — never flag these
INTERRUPT_PHASES = {"plan_review", "refinement", "wait_human_answers",
                    "wait_human_approval", "wait_human_review"}


def _vault_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def _events_log() -> Path:
    return _vault_root() / "logs" / "events.jsonl"


def _list_active_threads(db_path: Path) -> list[str]:
    """Read distinct thread_id values from the checkpoint DB."""
    if not db_path.exists():
        return []
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0) as conn:
            rows = conn.execute("SELECT DISTINCT thread_id FROM checkpoints").fetchall()
        return [r[0] for r in rows if r and r[0]]
    except Exception:
        return []


def _latest_state_for(db_path: Path, thread_id: str) -> dict:
    """Read the latest checkpointed state for thread_id (best-effort)."""
    if not db_path.exists():
        return {}
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0) as conn:
            row = conn.execute(
                "SELECT checkpoint FROM checkpoints WHERE thread_id = ? "
                "ORDER BY checkpoint_id DESC LIMIT 1",
                (thread_id,),
            ).fetchone()
        if not row or not row[0]:
            return {}
        cp = json.loads(row[0])
        state = cp.get("channel_values") if isinstance(cp, dict) else None
        return state if isinstance(state, dict) else {}
    except Exception:
        return {}


def _stuck_tasks(thread_states: dict[str, dict]) -> list[str]:
    """Return thread_ids whose active phase has been running past KILL_STUCK_MIN.

    Skips tasks at human gates and tasks in terminal states; those are
    expected to sit indefinitely.
    """
    stuck: list[str] = []
    for thread_id, state in thread_states.items():
        status = state.get("status", "")
        if status in ("completed", "failed", "budget_exceeded"):
            continue
        next_action = state.get("next_action", "")
        if (status in ("pending_human_answers", "pending_human_approval", "pending_human_review")
                or next_action in ("wait_human_answers", "wait_human_approval", "wait_human_review")):
            continue
        phase = state.get("current_phase", "")
        if phase in INTERRUPT_PHASES:
            continue
        phase_start = state.get("phase_start_time")
        if not phase_start:
            continue
        elapsed_min = (time.monotonic() - phase_start) / 60
        if elapsed_min >= KILL_STUCK_MIN:
            stuck.append(thread_id)
    return stuck


def _active_subproc_path() -> Path:
    return _vault_root() / "logs" / "active_subprocesses.json"


def _read_active_subprocs() -> dict:
    """Read the on-disk registry of active CLI subprocess PIDs (best-effort)."""
    path = _active_subproc_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _tree_kill(pid: int) -> bool:
    """Kill a process tree. Returns True on best-effort success.

    Mirrors `_kill_process_tree` in ai_dougs.py — cannot import that module
    here without creating a cycle, so we re-implement the platform check.
    """
    import sys as _sys
    if _sys.platform == "win32":
        try:
            import subprocess as _sp
            _sp.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                    capture_output=True, timeout=5)
            return True
        except Exception:
            return False
    else:
        try:
            import os as _os
            _os.killpg(_os.getpgid(pid), 9)
            return True
        except Exception:
            return False


def _kill_stuck_subprocs(stuck_task_ids: list[str]) -> list[dict]:
    """Tree-kill any registered CLI subproc alive > SUBPROC_GRACE_MIN.

    Called when one or more tasks have a phase older than KILL_STUCK_MIN.
    The registry doesn't bind PIDs to thread_ids, so the policy is: if any
    task is wedged for 30+ min AND a CLI subproc has been alive > grace,
    kill it.
    """
    registry = _read_active_subprocs()
    if not registry:
        return []

    killed: list[dict] = []
    now = time.time()
    for pid_str, info in list(registry.items()):
        try:
            pid = int(pid_str)
        except Exception:
            continue
        started = info.get("started_at", 0)
        alive_min = (now - started) / 60 if started else 0
        if alive_min < SUBPROC_GRACE_MIN:
            continue
        ok = _tree_kill(pid)
        killed.append({
            "pid": pid,
            "label": info.get("label", "?"),
            "alive_min": round(alive_min, 1),
            "killed_ok": ok,
            "owning_tasks": stuck_task_ids,
        })
        try:
            registry.pop(pid_str, None)
        except Exception:
            pass

    if killed:
        try:
            _active_subproc_path().write_text(json.dumps(registry, indent=2),
                                              encoding="utf-8")
        except Exception:
            pass

    return killed


def _log_kill(killed: list[dict], stuck_tasks: list[str]) -> None:
    """Append kill events to logs/events.jsonl (diagnostic only)."""
    if not killed:
        return
    log_file = _events_log()
    log_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        with P._fileLock(log_file, timeout=5.0):
            with open(log_file, "a", encoding="utf-8") as f:
                for k in killed:
                    entry = {
                        "timestamp": datetime.now().isoformat(timespec="seconds"),
                        "event": "watchdog_kill",
                        "task": ",".join(stuck_tasks),
                        "pid": k["pid"],
                        "label": k["label"],
                        "alive_min": k["alive_min"],
                        "details": f"tree-killed PID {k['pid']} ({k['label']}) "
                                   f"alive {k['alive_min']}min after stuck-task escalation",
                    }
                    f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


def _watchdog_pass():
    """One watchdog pass. Identify stuck tasks, tree-kill orphan subprocs."""
    from .checkpointer import checkpoint_db_path
    db_path = checkpoint_db_path()
    threads = _list_active_threads(db_path)
    if not threads:
        return

    states = {tid: _latest_state_for(db_path, tid) for tid in threads}
    states = {tid: s for tid, s in states.items() if s}
    stuck_ids = _stuck_tasks(states)
    if not stuck_ids:
        return

    killed = _kill_stuck_subprocs(stuck_ids)
    if killed:
        for k in killed:
            print(f"  {P.RED}[WATCHDOG-KILL] tree-killed PID {k['pid']} "
                  f"({k['label']}, alive {k['alive_min']}min) — "
                  f"owning task(s) {stuck_ids}{P.RESET}", flush=True)
        _log_kill(killed, stuck_ids)


def start_watchdog_thread() -> threading.Thread:
    """Launch the watchdog as a daemon thread inside the daemon process."""
    def loop():
        # Initial delay so we don't fire before the first task gets started
        time.sleep(POLL_INTERVAL_SEC)
        while True:
            try:
                _watchdog_pass()
            except Exception as e:
                print(f"  {P.YELLOW}[WATCHDOG] pass errored (non-fatal): {e}{P.RESET}",
                      flush=True)
            time.sleep(POLL_INTERVAL_SEC)

    t = threading.Thread(target=loop, daemon=True, name="watchdog")
    t.start()
    return t
