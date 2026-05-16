"""Vault backup helper for LangGraph execution attempts."""
from __future__ import annotations

import json
import os
import re
import tarfile
import tempfile
from datetime import datetime
from pathlib import Path

SCRIPT_VERSION = "1.1"  # 2026-05-10 - prune old vault backups after successful archive writes

BACKUP_EXCLUDE = {
    ".git",
    ".backups",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".cache",
    "dist",
    "build",
    "coverage",
    ".next",
    ".turbo",
    ".vite",
    ".venv",
    "venv",
    "env",
}

BACKUP_EXCLUDE_SUFFIXES = {
    ".pyc",
    ".pyo",
    ".log",
    ".tmp",
    ".temp",
}


def create_backup(vault_root, task_name: str) -> Path:
    """Create a compressed timestamped backup of the whole vault."""
    root = Path(vault_root).resolve()
    if not (root / "ai_main.md").exists():
        raise FileNotFoundError(f"vault root marker not found: {root / 'ai_main.md'}")

    safe_task_name = _safe_task_name(task_name)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    backup_dir = root / ".backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    final_path = backup_dir / f"{safe_task_name}_{timestamp}.tar.gz"

    temp_file = tempfile.NamedTemporaryFile(
        prefix=f".{safe_task_name}_{timestamp}_",
        suffix=".tar.gz.tmp",
        dir=backup_dir,
        delete=False,
    )
    temp_path = Path(temp_file.name)
    temp_file.close()

    try:
        with tarfile.open(temp_path, "w:gz") as archive:
            archive.add(root, arcname=root.name, filter=_tar_filter(root))
        os.replace(temp_path, final_path)
        prune_old_backups(backup_dir, _backup_retention_count(root))
        return final_path
    except Exception:
        try:
            temp_path.unlink(missing_ok=True)
        finally:
            raise


def prune_old_backups(backup_dir, retention_count: int) -> None:
    """Keep only the newest retention_count tarballs in backup_dir."""
    backup_path = Path(backup_dir)
    if retention_count < 1 or not backup_path.exists():
        return

    tarballs = [path for path in backup_path.glob("*.tar.gz") if path.is_file()]
    if len(tarballs) <= retention_count:
        return

    tarballs.sort(key=_backup_sort_key)
    for old_backup in tarballs[:-retention_count]:
        old_backup.unlink(missing_ok=True)


def should_backup_for_app_location(app_location: str | None, vault_root) -> bool:
    """Return True when execution is scoped to vault root infrastructure."""
    if not app_location or not str(app_location).strip():
        return True

    value = str(app_location).strip()
    normalized = value.replace("\\", "/").rstrip("/")
    if normalized in {".", "./"}:
        return True
    if normalized.startswith("code/"):
        return False

    root = Path(vault_root).resolve()
    try:
        candidate = (root / value).resolve()
    except OSError:
        return False
    return candidate == root


def _safe_task_name(task_name: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in str(task_name or "task"))
    cleaned = cleaned.strip("._")
    return cleaned or "task"


def _backup_retention_count(vault_root: Path) -> int:
    params_path = vault_root / "logs" / "routing_params.json"
    default_count = 5
    try:
        params = json.loads(params_path.read_text(encoding="utf-8"))
        value = int(params.get("backup_retention_count", default_count))
        return max(1, value)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return default_count


def _backup_sort_key(backup_path: Path):
    match = re.search(r"_(\d{4}-\d{2}-\d{2}_\d{6})\.tar\.gz$", backup_path.name)
    if match:
        return (0, match.group(1), backup_path.name)
    try:
        return (1, f"{backup_path.stat().st_mtime:020.6f}", backup_path.name)
    except OSError:
        return (2, backup_path.name, backup_path.name)


def _tar_filter(vault_root: Path):
    root = vault_root.resolve()

    def filter_entry(tar_info: tarfile.TarInfo) -> tarfile.TarInfo | None:
        path = root / Path(tar_info.name).relative_to(root.name)
        relative_parts = path.relative_to(root).parts
        if any(part in BACKUP_EXCLUDE for part in relative_parts):
            return None
        if path.suffix.lower() in BACKUP_EXCLUDE_SUFFIXES:
            return None
        return tar_info

    return filter_entry


if __name__ == "__main__":
    print(f"vault_backup {SCRIPT_VERSION}")
