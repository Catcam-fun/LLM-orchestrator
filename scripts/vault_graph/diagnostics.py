"""diagnostics.py — vault.py diagnostics sub-command.

A read-only inspector for vault state. Built so user + orchestrator can
answer questions like:
  - Which models are routing well lately? Which are spending without working?
  - What happened during task X — show me the whole timeline.
  - Find tasks that hit a particular failure mode or model.
  - What's running right now?

Sources it reads (no writes):
  - logs/cost_log.jsonl              per-LLM-call cost/status
  - logs/skill_usage.jsonl           skill load + helpful signals
  - logs/events.jsonl                watchdog kills, cost-ceiling crossings,
                                     truncation events, structural-only warnings
  - ai_context/failure_modes.md      failure log (append-only markdown)
  - LangGraph SQLite checkpoint DB   every state snapshot per task
  - .agent_memory/<slug>/task_log.jsonl per-project task history

Sub-commands (vault.py diagnostics <cmd>):
  summary (default)  one-screen overview
  task <name>        deep dive on one task
  search <query>     pattern search across sources
  live               snapshot of in-flight tasks
"""
from __future__ import annotations
import json
import re
import sqlite3
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable


# ─── ANSI helpers (no dependency on ported.py to keep this read-only-isolated) ─
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
BLUE = "\033[34m"
MAGENTA = "\033[35m"
CYAN = "\033[36m"


def _vault_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


# ─── Loaders ──────────────────────────────────────────────────────────────────

def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def load_cost_log(since_days: int | None = None) -> list[dict]:
    entries = _load_jsonl(_vault_root() / "logs" / "cost_log.jsonl")
    if since_days is None:
        return entries
    cutoff = datetime.now() - timedelta(days=since_days)
    out = []
    for e in entries:
        ts_str = e.get("timestamp") or e.get("date") or ""
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "").split("+")[0])
        except (ValueError, AttributeError):
            continue
        if ts >= cutoff:
            out.append(e)
    return out


def load_events(since_days: int | None = None) -> list[dict]:
    entries = _load_jsonl(_vault_root() / "logs" / "events.jsonl")
    if since_days is None:
        return entries
    cutoff = datetime.now() - timedelta(days=since_days)
    out = []
    for e in entries:
        ts_str = e.get("timestamp", "")
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", ""))
        except ValueError:
            continue
        if ts >= cutoff:
            out.append(e)
    return out


def load_skill_usage() -> list[dict]:
    return _load_jsonl(_vault_root() / "logs" / "skill_usage.jsonl")


def load_failure_modes() -> list[dict]:
    """Parse ai_context/failure_modes.md into structured entries."""
    path = _vault_root() / "ai_context" / "failure_modes.md"
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8", errors="replace")
    entries = []
    # Entries are separated by "---" and have a `## YYYY-MM-DD HH:MM — task / phase` heading.
    for block in text.split("\n---\n"):
        block = block.strip()
        m = re.search(r"^##\s+(\S+ \S+)\s+\S+\s+(\S+)\s*/\s*(\S+)", block, re.MULTILINE)
        if not m:
            continue
        ts, task, phase = m.group(1), m.group(2), m.group(3)
        type_m = re.search(r"^\s*-\s*\*\*Type\*\*:\s*(.+)$", block, re.MULTILINE)
        app_m = re.search(r"^\s*-\s*\*\*App\*\*:\s*(.+)$", block, re.MULTILINE)
        details_m = re.search(r"^\s*-\s*\*\*Details\*\*:\s*(.+)$", block, re.MULTILINE | re.DOTALL)
        entries.append({
            "timestamp": ts,
            "task": task,
            "phase": phase,
            "type": (type_m.group(1).strip() if type_m else ""),
            "app": (app_m.group(1).strip() if app_m else ""),
            "details": (details_m.group(1).strip() if details_m else ""),
        })
    return entries


def _checkpoint_db_path() -> Path:
    # Matches scripts/vault_graph/checkpointer.py
    return _vault_root() / ".state" / "checkpoints.sqlite"


