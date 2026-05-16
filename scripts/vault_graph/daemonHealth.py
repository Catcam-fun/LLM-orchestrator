"""Shared daemon heartbeat state for vault operational status."""
from __future__ import annotations

import ctypes
import errno
import json
import os
from datetime import datetime
from pathlib import Path


def healthStatePath(vaultRoot: Path) -> Path:
    return Path(vaultRoot) / ".state" / "daemon_health.json"


def utcTimestamp() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def parseUtcTimestamp(value: str) -> datetime | None:
    if not value:
        return None
    try:
        if value.endswith("Z"):
            value = value[:-1]
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def secondsSince(value: str, now: datetime | None = None) -> int | None:
    timestamp = parseUtcTimestamp(value)
    if timestamp is None:
        return None
    now = now or datetime.utcnow()
    return max(0, int((now - timestamp).total_seconds()))


def readHealthState(vaultRoot: Path) -> dict:
    path = healthStatePath(vaultRoot)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def writeHealthState(vaultRoot: Path, state: dict) -> None:
    path = healthStatePath(vaultRoot)
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(state, indent=2, sort_keys=True)
    if not content.endswith("\n"):
        content += "\n"
    # Bug O fix (2026-05-10): use a per-writer tmp filename so concurrent
    # daemons (sharpener + executor) don't collide on the SAME `.tmp` file.
    # Previously both daemons wrote to `daemon_health.tmp`, and on Windows
    # the second writer would hit PermissionError mid-write_text() because
    # the first writer still had the file open. The .replace() retry loop
    # only handled destination contention, not source contention. PID makes
    # the tmp filename unique per writing process.
    import os as _os
    tmp = path.with_suffix(f".{_os.getpid()}.tmp")
    # Wrap write_text in retry too — even with per-pid filenames, a stale
    # .tmp from a killed process could rarely interfere; better to defend.
    import time as _time
    write_err = None
    for attempt in range(5):
        try:
            tmp.write_text(content, encoding="utf-8")
            write_err = None
            break
        except PermissionError as e:
            write_err = e
            _time.sleep(0.2 * (attempt + 1))
    if write_err:
        raise write_err
    # Windows-safe atomic replace: if the destination is being read by another
    # process (vault.py status, file scanner), os.replace raises PermissionError.
    # Retry a few times with short backoff. Heartbeat is a soft-realtime signal
    # so a 1-2s delay before write is fine.
    last_err = None
    for attempt in range(5):
        try:
            tmp.replace(path)
            return
        except PermissionError as e:
            last_err = e
            _time.sleep(0.2 * (attempt + 1))
    # Final attempt — if this fails, raise (caller handles)
    try:
        tmp.replace(path)
    except PermissionError:
        # Last resort: try to remove tmp so it doesn't accumulate, then re-raise
        try:
            tmp.unlink()
        except Exception:
            pass
        raise last_err


def recordHeartbeat(
    vaultRoot: Path,
    daemon: str,
    role: str,
    pid: int | None = None,
    unresponsiveAfterSeconds: int | None = None,
) -> dict:
    state = readHealthState(vaultRoot)
    existing = state.get(daemon)
    if not isinstance(existing, dict):
        existing = {}
    now = utcTimestamp()
    entry = {
        "pid": int(pid if pid is not None else os.getpid()),
        "daemon": daemon,
        "role": role,
        "lastHeartbeat": now,
        "startedAt": existing.get("startedAt") or now,
    }
    if unresponsiveAfterSeconds is not None:
        entry["unresponsiveAfterSeconds"] = int(unresponsiveAfterSeconds)
    state[daemon] = entry
    writeHealthState(vaultRoot, state)
    return entry


def isProcessRunning(pid) -> bool:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        processQueryLimitedInformation = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            processQueryLimitedInformation, False, pid
        )
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        return exc.errno == errno.EPERM
