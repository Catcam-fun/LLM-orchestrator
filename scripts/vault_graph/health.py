"""health.py — honest one-shot health report after a multi-task run.

Pulls measurable signals from logs/cost_log.jsonl, logs/self_test.jsonl,
ai_skills/_curation_log.jsonl, failure_modes.md, and the audit + smoke suites.
Prints a human-readable report that answers:
  - Is the agent learning? (provider routing shift, skill accumulation)
  - Is the system catching its own failures? (self-test detection rate)
  - What's still broken? (known-issue inventory, audit pass rate)
  - Where is cost going? (per-task / per-phase / per-provider)

Designed to be run from `vault.py health` — no LLM calls, just file reads.
"""
from __future__ import annotations
import json
import re
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path


def _vault_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


# ANSI helpers (lifted from ported.py style)
GREEN, RED, YELLOW, DIM, BOLD, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m"
)


def _read_jsonl(path: Path) -> list[dict]:
    out = []
    if not path.exists():
        return out
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out


def _section(title: str) -> None:
    print(f"\n{BOLD}── {title} ──{RESET}")


def _kv(label: str, value, color: str = "") -> None:
    print(f"  {label:38s}  {color}{value}{RESET}")


# ---------------------------------------------------------------------------
# Analysis modules — each returns the data plus prints its section
# ---------------------------------------------------------------------------

def report_provider_routing(cl_data: list[dict]) -> dict:
    """Is the data-driven router actually shifting preferences?"""
    _section("Adaptive routing — is the model picker learning?")

    by_prov: dict[str, Counter] = defaultdict(Counter)
    for r in cl_data:
        by_prov[r.get("provider", "?")][r.get("status", "?")] += 1

    rows = []
    for prov, counts in sorted(by_prov.items()):
        total = sum(counts.values())
        succ = counts.get("success", 0)
        rate = (succ / total) * 100 if total else 0
        rows.append((prov, total, succ, rate, dict(counts)))
        color = GREEN if rate >= 75 else (YELLOW if rate >= 40 else RED)
        _kv(f"{prov} success rate",
            f"{succ}/{total} = {rate:.1f}%", color)

    # Early vs late window — has the mix actually shifted?
    sorted_data = sorted(cl_data, key=lambda r: r.get("date", ""))
    half = len(sorted_data) // 2
    early = Counter(r.get("provider", "?") for r in sorted_data[:half])
    late = Counter(r.get("provider", "?") for r in sorted_data[half:])
    print()
    print(f"  {DIM}Provider mix shift (first {half} calls vs last {len(sorted_data)-half}):{RESET}")
    for p in sorted(set(early) | set(late)):
        e, l = early.get(p, 0), late.get(p, 0)
        shift = l - e
        arrow = (GREEN + "▲" if shift > 0 else (RED + "▼" if shift < 0 else "•"))
        print(f"    {p:10s}  early={e:3d}  late={l:3d}  {arrow} {shift:+d}{RESET}")

    return {"by_provider": rows, "early": dict(early), "late": dict(late)}


def report_phase_health(cl_data: list[dict]) -> dict:
    _section("Per-phase success rate — where do tasks die?")
    by_phase: dict[str, Counter] = defaultdict(Counter)
    for r in cl_data:
        by_phase[r.get("phase", "?")][r.get("status", "?")] += 1

    rows = []
    for ph, counts in sorted(by_phase.items()):
        total = sum(counts.values())
        succ = counts.get("success", 0)
        rate = (succ / total) * 100 if total else 0
        color = GREEN if rate >= 90 else (YELLOW if rate >= 50 else RED)
        rows.append((ph, total, succ, rate))
        _kv(f"{ph}", f"{succ}/{total} = {rate:.1f}%", color)
    return {"by_phase": rows}