def list_active_threads() -> list[str]:
    """Return all task names that have any checkpoint in the DB."""
    db = _checkpoint_db_path()
    if not db.exists():
        return []
    try:
        with sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5.0) as conn:
            rows = conn.execute("SELECT DISTINCT thread_id FROM checkpoints").fetchall()
        return sorted(r[0] for r in rows if r and r[0])
    except Exception:
        return []


def latest_state(thread_id: str) -> dict:
    """Decode the latest checkpoint for a task via LangGraph's serializer."""
    db = _checkpoint_db_path()
    if not db.exists():
        return {}
    try:
        from langgraph.checkpoint.sqlite import SqliteSaver
        with SqliteSaver.from_conn_string(str(db)) as saver:
            cp = saver.get({"configurable": {"thread_id": thread_id}})
        if not cp:
            return {}
        cv = cp.get("channel_values") if isinstance(cp, dict) else None
        return cv if isinstance(cv, dict) else {}
    except Exception:
        return {}


def load_active_subprocs() -> dict:
    p = _vault_root() / "logs" / "active_subprocesses.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _fmt_money(usd: float) -> str:
    if usd < 0.01:
        return f"${usd:.4f}"
    if usd < 1:
        return f"${usd:.3f}"
    return f"${usd:.2f}"


def _fmt_short(text: str, n: int = 80) -> str:
    text = (text or "").replace("\n", " ").strip()
    return text if len(text) <= n else text[: n - 1] + "…"


def _section(title: str) -> None:
    print(f"\n{BOLD}── {title} ──{RESET}")


def _terminal_states() -> set[str]:
    return {"completed", "failed", "budget_exceeded"}


def _is_active(state: dict) -> bool:
    return state.get("status", "") not in _terminal_states()


# ─── summary ──────────────────────────────────────────────────────────────────

def cmd_summary(since_days: int = 7) -> int:
    print(f"\n{BOLD}vault diagnostics — last {since_days} day(s){RESET}")

    cost = load_cost_log(since_days)
    events = load_events(since_days)
    failures = load_failure_modes()
    threads = list_active_threads()

    # Recent activity
    _section("Activity")
    task_set = {e.get("task", "") for e in cost if e.get("task")}
    total_cost = sum(float(e.get("spend", e.get("cost_usd", 0)) or 0) for e in cost)
    print(f"  Tasks touched   : {len(task_set)}")
    print(f"  LLM calls       : {len(cost)}")
    print(f"  Total spend     : {_fmt_money(total_cost)}")

    # Routing health: top (provider, model, phase) tuples
    _section("Top routings (provider · model · phase)")
    bucket: dict[tuple, list[dict]] = defaultdict(list)
    for e in cost:
        p, m, ph = e.get("provider", "?"), e.get("model", "?"), e.get("phase", "?")
        bucket[(p, m, ph)].append(e)
    rows = []
    for (p, m, ph), es in bucket.items():
        n = len(es)
        spend = sum(float(x.get("spend", x.get("cost_usd", 0)) or 0) for x in es)
        ok = sum(1 for x in es if x.get("status") == "success")
        rows.append((n, spend, ok, p, m, ph))
    for n, spend, ok, p, m, ph in sorted(rows, reverse=True)[:5]:
        rate = (ok / n * 100) if n else 0
        print(f"  {n:>3}x  {_fmt_money(spend):>9}  {rate:>5.1f}% ok   {p}:{m}  {DIM}({ph}){RESET}")

    # Top expensive tasks
    _section("Most expensive tasks")
    task_spend: dict[str, float] = defaultdict(float)
    for e in cost:
        if t := e.get("task"):
            task_spend[t] += float(e.get("spend", e.get("cost_usd", 0)) or 0)
    for task, spend in sorted(task_spend.items(), key=lambda kv: -kv[1])[:5]:
        print(f"  {_fmt_money(spend):>9}  {task}")

    # Recent failures grouped by type
    _section("Recent failures (by type)")
    recent_failures = [f for f in failures
                       if f["timestamp"] >= (datetime.now() - timedelta(days=since_days)).strftime("%Y-%m-%d %H:%M")]
    type_counts = Counter(f["type"] for f in recent_failures if f["type"])
    if not type_counts:
        print(f"  {DIM}(none in window){RESET}")
    for ftype, count in type_counts.most_common(8):
        print(f"  {count:>3}x  {ftype}")

    # Skill usage
    _section("Skill usage")
    skill_events = load_skill_usage()
    skill_stats: dict[str, dict] = defaultdict(lambda: {"loaded": 0, "used": 0, "helpful": 0})
    for ev in skill_events:
        name = ev.get("skill", "")
        if not name:
            continue
        if ev.get("loaded"):
            skill_stats[name]["loaded"] += 1
        if ev.get("used"):
            skill_stats[name]["used"] += 1
        if ev.get("helpful"):
            skill_stats[name]["helpful"] += 1
    skills_dir = _vault_root() / "ai_skills"
    all_skills = [d.name for d in skills_dir.iterdir()
                  if skills_dir.exists() and d.is_dir() and not d.name.startswith("_")]
    print(f"  {BOLD}most loaded:{RESET}")
    for name, s in sorted(skill_stats.items(), key=lambda kv: -kv[1]["loaded"])[:5]:
        helpful_rate = (s["helpful"] / s["used"] * 100) if s["used"] else 0
        print(f"    {s['loaded']:>3}x loaded   {s['used']:>3}x used   "
              f"{helpful_rate:>5.1f}% helpful   {name}")
    never_loaded = sorted(set(all_skills) - set(skill_stats.keys()))
    if never_loaded:
        print(f"  {BOLD}never loaded:{RESET} {DIM}{', '.join(never_loaded)}{RESET}")

    # Events (watchdog, cost-ceiling, structural, truncation)
    _section("Events")
    event_counts = Counter(e.get("event", e.get("rule", "?")) for e in events)
    if not event_counts:
        print(f"  {DIM}(no events recorded in window){RESET}")
    for ev_type, n in event_counts.most_common(10):
        print(f"  {n:>3}x  {ev_type}")

    # Active tasks summary
    _section("In-flight tasks")
    active = []
    for tid in threads:
        st = latest_state(tid)
        if _is_active(st):
            active.append((tid, st))
    if not active:
        print(f"  {DIM}(none active){RESET}")
    for tid, st in active:
        print(f"  {tid}  {DIM}phase={st.get('current_phase', '?')}  "
              f"status={st.get('status', '?')}  next={st.get('next_action', '?')}{RESET}")

    print()
    return 0


