"""Subprocess and supervisor registry helpers for ai_dougs."""

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

SCRIPT_VERSION = "1.0"  # 2026-05-10 - extracted subprocess helpers from ai_dougs.py

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
CYAN = "\033[95m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[31m"
WHITE = "\033[97m"
MAGENTA = "\033[35m"

W = 50

_ANIM_COLORS = [CYAN, GREEN, YELLOW, MAGENTA, WHITE]
_SPINNER = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
_WAVE = "▁▂▃▄▅▆▇█▇▆▅▄▃▂▁"

_ACTIVE_SUBPROC_LOCK = threading.Lock()
_AI_CLI_NAMES = ("claude", "codex", "gemini", "agent")


def _kill_process_tree(pid):
    """Kill a process and all its children. Windows-safe.

    Standard subprocess.run timeout calls .kill() which only terminates the
    direct parent. For .cmd / .bat wrappers (gemini, codex, claude on Windows),
    the actual work runs in child node.exe processes that survive. This walks
    the process tree and kills everything.
    """
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                timeout=5,
            )
        except Exception:
            pass
    else:
        try:
            os.killpg(os.getpgid(pid), 9)
        except Exception:
            pass


def _active_subproc_file() -> Path:
    root = Path(__file__).resolve().parent.parent.parent
    return root / "logs" / "active_subprocesses.json"


def _registerActiveSubproc(pid: int, label: str, cmd_head: str) -> None:
    """Add an in-flight CLI subprocess to the on-disk registry.

    Allows the supervisor to enumerate live CLI PIDs and tree-kill them when a
    phase blows past its stuck threshold. Best-effort failures here must never
    block the actual call.
    """
    try:
        path = _active_subproc_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        with _ACTIVE_SUBPROC_LOCK:
            try:
                data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            except Exception:
                data = {}
            data[str(pid)] = {
                "label": label,
                "cmd": cmd_head,
                "started_at": time.time(),
                "owner_pid": os.getpid(),
            }
            path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass


def _deregisterActiveSubproc(pid: int) -> None:
    try:
        path = _active_subproc_file()
        if not path.exists():
            return
        with _ACTIVE_SUBPROC_LOCK:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                return
            data.pop(str(pid), None)
            path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass


def _is_ai_cli_command(cmd) -> bool:
    """Detect whether cmd is invoking one of the AI CLI subprocess agents.

    Used by _run_with_tree_kill_timeout to mark subprocess agents with
    VAULT_SUBPROCESS_AGENT=1 in their env. The hook script
    `_human_notes/session_start_notes_reminder.py` reads that flag and
    suppresses its reminder so subprocess agents don't see orchestrator-only
    notes. The pre_read.py hook reads it to mechanically refuse reads of
    `_human_notes/`. (Hook follow-ups, 2026-05-09.)
    """
    if not cmd:
        return False
    head = cmd[0] if isinstance(cmd, (list, tuple)) else str(cmd)
    name = Path(str(head)).stem.lower()
    return name in _AI_CLI_NAMES


def _run_with_tree_kill_timeout(cmd, timeout, **kwargs):
    """Run a command with a true tree-kill timeout (manual Popen + poll).

    Returns either CompletedProcess or TimeoutExpired exception.
    Translates `capture_output=True` and `input=...` from run() semantics
    to Popen + communicate() since Popen doesn't accept those directly.
    """
    if kwargs.pop("capture_output", False):
        kwargs.setdefault("stdout", subprocess.PIPE)
        kwargs.setdefault("stderr", subprocess.PIPE)
    input_data = kwargs.pop("input", None)

    if _is_ai_cli_command(cmd) and "env" not in kwargs:
        agent_env = os.environ.copy()
        agent_env["VAULT_SUBPROCESS_AGENT"] = "1"
        kwargs["env"] = agent_env
    if input_data is not None:
        kwargs.setdefault("stdin", subprocess.PIPE)

    proc = subprocess.Popen(cmd, **kwargs)
    try:
        cmd_head = " ".join(str(c) for c in (cmd[:3] if isinstance(cmd, (list, tuple)) else [cmd]))
    except Exception:
        cmd_head = "?"
    _registerActiveSubproc(proc.pid, label=cmd_head[:80], cmd_head=cmd_head)
    try:
        stdout, stderr = proc.communicate(input=input_data, timeout=timeout)
        return subprocess.CompletedProcess(
            cmd,
            proc.returncode,
            stdout=stdout,
            stderr=stderr,
        )
    except subprocess.TimeoutExpired:
        _kill_process_tree(proc.pid)
        try:
            stdout, stderr = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            stdout, stderr = "", ""
        return subprocess.TimeoutExpired(cmd, timeout, output=stdout, stderr=stderr)
    finally:
        _deregisterActiveSubproc(proc.pid)


def run_with_animation(cmd, label, **kwargs):
    """Run command with animated spinner while executing.

    A timeout is always enforced (default 600s) so a hung CLI cannot block the
    pipeline forever. Pass `timeout=N` in kwargs to override per-call.
    """
    timeout = kwargs.pop("timeout", 600)

    if not sys.stdout.isatty():
        print(f"    {DIM}-> running...{RESET}", flush=True)
        try:
            return _run_with_tree_kill_timeout(cmd, timeout, **kwargs)
        except subprocess.TimeoutExpired as e:
            print(f"    {RED}[TIMEOUT] CLI exceeded {timeout}s - aborting{RESET}")
            return e
        except Exception as e:
            return e

    result_box = [None]
    done = threading.Event()

    def worker():
        try:
            result_box[0] = _run_with_tree_kill_timeout(cmd, timeout, **kwargs)
        except subprocess.TimeoutExpired as e:
            result_box[0] = e
        except Exception as e:
            result_box[0] = e
        finally:
            done.set()

    threading.Thread(target=worker, daemon=True).start()

    short = (label[:38] + "...") if len(label) > 38 else label
    wave_width = W - 4
    start = time.time()
    frame = 0

    sys.stdout.write("\n")
    sys.stdout.flush()

    while not done.is_set():
        elapsed = int(time.time() - start)
        m, s = divmod(elapsed, 60)
        color = _ANIM_COLORS[(frame // 6) % len(_ANIM_COLORS)]
        spin = _SPINNER[frame % len(_SPINNER)]
        wave = "".join(_WAVE[(frame + j) % len(_WAVE)] for j in range(wave_width))
        line1 = f"  {color}{BOLD}{spin}{RESET}  {DIM}{short}{RESET}  {color}[{m}:{s:02d}]{RESET}"
        line2 = f"  {color}{wave}{RESET}"
        sys.stdout.write(f"\033[1A\033[2K\r{line1}\n\033[2K\r{line2}")
        sys.stdout.flush()
        frame += 1
        time.sleep(0.07)

    sys.stdout.write("\033[1A\033[2K\r\n\033[2K\r\033[1A\r")
    sys.stdout.flush()

    return result_box[0]
