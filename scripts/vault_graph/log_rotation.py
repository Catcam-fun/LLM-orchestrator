"""Log lifecycle rotation and archival helpers for the vault daemon."""
from __future__ import annotations

import json
import re
import shutil
from datetime import date, datetime, timezone
from pathlib import Path

SCRIPT_VERSION = "1.0"  # 2026-05-10 - initial log lifecycle rotation helper

DEFAULT_LOG_PARAMS = {
    "cost_log_max_mb": 5,
    "log_archive_days": 7,
    "log_delete_days": 30,
}

LOG_DATE_PATTERN = re.compile(r"^(daemon|sharpener)_(\d{4}-\d{2}-\d{2})\.log$")


def rotate_logs(vault_root=None, params=None, today_utc=None) -> None:
    """Rotate cost_log.jsonl and archive/delete dated daemon logs."""
    root = Path(vault_root).resolve() if vault_root is not None else Path(__file__).resolve().parent.parent.parent
    effective_params = _load_params(root)
    if params:
        effective_params.update(params)
    today = _coerce_date(today_utc)

    logs_dir = root / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    rotate_cost_log(logs_dir / "cost_log.jsonl", effective_params["cost_log_max_mb"], today)
    _rotate_dated_logs(root, logs_dir, effective_params, today)


def rotate_cost_log(log_file, max_mb, today_utc=None) -> None:
    """Rotate active cost log when it exceeds max_mb."""
    active_log = Path(log_file)
    if not active_log.exists():
        return

    max_bytes = max(1, int(float(max_mb) * 1024 * 1024))
    try:
        if active_log.stat().st_size <= max_bytes:
            return
    except OSError:
        return

    today = _coerce_date(today_utc)
    archive_file = active_log.parent / f"cost_log-{today:%Y%m%d}.jsonl"
    archive_file.parent.mkdir(parents=True, exist_ok=True)

    if archive_file.exists():
        with open(archive_file, "ab") as archive:
            with open(active_log, "rb") as active:
                shutil.copyfileobj(active, archive)
        active_log.write_text("", encoding="utf-8")
    else:
        active_log.replace(archive_file)
        active_log.touch()


def _load_params(vault_root: Path) -> dict:
    params = dict(DEFAULT_LOG_PARAMS)
    params_file = vault_root / "logs" / "routing_params.json"
    try:
        loaded = json.loads(params_file.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            params.update({key: loaded[key] for key in DEFAULT_LOG_PARAMS if key in loaded})
    except (OSError, json.JSONDecodeError):
        pass
    return params


def _rotate_dated_logs(vault_root: Path, logs_dir: Path, params: dict, today: date) -> None:
    archive_days = int(params["log_archive_days"])
    delete_days = int(params["log_delete_days"])
    archive_dir = vault_root / "_archive" / "old_logs"

    for log_path in logs_dir.iterdir() if logs_dir.exists() else []:
        match = LOG_DATE_PATTERN.match(log_path.name)
        if not match or not log_path.is_file():
            continue
        try:
            log_date = datetime.strptime(match.group(2), "%Y-%m-%d").date()
        except ValueError:
            continue

        age_days = (today - log_date).days
        if age_days > delete_days:
            log_path.unlink(missing_ok=True)
        elif age_days > archive_days:
            archive_dir.mkdir(parents=True, exist_ok=True)
            destination = archive_dir / log_path.name
            if destination.exists():
                destination.unlink()
            shutil.move(str(log_path), str(destination))


def _coerce_date(value) -> date:
    if value is None:
        return datetime.now(timezone.utc).date()
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).date() if value.tzinfo else value.date()
    if isinstance(value, date):
        return value
    raise TypeError(f"today_utc must be date or datetime, got {type(value).__name__}")