# ─── task drill-down ──────────────────────────────────────────────────────────

def cmd_task(task_name: str) -> int:
    print(f"\n{BOLD}vault diagnostics — task: {task_name}{RESET}")
    st = latest_state(task_name)
    if not st:
        print(f"  {YELLOW}no checkpoints found for task {task_name!r}{RESET}\n")
        return 1

    _section("Summary")
    print(f"  Status      : {st.get('status', '?')}")
    print(f"  Phase       : {st.get('current_phase', '?')}")
    print(f"  Next action : {st.get('next_action', '?')}")
    print(f"  App         : {st.get('app_location', '(vault)') or '(vault)'}")
    print(f"  Refinements : {st.get('refinement_n', 0)}")
    print(f"  Exec attempts: {st.get('execution_attempts', 0)}")
    pd = st.get("phase_durations", {}) or {}
    if pd:
        print(f"  Phase durations (s):")
        for phase, dur in pd.items():
            print(f"    {phase:<22} {dur:.1f}s")

    # Per-phase cost summary from cost_log
    _section("Cost by phase")
    cost = load_cost_log()
    rows: dict[str, list[dict]] = defaultdict(list)
    for e in cost:
        if e.get("task") == task_name:
            rows[e.get("phase", "?")].append(e)
    if not rows:
        print(f"  {DIM}(no cost_log entries for this task){RESET}")
    total = 0.0
    for phase in sorted(rows):
        es = rows[phase]
        spend = sum(float(x.get("spend", x.get("cost_usd", 0)) or 0) for x in es)
        total += spend
        providers = sorted({f"{x.get('provider','?')}:{x.get('model','?')}" for x in es})
        print(f"  {phase:<22} {_fmt_money(spend):>9}  {len(es)} call(s)  "
              f"{DIM}{', '.join(providers)}{RESET}")
    print(f"  {BOLD}{'TOTAL':<22} {_fmt_money(total):>9}{RESET}")

    # Plan text excerpt
    plan = st.get("plan_text", "") or ""
    if plan:
        _section("Plan (excerpt)")
        for line in plan.splitlines()[:30]:
            print(f"  {line}")
        if len(plan.splitlines()) > 30:
            print(f"  {DIM}... ({len(plan.splitlines())} lines total){RESET}")

    # Refinements
    refinements = st.get("refinements") or []
    if refinements:
        _section(f"Refinements ({len(refinements)})")
        for i, r in enumerate(refinements):
            print(f"  {BOLD}#{i+1}:{RESET}")
            for line in (r or "").splitlines()[:6]:
                print(f"    {line}")

    # Execution log
    exec_log = st.get("execution_log", "") or ""
    if exec_log:
        _section(f"Execution log (last 1500 chars of {len(exec_log)})")
        tail = exec_log[-1500:]
        for line in tail.splitlines():
            print(f"  {line}")

    # Verification outcome
    vo = st.get("verification_outcome") or {}
    if vo:
        _section("Verification")
        ap = vo.get("all_passed")
        print(f"  all_passed: {ap}  iterations_used: {vo.get('iterations_used', '?')}")
        for r in vo.get("results", []) or []:
            ok = "✓" if r.get("passed") else "✗"
            color = GREEN if r.get("passed") else RED
            print(f"  {color}{ok}{RESET} {r.get('id', '?'):<24} "
                  f"{DIM}{_fmt_short(r.get('detail', ''), 90)}{RESET}")

    # Judge verdict + concerns
    jv = st.get("judge_verdict") or {}
    if jv:
        _section("Judge verdict (advisory)")
        v = jv.get("verdict", "?")
        avg = jv.get("score_avg", 0)
        print(f"  Verdict: {v}   avg score: {avg}/5")
        for axis, sc in (jv.get("scores") or {}).items():
            print(f"    {axis:<22} {sc}/5")
        for c in (jv.get("concerns") or [])[:15]:
            print(f"  • {_fmt_short(c, 110)}")

    # Plan review concerns
    prv = st.get("plan_review_verdict") or {}
    if prv:
        _section("Plan review (advisory)")
        print(f"  Verdict: {prv.get('verdict', '?')}   avg score: {prv.get('score_avg', 0)}/5")
        for c in (prv.get("concerns") or [])[:10]:
            print(f"  • {_fmt_short(c, 110)}")

    # Skills loaded
    skills = st.get("loaded_skills") or []
    if skills:
        _section("Skills loaded")
        print(f"  {', '.join(skills)}")

    # Failures recorded for this task
    failures = [f for f in load_failure_modes() if f["task"] == task_name]
    if failures:
        _section(f"Failures recorded ({len(failures)})")
        for f in failures:
            print(f"  [{f['timestamp']}] {f['phase']} · {f['type']}")
            print(f"    {DIM}{_fmt_short(f['details'], 150)}{RESET}")

    # Events for this task
    events = [e for e in load_events() if e.get("task") == task_name
              or task_name in (e.get("task") or "")]
    if events:
        _section(f"Events ({len(events)})")
        for e in events:
            evt = e.get("event", e.get("rule", "?"))
            print(f"  [{e.get('timestamp', '?')}] {evt}  "
                  f"{DIM}{_fmt_short(e.get('details', ''), 120)}{RESET}")

    print()
    return 0