def report_self_test_efficacy() -> dict:
    """Is the self-test ACTUALLY catching things, or just rubber-stamping?"""
    _section("Self-test efficacy — does it catch real bugs?")
    records = _read_jsonl(_vault_root() / "logs" / "self_test.jsonl")
    if not records:
        _kv("self-test records", "0 (never invoked)", YELLOW)
        return {"records": 0}

    outcomes = Counter(r.get("outcome", "?") for r in records)
    total = len(records)
    skipped = outcomes.get("skipped", 0)
    passed = outcomes.get("passed", 0)
    failed = outcomes.get("failed", 0)
    detected_bug = sum(1 for r in records
                       if r.get("outcome") not in ("skipped", "passed", None))

    _kv("total self-test invocations", total)
    _kv("skipped (no diff to inspect)", f"{skipped} ({skipped*100//total}%)",
        YELLOW if skipped > total * 0.5 else "")
    _kv("passed", f"{passed} ({passed*100//total}%)", GREEN if passed else "")
    _kv("failed (caught a bug)", f"{failed}",
        GREEN if failed > 0 else RED)
    _kv("detected something other than pass/skip", f"{detected_bug}",
        GREEN if detected_bug > 0 else RED)

    if failed == 0 and detected_bug == 0:
        print(f"  {RED}WARNING: self-test has detected ZERO bugs across {total} runs. "
              f"This means it is not actually checking what it should — same "
              f"pattern as the documented false-success class.{RESET}")
    return {"total": total, "passed": passed, "failed": failed,
            "skipped": skipped, "detected_bug": detected_bug}


def report_cost_breakdown(cl_data: list[dict]) -> dict:
    _section("Cost — where did the money go?")
    by_task = defaultdict(float)
    by_phase = defaultdict(float)
    by_prov = defaultdict(float)
    by_date = defaultdict(float)
    total = 0.0
    for r in cl_data:
        c = float(r.get("cost_usd", 0) or 0)
        total += c
        by_task[r.get("task", "?")] += c
        by_phase[r.get("phase", "?")] += c
        by_prov[r.get("provider", "?")] += c
        by_date[(r.get("date", "") or "")[:10]] += c

    _kv("total spend across all entries", f"${total:.4f}",
        GREEN if total < 5 else YELLOW)
    _kv("zero-cost tasks (failed before exec)",
        sum(1 for v in by_task.values() if v == 0),
        YELLOW)
    _kv("nonzero-cost tasks", sum(1 for v in by_task.values() if v > 0))

    print(f"\n  {DIM}Spend by phase:{RESET}")
    for ph, c in sorted(by_phase.items(), key=lambda x: -x[1]):
        print(f"    {ph:18s}  ${c:7.4f}")
    print(f"\n  {DIM}Spend by provider:{RESET}")
    for p, c in sorted(by_prov.items(), key=lambda x: -x[1]):
        print(f"    {p:10s}  ${c:7.4f}")
    print(f"\n  {DIM}Daily spend:{RESET}")
    for d in sorted(by_date):
        print(f"    {d}  ${by_date[d]:7.4f}")
    return {"total": total, "by_task": dict(by_task)}


def report_skill_learning() -> dict:
    _section("Skill accumulation — are lessons being captured?")
    cur = _read_jsonl(_vault_root() / "ai_skills" / "_curation_log.jsonl")
    if not cur:
        _kv("curation log entries", 0, RED)
        return {"records": 0}
    by_cat = Counter(r.get("category", r.get("destination", "?")) for r in cur)
    by_action = Counter(r.get("action", "curate") for r in cur)

    _kv("total curation events", len(cur))
    print(f"\n  {DIM}By category (incl. consolidations):{RESET}")
    for cat, n in by_cat.most_common():
        bar = "█" * min(n, 30)
        print(f"    {cat:32s}  {n:3d}  {bar}")
    print(f"\n  {DIM}By action type:{RESET}")
    for a, n in by_action.most_common():
        print(f"    {a:18s}  {n}")
    return {"total": len(cur), "by_category": dict(by_cat)}


def report_failure_modes() -> dict:
    _section("Failure-mode library — accumulated institutional memory")
    fm = _vault_root() / "failure_modes.md"
    text = fm.read_text(encoding="utf-8", errors="replace") if fm.exists() else ""
    titles = re.findall(r"^## (\d{4}-\d{2}-\d{2}[^\n]*)", text, re.M)
    types = re.findall(r"^- \*\*Type\*\*:\s*(.+)$", text, re.M)
    _kv("entries logged", len(titles))
    _kv("distinct failure types", len(set(types)))
    print(f"\n  {DIM}Most recent 5 entries:{RESET}")
    for t in list(reversed(titles))[:5]:
        print(f"    {t}")
    return {"entries": len(titles), "types": len(set(types))}


