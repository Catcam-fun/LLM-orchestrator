#!/usr/bin/env python3
"""
session_digest.py - Compile a daily/session digest of vault activity.

Aggregates the day's logs into a structured markdown report stored at
`vault/session_logs/YYYY-MM-DD.md`. Loaded by future sessions for context.

Sources:
- logs/cost_log.jsonl       (model routing, cost, time per phase)
- failure_modes.md          (cross-task failure patterns)
- task_files/*.md           (completed/in-progress task statuses)
- prompt_archive/*/         (which prompts were sent — counts only)

Usage:
    python scripts/session_digest.py                  # today's digest
    python scripts/session_digest.py 2026-05-02       # specific date
    python scripts/session_digest.py --range 7        # last 7 days

Exit code 0 = digest written. Non-zero = error.
"""
import json
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

# Force UTF-8 on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SCRIPT_VERSION = "1.0"  # 2026-05-03 - initial

RESET, GREEN, DIM, BOLD = "\033[0m", "\033[32m", "\033[2m", "\033[1m"


def _vault_root():
    return Path(__file__).resolve().parent.parent


def _parseLogEntries(log_file, target_date):
    """Yield cost_log entries matching target_date (YYYY-MM-DD)."""
    if not log_file.exists():
        return
    with open(log_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                if entry.get("date") == target_date:
                    yield entry
            except json.JSONDecodeError:
                continue


def _collectTaskStatuses(task_files_dir):
    """Group task files by current status."""
    by_status = defaultdict(list)
    if not task_files_dir.exists():
        return by_status
    for tf in task_files_dir.glob("*.md"):
        try:
            content = tf.read_text(encoding="utf-8")
            # Cheap frontmatter parse — find status: line
            for line in content.split("\n")[:30]:
                if line.startswith("status:"):
                    status = line.split(":", 1)[1].strip()
                    by_status[status].append(tf.name)
                    break
        except Exception:
            continue
    return by_status


def _collectFailures(failure_modes_path, target_date):
    """Extract failure entries from failure_modes.md matching target_date."""
    if not failure_modes_path.exists():
        return []
    try:
        content = failure_modes_path.read_text(encoding="utf-8")
    except Exception:
        return []
    matches = []
    # Entries start with `## YYYY-MM-DD HH:MM`
    for chunk in content.split("\n## "):
        if chunk.startswith(target_date):
            matches.append("## " + chunk.strip())
    return matches


def _summarizeRouting(entries):
    """Per-(provider, model, phase): count, success count, total cost, avg duration."""
    grouped = defaultdict(lambda: {
        "total": 0, "success": 0, "failed": 0, "cost": 0.0, "duration": 0.0,
    })
    for e in entries:
        key = (e.get("provider", "?"), e.get("model", "?"), e.get("phase", "?"))
        g = grouped[key]
        g["total"] += 1
        if e.get("status") == "success":
            g["success"] += 1
        else:
            g["failed"] += 1
        g["cost"] += float(e.get("cost_usd", 0) or 0)
        g["duration"] += float(e.get("duration_seconds", 0) or 0)
    return grouped


def _routingSourceCounts(entries):
    """Count how many calls used each routing source (explore/history/default/etc)."""
    return Counter(e.get("routing_source", "?") for e in entries)


def _buildDigest(target_date, vault_root):
    """Produce the markdown digest for target_date."""
    log_file = vault_root / "logs" / "cost_log.jsonl"
    fm_path = vault_root / "ai_context" / "failure_modes.md"
    task_files_dir = vault_root / "task_files"
    archive_dir = vault_root / "logs" / "prompt_archive"

    entries = list(_parseLogEntries(log_file, target_date))
    statuses = _collectTaskStatuses(task_files_dir)
    failures = _collectFailures(fm_path, target_date)

    # Aggregate metrics
    total_cost = sum(float(e.get("cost_usd", 0) or 0) for e in entries)
    total_calls = len(entries)
    total_duration = sum(float(e.get("duration_seconds", 0) or 0) for e in entries)
    grouped = _summarizeRouting(entries)
    routing_sources = _routingSourceCounts(entries)
    phases = Counter(e.get("phase", "?") for e in entries)
    statuses_count = Counter(e.get("status", "?") for e in entries)

    # Tasks touched today (from log entries)
    tasks_touched = sorted({e.get("task", "?") for e in entries if e.get("task")})

    # Prompt archive counts
    archive_today_count = 0
    if archive_dir.exists():
        for task_dir in archive_dir.iterdir():
            if task_dir.is_dir():
                for f in task_dir.glob("*.md"):
                    try:
                        if datetime.fromtimestamp(f.stat().st_mtime).date().isoformat() == target_date:
                            archive_today_count += 1
                    except Exception:
                        pass

    # Build markdown
    lines = []
    lines.append(f"---")
    lines.append(f"date: {target_date}")
    lines.append(f"generated: {datetime.now().isoformat(timespec='seconds')}")
    lines.append(f"total_calls: {total_calls}")
    lines.append(f"total_cost_usd: {total_cost:.4f}")
    lines.append(f"total_duration_seconds: {total_duration:.1f}")
    lines.append(f"tasks_touched: {len(tasks_touched)}")
    lines.append(f"failures_logged: {len(failures)}")
    lines.append(f"---")
    lines.append("")
    lines.append(f"# Session Digest — {target_date}")
    lines.append("")
    lines.append("Auto-generated. Read this on the next session start to know what happened.")
    lines.append("")
    lines.append("## Overview")
    lines.append("")
    lines.append(f"- **AI calls**: {total_calls}")
    lines.append(f"- **Cost**: ${total_cost:.4f}")
    lines.append(f"- **Total LLM duration**: {total_duration:.1f}s")
    lines.append(f"- **Tasks touched**: {len(tasks_touched)}")
    lines.append(f"- **Prompts archived**: {archive_today_count}")
    lines.append(f"- **Failures logged**: {len(failures)}")
    lines.append("")

    if not entries:
        lines.append("_No agent activity recorded for this date._")
        return "\n".join(lines) + "\n"

    # Phase breakdown
    lines.append("## Calls by Phase")
    lines.append("")
    lines.append("| Phase | Calls |")
    lines.append("|-------|-------|")
    for phase, count in sorted(phases.items(), key=lambda x: -x[1]):
        lines.append(f"| {phase} | {count} |")
    lines.append("")

    # Status breakdown
    lines.append("## Outcomes")
    lines.append("")
    lines.append("| Status | Count |")
    lines.append("|--------|-------|")
    for status, count in sorted(statuses_count.items(), key=lambda x: -x[1]):
        lines.append(f"| {status} | {count} |")
    lines.append("")

    # Routing sources — how decisions were made
    lines.append("## Routing Decisions")
    lines.append("")
    lines.append("| Source | Count |")
    lines.append("|--------|-------|")
    for src, count in sorted(routing_sources.items(), key=lambda x: -x[1]):
        lines.append(f"| {src} | {count} |")
    lines.append("")

    # Per-model breakdown
    lines.append("## Per Model × Phase")
    lines.append("")
    lines.append("| Provider | Model | Phase | Calls | Success | Failed | Cost USD | Avg Duration s |")
    lines.append("|----------|-------|-------|-------|---------|--------|----------|----------------|")
    for (prov, model, phase), g in sorted(grouped.items(), key=lambda x: (-x[1]["total"], x[0])):
        avg_dur = g["duration"] / g["total"] if g["total"] else 0
        lines.append(f"| {prov} | {model} | {phase} | {g['total']} | {g['success']} | {g['failed']} | ${g['cost']:.4f} | {avg_dur:.1f} |")
    lines.append("")

    # Tasks touched
    if tasks_touched:
        lines.append("## Tasks Touched")
        lines.append("")
        for t in tasks_touched:
            lines.append(f"- `{t}`")
        lines.append("")

    # Current task statuses (across whole task_files folder, not just today)
    if statuses:
        lines.append("## Task Backlog Snapshot")
        lines.append("")
        lines.append(f"_(state of `task_files/` at digest time, not just today)_")
        lines.append("")
        for status, names in sorted(statuses.items()):
            lines.append(f"- **{status}** ({len(names)}): {', '.join(names)}")
        lines.append("")

    # Failures from today
    if failures:
        lines.append("## Failures Logged Today")
        lines.append("")
        for f in failures:
            lines.append(f)
            lines.append("")

    # Patterns + recommendations (heuristic — no LLM call)
    lines.append("## Patterns & Recommendations")
    lines.append("")
    recs = _generateRecommendations(grouped, routing_sources, statuses_count, failures)
    if recs:
        for r in recs:
            lines.append(f"- {r}")
    else:
        lines.append("_No notable patterns detected._")
    lines.append("")

    return "\n".join(lines) + "\n"


def _generateRecommendations(grouped, routing_sources, statuses_count, failures):
    """Heuristic pattern detection — no LLM call. Cheap, fast, deterministic."""
    recs = []

    # Exploration progress
    explore_calls = routing_sources.get("explore", 0)
    history_calls = routing_sources.get("history", 0)
    if explore_calls > 0 and history_calls == 0:
        recs.append(f"Routing is still in exploration phase ({explore_calls} calls). "
                    f"More tasks needed before evidence-driven routing kicks in.")
    elif history_calls > explore_calls * 3:
        recs.append(f"Routing has converged ({history_calls} history-driven vs {explore_calls} explore). "
                    f"System is now optimizing based on collected evidence.")

    # High failure rate
    total = sum(statuses_count.values())
    failed = total - statuses_count.get("success", 0)
    if total > 0 and failed / total > 0.3:
        recs.append(f"Failure rate is high ({failed}/{total} = {int(failed/total*100)}%). "
                    f"Check failure_modes.md for patterns.")

    # Underperforming models
    for (prov, model, phase), g in grouped.items():
        if g["total"] >= 3:
            rate = g["success"] / g["total"]
            if rate < 0.5:
                recs.append(f"`{prov}:{model}` has low success rate for {phase} "
                            f"({g['success']}/{g['total']}). Routing should escalate.")

    # Repeated failures of the same type
    if failures:
        types = Counter()
        for f in failures:
            for line in f.split("\n"):
                if "**Type**:" in line:
                    types[line.split(":", 1)[1].strip()] += 1
        for ftype, count in types.items():
            if count >= 3:
                recs.append(f"Failure type `{ftype}` occurred {count}x today. "
                            f"This pattern needs root-cause investigation.")

    return recs


def main(argv):
    vault_root = _vault_root()

    # Parse args
    target_date = None
    range_days = 1
    if argv:
        if argv[0] == "--range" and len(argv) > 1:
            try:
                range_days = int(argv[1])
            except ValueError:
                print(f"Invalid range: {argv[1]}")
                return 2
        else:
            target_date = argv[0]

    digest_dir = vault_root / "logs" / "session_logs"
    digest_dir.mkdir(parents=True, exist_ok=True)

    # Determine dates to process
    if range_days > 1:
        end = date.today()
        dates_to_process = [(end - timedelta(days=i)).isoformat() for i in range(range_days)]
    elif target_date:
        dates_to_process = [target_date]
    else:
        dates_to_process = [date.today().isoformat()]

    written = []
    for d in dates_to_process:
        digest = _buildDigest(d, vault_root)
        out_file = digest_dir / f"{d}.md"
        out_file.write_text(digest, encoding="utf-8")
        written.append(out_file)

    print(f"{BOLD}session_digest {SCRIPT_VERSION}{RESET}")
    for f in written:
        try:
            rel = f.relative_to(vault_root)
        except ValueError:
            rel = f
        print(f"  {GREEN}✓{RESET} {rel}  {DIM}({f.stat().st_size} bytes){RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