# ─── search ───────────────────────────────────────────────────────────────────

def cmd_search(query: str) -> int:
    q = query.lower()
    print(f"\n{BOLD}vault diagnostics — search: {query!r}{RESET}")
    hits = []

    # cost_log
    for e in load_cost_log():
        blob = json.dumps(e, default=str).lower()
        if q in blob:
            hits.append(("cost_log", e.get("task", "?"),
                         f"{e.get('phase', '?')} · {e.get('provider', '?')}:{e.get('model', '?')} "
                         f"· {e.get('status', '?')} · {_fmt_money(float(e.get('spend', 0) or 0))}"))

    # events
    for e in load_events():
        blob = json.dumps(e, default=str).lower()
        if q in blob:
            hits.append(("event", e.get("task", "?"),
                         f"{e.get('event', e.get('rule', '?'))} · "
                         f"{_fmt_short(e.get('details', ''), 100)}"))

    # failure_modes
    for f in load_failure_modes():
        blob = json.dumps(f, default=str).lower()
        if q in blob:
            hits.append(("failure", f["task"],
                         f"{f['phase']} · {f['type']} · {_fmt_short(f['details'], 100)}"))

    # task states (search plan_text / execution_log / judge concerns)
    for tid in list_active_threads():
        st = latest_state(tid)
        haystack = " ".join(str(v) for v in [
            st.get("plan_text", ""),
            st.get("execution_log", ""),
            st.get("execution_judge_text", ""),
            st.get("plan_review_text", ""),
            json.dumps(st.get("judge_verdict", {}), default=str),
        ]).lower()
        if q in haystack:
            hits.append(("task_state", tid,
                         f"{st.get('current_phase', '?')} · {st.get('status', '?')}"))

    if not hits:
        print(f"  {DIM}no matches{RESET}\n")
        return 0

    # Group by source
    by_src: dict[str, list[tuple]] = defaultdict(list)
    for src, task, detail in hits:
        by_src[src].append((task, detail))
    for src, rows in by_src.items():
        _section(f"{src}  ({len(rows)} match{'es' if len(rows) != 1 else ''})")
        # dedupe (task, detail) tuples but cap output
        seen = set()
        for task, detail in rows:
            key = (task, detail)
            if key in seen:
                continue
            seen.add(key)
            print(f"  {task:<32}  {DIM}{detail}{RESET}")
            if len(seen) >= 15:
                print(f"  {DIM}... ({len(rows) - 15} more){RESET}")
                break
    print()
    return 0