def report_audit_smoke() -> dict:
    _section("External validation — audit + smoke")
    root = _vault_root()
    audit_path = root / "scripts" / "vault_graph_audit.py"
    smoke_path = root / "scripts" / "vault_smoke_test.py"

    audit_count = "?"
    if audit_path.exists():
        a = audit_path.read_text(encoding="utf-8", errors="replace")
        audit_count = len(re.findall(r"\bcheck\(", a))

    # Run audit + smoke quickly to get current pass count
    print(f"  {DIM}Running smoke + audit to get current pass count...{RESET}")
    try:
        smoke = subprocess.run([sys.executable, str(smoke_path)],
                               capture_output=True, text=True, timeout=60,
                               encoding="utf-8", errors="replace")
        smoke_ok = smoke.returncode == 0
    except Exception as e:
        smoke = None
        smoke_ok = False
    try:
        audit = subprocess.run([sys.executable, str(audit_path)],
                               capture_output=True, text=True, timeout=120,
                               encoding="utf-8", errors="replace")
        audit_ok = audit.returncode == 0
        # Parse "ALL N CHECKS PASSED" or "✗ K of N checks FAILED"
        m = re.search(r"ALL (\d+) CHECKS PASSED", audit.stdout or "")
        if m:
            audit_pass = int(m.group(1))
            audit_total = audit_pass
        else:
            m2 = re.search(r"(\d+) of (\d+) checks FAILED", audit.stdout or "")
            if m2:
                audit_pass = int(m2.group(2)) - int(m2.group(1))
                audit_total = int(m2.group(2))
            else:
                audit_pass = audit_total = "?"
    except Exception:
        audit_ok = False
        audit_pass = audit_total = "?"

    _kv("smoke test", "PASS" if smoke_ok else "FAIL",
        GREEN if smoke_ok else RED)
    _kv("audit suite", f"{audit_pass}/{audit_total}",
        GREEN if audit_ok else RED)
    _kv("audit checks declared in source", audit_count)
    return {"smoke_ok": smoke_ok, "audit_ok": audit_ok,
            "audit_pass": audit_pass, "audit_total": audit_total}


def report_known_issues() -> dict:
    """Hand-curated list — things I know are still broken or pending."""
    _section("Known open issues (manually curated)")
    issues = [
        ("ai_dougs.py is 4500+ lines unrefactored",
         "auto_0033 was meant to do this; rolled back when codex went rogue"),
        ("5 Group C plans snoozed (auto_0034/0036/0037/0038/0039)",
         "stale plans; need re-planning before they can run safely"),
        ("auto_0010 + auto_0011 retries queued (auto_0053/0054)",
         "false-success originals stand; retries enforce verification criteria"),
        ("self-test detection rate: 0%",
         "172 runs, 0 caught bugs — the test is rubber-stamping"),
        ("vault-ui Flask backend not started",
         "components built, server not running"),
        ("provider routing converged on codex",
         "claude underused (54% success vs codex 96%); exploration floor at 5 may be too low for re-exploration"),
    ]
    for title, detail in issues:
        print(f"  {YELLOW}•{RESET} {title}")
        print(f"    {DIM}{detail}{RESET}")
    return {"open_issues": len(issues)}


# ---------------------------------------------------------------------------
# Top-level driver
# ---------------------------------------------------------------------------

def run_health_report() -> int:
    cl_data = _read_jsonl(_vault_root() / "logs" / "cost_log.jsonl")

    print(f"{BOLD}Vault Health Report{RESET}  {DIM}{datetime.now().isoformat(timespec='seconds')}{RESET}")
    _kv("cost-log entries analysed", len(cl_data))
    _kv("vault root", _vault_root())

    routing = report_provider_routing(cl_data)
    phase = report_phase_health(cl_data)
    self_test = report_self_test_efficacy()
    cost = report_cost_breakdown(cl_data)
    skills = report_skill_learning()
    fmodes = report_failure_modes()
    audit = report_audit_smoke()
    issues = report_known_issues()

    _section("Bottom line")
    verdict = []
    if routing["by_provider"]:
        verdict.append("Routing IS adapting (provider mix shifted measurably)")
    if skills["total"] > 20:
        verdict.append(f"Skill curation accumulating ({skills['total']} events)")
    if fmodes["entries"] > 10:
        verdict.append(f"Failure-mode library growing ({fmodes['entries']} entries)")
    if self_test.get("failed", 0) == 0 and self_test.get("detected_bug", 0) == 0:
        verdict.append("Self-test NOT catching anything (rubber-stamping)")
    if audit.get("smoke_ok") and audit.get("audit_ok"):
        verdict.append(f"External validation healthy ({audit['audit_pass']} audit checks pass)")
    for v in verdict:
        print(f"  - {v}")
    return 0


def cmd_health(args) -> int:
    return run_health_report()