# ─── live snapshot ────────────────────────────────────────────────────────────

def cmd_live() -> int:
    print(f"\n{BOLD}vault diagnostics — live snapshot{RESET}")
    threads = list_active_threads()
    active = [(tid, latest_state(tid)) for tid in threads]
    active = [(tid, st) for tid, st in active if _is_active(st)]
    if not active:
        print(f"  {DIM}no in-flight tasks{RESET}\n")
    else:
        _section(f"In-flight ({len(active)})")
        for tid, st in active:
            print(f"  {BOLD}{tid}{RESET}")
            print(f"    phase       : {st.get('current_phase', '?')}")
            print(f"    status      : {st.get('status', '?')}")
            print(f"    next_action : {st.get('next_action', '?')}")
            print(f"    app         : {st.get('app_location', '(vault)') or '(vault)'}")
            ps = st.get("phase_start_time")
            if isinstance(ps, (int, float)):
                elapsed = (time.monotonic() - ps) / 60
                print(f"    in phase    : {elapsed:.1f} min")

    subs = load_active_subprocs()
    _section(f"Active subprocesses ({len(subs)})")
    if not subs:
        print(f"  {DIM}(none registered){RESET}")
    now = time.time()
    for pid, info in subs.items():
        started = info.get("started_at", 0)
        alive_min = (now - started) / 60 if started else 0
        print(f"  PID {pid}  {info.get('label', '?'):<28}  alive {alive_min:.1f} min")

    print()
    return 0


# ─── CLI entrypoint ───────────────────────────────────────────────────────────

def cmd_diagnostics(args) -> int:
    """Top-level dispatcher for `vault.py diagnostics <sub>`."""
    sub = getattr(args, "sub", None) or "summary"
    if sub == "summary":
        return cmd_summary(since_days=getattr(args, "days", 7))
    if sub == "task":
        name = getattr(args, "task_name", "")
        if not name:
            print(f"{RED}usage: vault.py diagnostics task <task_name>{RESET}")
            return 2
        return cmd_task(name)
    if sub == "search":
        query = getattr(args, "query", "")
        if not query:
            print(f"{RED}usage: vault.py diagnostics search <query>{RESET}")
            return 2
        return cmd_search(query)
    if sub == "live":
        return cmd_live()
    print(f"{RED}unknown subcommand: {sub!r}{RESET}")
    print(f"  Available: summary, task, search, live")
    return 2
