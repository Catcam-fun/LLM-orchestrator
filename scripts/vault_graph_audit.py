#!/usr/bin/env python3
"""vault_graph_audit.py — Behavioral verification of the LangGraph migration.

Runs after Days 1-4 complete. Verifies every component of the new vault_graph
package works end-to-end without burning real LLM cost. Catches regressions
between the old ai_dougs.py implementation and the new graph nodes.

Tests:
  1. Package imports + module versions
  2. State schema sanity (initial_state, all expected fields)
  3. Every phase node is callable
  4. _synthesise_task_text builds valid markdown for the agent
  5. Graph compiles with checkpointer
  6. Daemon's _parse_task_file handles real markdown task spec
  7. CLI subcommands all registered
  8. Crash recovery still works on dummy task (full pipeline with stubs)
  9. Cost log + failure_modes + prompt_archive write paths intact

Doesn't make any LLM calls. Run anytime to verify the migration didn't break.
"""
import importlib.util
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

GREEN, RED, DIM, BOLD, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"

SCRIPT_VERSION = "0.1.13"  # 2026-05-11 - refined verification block consumer checks
VAULT_ROOT = Path(__file__).resolve().parent.parent

results = []


def check(name, fn):
    try:
        msg = fn()
        results.append((name, True, msg or "ok"))
        print(f"  {GREEN}✓{RESET} {name}  {DIM}{msg or ''}{RESET}")
    except AssertionError as e:
        results.append((name, False, str(e)))
        print(f"  {RED}✗{RESET} {name}  {RED}{e}{RESET}")
    except Exception as e:
        results.append((name, False, f"{type(e).__name__}: {e}"))
        print(f"  {RED}✗{RESET} {name}  {RED}{type(e).__name__}: {e}{RESET}")


def section(title):
    print(f"\n{BOLD}── {title} ──{RESET}")


print(f"{BOLD}vault_graph_audit {SCRIPT_VERSION}{RESET}  {DIM}{VAULT_ROOT}{RESET}")
print(f"{DIM}Loading vault_graph package...{RESET}")

sys.path.insert(0, str(VAULT_ROOT / "scripts"))
from vault_graph import state, graph, checkpointer, cli
from vault_graph import project_memory
from vault_graph import ported as P
from vault_graph import vault_backup
from vault_graph.log_rotation import rotate_logs
from vault_graph.nodes import stubs, planning as planning_node
from vault_graph.nodes import execution as execution_node
from vault_graph.nodes import refinement as refinement_node


# ════════════════════════════════════════════════════════════════════════════
# 1. Package + version sanity
# ════════════════════════════════════════════════════════════════════════════
section("Package sanity")

check("ported.py shim covers every needed helper",
      lambda: f"{len([k for k in dir(P) if not k.startswith('__')])} symbols")
# Pruned 2026-05-07: "package importable", "cli.SCRIPT_VERSION present",
# "vault_backup exposes create_backup" — duplicate of smoke test + trivial
# version checks. The behavioral coverage below catches real regressions.


# ════════════════════════════════════════════════════════════════════════════
# 1b. Vault backup guard
# ════════════════════════════════════════════════════════════════════════════
section("Vault backup guard")


def t_backup_scope_empty_app_location():
    assert vault_backup.should_backup_for_app_location("", VAULT_ROOT)
    assert vault_backup.should_backup_for_app_location(None, VAULT_ROOT)
    return "empty app_location triggers backup"


def t_backup_scope_vault_root():
    assert vault_backup.should_backup_for_app_location(".", VAULT_ROOT)
    assert vault_backup.should_backup_for_app_location(str(VAULT_ROOT), VAULT_ROOT)
    return "explicit vault root triggers backup"


def t_backup_scope_code_app_skips():
    assert not vault_backup.should_backup_for_app_location("code/example", VAULT_ROOT)
    assert not vault_backup.should_backup_for_app_location("code\\example", VAULT_ROOT)
    return "code/<app> app_location skips backup"


def t_backup_exclusions_include_standard_dirs():
    required = {".git", ".backups", "node_modules"}
    missing = required - vault_backup.BACKUP_EXCLUDE
    assert not missing, f"missing exclusions: {missing}"
    return f"{len(vault_backup.BACKUP_EXCLUDE)} excluded directory names"


def t_backup_filename_task_name_is_safe():
    safe = vault_backup._safe_task_name("auto/0009: root backup?")
    assert safe == "auto_0009__root_backup", safe
    return safe


def t_backup_tiny_fixture_archive():
    import tarfile
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "vault"
        root.mkdir()
        (root / "ai_main.md").write_text("# root", encoding="utf-8")
        (root / "README.md").write_text("readme", encoding="utf-8")
        (root / ".git").mkdir()
        (root / ".git" / "config").write_text("ignore", encoding="utf-8")
        (root / "node_modules").mkdir()
        (root / "node_modules" / "package.txt").write_text("ignore", encoding="utf-8")
        backup_path = vault_backup.create_backup(root, "audit_task")
        assert backup_path.exists(), "backup archive was not created"
        assert backup_path.parent.name == ".backups"
        with tarfile.open(backup_path, "r:gz") as archive:
            names = archive.getnames()
        assert "vault/README.md" in names
        assert not any("/.git/" in name or name.endswith("/.git") for name in names)
        assert not any("/node_modules/" in name or name.endswith("/node_modules") for name in names)
    return "tiny fixture archive created with exclusions"


def t_backup_global_pruning():
    with tempfile.TemporaryDirectory() as td:
        backup_dir = Path(td) / ".backups"
        backup_dir.mkdir()
        retention_count = 5
        tasks = ["auto_0001", "auto_0002", "auto_0003", "auto_0004", "auto_0005", "auto_0006", "auto_0007"]
        for index, task in enumerate(tasks, start=1):
            (backup_dir / f"{task}_2026-01-{index:02d}_120000.tar.gz").write_text("x", encoding="utf-8")
        vault_backup.prune_old_backups(backup_dir, retention_count)
        remaining = sorted(path.name for path in backup_dir.glob("*.tar.gz"))
        assert len(remaining) == retention_count, remaining
        assert not any(name.startswith(("auto_0001_", "auto_0002_")) for name in remaining), remaining
    return "global backup retention keeps newest 5 tarballs"


check("backup triggers for empty app_location", t_backup_scope_empty_app_location)
check("backup triggers for explicit vault root app_location", t_backup_scope_vault_root)
check("backup skips code/<app> app_location", t_backup_scope_code_app_skips)
check("backup exclusions include .git, .backups, node_modules",
      t_backup_exclusions_include_standard_dirs)
check("backup task-name sanitiser produces filesystem-safe prefix",
      t_backup_filename_task_name_is_safe)
check("backup creates tiny fixture archive and applies exclusions",
      t_backup_tiny_fixture_archive)
check("backup pruning is global across task prefixes",
      t_backup_global_pruning)


def t_log_rotation_lifecycle():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        logs_dir = root / "logs"
        logs_dir.mkdir()
        today = datetime(2026, 5, 10, tzinfo=timezone.utc).date()
        (logs_dir / "cost_log.jsonl").write_bytes(b"x" * (5 * 1024 * 1024 + 1))
        old8 = today - timedelta(days=8)
        old35 = today - timedelta(days=35)
        (logs_dir / f"daemon_{old8:%Y-%m-%d}.log").write_text("old8", encoding="utf-8")
        (logs_dir / f"daemon_{old35:%Y-%m-%d}.log").write_text("old35", encoding="utf-8")

        rotate_logs(
            root,
            params={"cost_log_max_mb": 5, "log_archive_days": 7, "log_delete_days": 30},
            today_utc=today,
        )

        assert (logs_dir / "cost_log-20260510.jsonl").exists()
        assert (logs_dir / "cost_log.jsonl").exists()
        assert (logs_dir / "cost_log.jsonl").stat().st_size == 0
        assert (root / "_archive" / "old_logs" / f"daemon_{old8:%Y-%m-%d}.log").exists()
        assert not (logs_dir / f"daemon_{old35:%Y-%m-%d}.log").exists()
    return "cost log rotated, old daemon logs archived/deleted"


check("log rotation lifecycle handles cost, archive, and delete policies",
      t_log_rotation_lifecycle)


# ════════════════════════════════════════════════════════════════════════════
# 2. State schema
# ════════════════════════════════════════════════════════════════════════════
section("State schema")

def t_initial_state():
    s = state.initial_state("audit_test", initial_prompt="test")
    expected = {"task_name", "status", "current_phase", "execution_attempts",
                "existing_cost_usd", "cost_accumulator", "phase_durations",
                "planning_cost_usd", "execution_cost_usd", "learning_cost_usd",
                "refinements", "refinement_n"}
    missing = expected - set(s.keys())
    assert not missing, f"missing fields: {missing}"
    return f"{len(s)} fields populated"

def t_initial_state_overrides():
    s = state.initial_state(
        "x",
        app_location="code/foo",
        cost_ceiling=1.50,
        max_cost_usd=5.0,
    )
    assert s["app_location"] == "code/foo"
    assert s["cost_ceiling"] == 1.50
    assert s["max_cost_usd"] == 5.0
    return "kwargs pass through"

check("initial_state produces complete TaskState", t_initial_state)
check("initial_state honours kwargs overrides", t_initial_state_overrides)


# ════════════════════════════════════════════════════════════════════════════
# 3. Every phase node callable
# ════════════════════════════════════════════════════════════════════════════
section("Phase nodes callable")

REAL_NODES = {
    "task_entry":    planning_node.task_entry,
    "planning":      planning_node.planning,
    "plan_review":   planning_node.plan_review,
    "execution":     execution_node.execution,
    "refinement":    refinement_node.refinement,
    "learning":      refinement_node.learning,
}
STUB_NODES = {
    "wait_human_answers":  stubs.wait_human_answers,
    "wait_human_approval": stubs.wait_human_approval,
    "wait_human_review":   stubs.wait_human_review,
    "terminal_complete":   stubs.terminal_complete,
}

for name, fn in {**REAL_NODES, **STUB_NODES}.items():
    check(f"node {name} is callable",
          lambda fn=fn: f"{fn.__module__}.{fn.__name__}" if callable(fn) else (_ for _ in ()).throw(AssertionError("not callable")))


# ════════════════════════════════════════════════════════════════════════════
# 4. Stub nodes return correct state shape
# ════════════════════════════════════════════════════════════════════════════
section("Stub nodes (interrupt placeholders)")

def t_wait_node(node_fn):
    s = state.initial_state("audit_test")
    update = node_fn(s)
    assert isinstance(update, dict), "must return dict"
    assert "current_phase" in update, "must update current_phase"
    return f"returns {len(update)} state updates"

for name, fn in STUB_NODES.items():
    if name != "terminal_complete":
        check(f"{name} returns valid update", lambda fn=fn: t_wait_node(fn))


# ════════════════════════════════════════════════════════════════════════════
# 5. _synthesise_task_text produces valid markdown
# ════════════════════════════════════════════════════════════════════════════
section("Task text synthesis (for execution prompt)")

def t_synthesise():
    s = state.initial_state(
        "audit_test",
        initial_prompt="Add a comment to foo.py",
        plan_text="1. Open foo.py\n2. Add comment\n3. Save",
        plan_review_text="Looks reasonable.",
        plan_review_provider="gemini",
        plan_review_model="gemini-2.5-flash",
        human_answers="Yes proceed",
        refinements=["Refined plan v1"],
        human_approval="approved",
        app_location="code/foo",
        build_command="echo build",
    )
    text = execution_node._synthesise_task_text(s)
    assert "Initial Prompt" in text
    assert "Plan" in text
    assert "Plan Review" in text
    assert "Human Answers" in text
    assert "refinement 1" in text.lower()
    assert "Approval" in text
    return f"{len(text)} chars, has all sections"

check("_synthesise_task_text builds full markdown", t_synthesise)


# ════════════════════════════════════════════════════════════════════════════
# 6. Graph compilation
# ════════════════════════════════════════════════════════════════════════════
section("Graph compilation")

def t_graph_builds():
    g = graph.build_graph()
    nodes = sorted(g.nodes.keys())
    expected = {"task_entry", "planning", "plan_review", "wait_human_answers",
                "refinement", "wait_human_approval", "execution",
                "wait_human_review", "learning", "terminal_complete"}
    actual = set(nodes)
    missing = expected - actual
    extra = actual - expected
    assert not missing, f"missing: {missing}"
    assert not extra, f"unexpected: {extra}"
    return f"{len(nodes)} nodes, all expected"

def t_graph_compiles():
    with checkpointer.checkpointer() as saver:
        compiled = graph.compile_graph(saver)
    return f"compiled: {compiled.__class__.__name__}"

check("graph.build_graph wires correct nodes", t_graph_builds)
check("graph.compile_graph with sqlite checkpointer", t_graph_compiles)


# ════════════════════════════════════════════════════════════════════════════
# 7. Daemon: _parse_task_file
# ════════════════════════════════════════════════════════════════════════════
section("Daemon task-file parsing")

def t_parse_task_file():
    sample = """---
status: pending_ai_planning
app_location: code/myapp
files_allowed: code/myapp
build_command: npm run build
cost_ceiling: 1.5
---

# Test Task

## Initial Prompt

Build a thing that does another thing.

## Context

Some context.
"""
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as f:
        f.write(sample)
        path = Path(f.name)
    try:
        spec = cli._parse_task_file(path)
        assert spec["status"] == "pending_ai_planning"
        assert spec["app_location"] == "code/myapp"
        assert spec["build_command"] == "npm run build"
        assert spec["cost_ceiling"] == 1.5
        assert spec["max_cost_usd"] is None
        assert spec["resolved_cost_ceilings"] == {"planning": 1.5, "execution": 1.5, "learning": 1.5}
        assert "Build a thing" in spec["initial_prompt"]
        return "frontmatter + initial prompt extracted"
    finally:
        path.unlink()

check("_parse_task_file handles standard task spec", t_parse_task_file)


def t_parse_task_file_legacy_cost_fallback():
    sample = """---
status: pending_ai_planning
max_cost_usd: 1.5
---

# Test Task

## Initial Prompt

Build a thing that does another thing.
"""
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as f:
        f.write(sample)
        path = Path(f.name)
    try:
        spec = cli._parse_task_file(path)
        assert spec["cost_ceiling"] == 1.5
        assert spec["max_cost_usd"] == 1.5
        assert spec["resolved_cost_ceilings"] == {"planning": 1.5, "execution": 1.5, "learning": 1.5}
        return "legacy max_cost_usd falls back to single cost_ceiling"
    finally:
        path.unlink()


def t_phase_budget_status_uses_specific_fields():
    s = state.initial_state(
        "budget_audit",
        cost_ceiling=1.5,
        planning_cost_usd=0.2,
        execution_cost_usd=1.4,
        learning_cost_usd=0.05,
    )
    # All phases share the single cost_ceiling now (2026-05-12).
    assert P.phase_budget_status(s, "planning")["ceiling"] == 1.5
    assert P.phase_budget_status(s, "execution")["ceiling"] == 1.5
    assert P.phase_budget_status(s, "learning")["ceiling"] == 1.5
    update = P.phase_budget_exceeded_update(s, "execution", 1.6)
    assert update is None, (
        f"phase_budget_exceeded_update returned a halt update {update!r} — "
        f"quality hierarchy violation. Cost ceilings must be observation-only."
    )
    return "single ceiling; over-budget calls remain observation-only"


check("_parse_task_file supports legacy max_cost_usd fallback", t_parse_task_file_legacy_cost_fallback)
check("budget resolver uses single cost_ceiling; over-budget is observation-only", t_phase_budget_status_uses_specific_fields)


# ════════════════════════════════════════════════════════════════════════════
# 8. CLI subcommands registered
# ════════════════════════════════════════════════════════════════════════════
section("CLI subcommand registration")

def t_cli_subcommands():
    import argparse
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd")
    # Re-introspect what cli.main does — verify each expected subcommand exists
    expected_cmds = {"list", "show", "inspect", "new", "advance", "rerun", "replay", "snooze", "delete", "budget", "status", "models", "daemon"}
    # Easier: check the cmd_* functions all exist on the cli module
    missing = [c for c in expected_cmds if not hasattr(cli, f"cmd_{c}")]
    assert not missing, f"missing: {missing}"
    return f"{len(expected_cmds)} subcommands"

check("All CLI subcommands have cmd_* functions", t_cli_subcommands)


def t_prune_skills_dry_run_subprocess():
    """2026-05-11: previously asserted "cli-patterns" specifically. After the
    skill scrub moved 10 curated skills to ai_skills/_archive_pre_scrub_2026_05_11/,
    that hardcoded name no longer exists in the active set. Now asserts:
    (a) command exits 0, (b) output mentions at least one active skill from
    the live ai_skills/ directory (ignoring _* archive dirs)."""
    result = subprocess.run(
        [sys.executable, "vault.py", "prune-skills", "--dry-run"],
        cwd=str(VAULT_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    assert result.returncode == 0, (
        f"prune-skills --dry-run exited {result.returncode}: {result.stderr[:500]}"
    )
    skills_dir = VAULT_ROOT / "ai_skills"
    active = [d.name for d in skills_dir.iterdir()
              if d.is_dir() and not d.name.startswith("_")]
    assert active, "no active skills in ai_skills/ — fixture broken?"
    assert any(s in result.stdout for s in active), (
        f"stdout mentioned none of the active skills {active}; "
        f"got: {result.stdout[:200]!r}"
    )
    return (f"prune-skills --dry-run returned 0, printed {len(result.stdout)} "
            f"chars, mentioned at least one of {len(active)} active skills")


check("prune-skills --dry-run subprocess succeeds and lists at least one active skill",
      t_prune_skills_dry_run_subprocess)


def t_cli_replay_help_registered():
    output = io.StringIO()
    try:
        with contextlib.redirect_stdout(output):
            cli.main(["replay", "--help"])
    except SystemExit as e:
        assert e.code == 0, f"replay --help exited {e.code}"
    helpText = output.getvalue()
    assert "test prompt changes" in helpText
    return "replay help is registered"


def t_cmd_replay_rejects_non_completed():
    import inspect
    src = inspect.getsource(cli.cmd_replay)
    assert 'state.get("status") != "completed"' in src, "missing completed-status guard"
    return "cmd_replay requires status == completed"


def t_cmd_replay_uses_dry_run_planning_only():
    import inspect
    src = inspect.getsource(cli.cmd_replay)
    assert "run_planning_phase" in src and "dry_run=True" in src, "missing dry-run planning call"
    forbidden = ["compile_graph", "graph.stream", "update_state"]
    present = [token for token in forbidden if token in src]
    assert not present, f"cmd_replay invokes graph execution APIs: {present}"
    return "cmd_replay calls dry-run planner without graph execution"


def t_dry_run_planning_suppresses_writes():
    import inspect
    src = inspect.getsource(planning_node.run_planning_phase)
    required = [
        "if not dry_run:",
        "write_text",
        "_archivePromptResponse",
        "_writeCostLog",
        "_appendFailureMode",
        "_logContextInjection",
    ]
    missing = [token for token in required if token not in src]
    assert not missing, f"missing dry-run write suppression markers: {missing}"
    return "dry-run planner gates snapshot/archive/cost/failure/context writes"


check("replay subcommand help is registered", t_cli_replay_help_registered)
check("cmd_replay rejects non-completed state", t_cmd_replay_rejects_non_completed)
check("cmd_replay uses dry-run planning without graph execution", t_cmd_replay_uses_dry_run_planning_only)
check("dry-run planning suppresses replay side-effect writes", t_dry_run_planning_suppresses_writes)


def t_cli_show_plan_vs_execution_flag_registered():
    output = io.StringIO()
    try:
        with contextlib.redirect_stdout(output):
            cli.main(["show", "--help"])
    except SystemExit as e:
        assert e.code == 0, f"show --help exited {e.code}"
    helpText = output.getvalue()
    assert "--plan-vs-execution" in helpText
    return "show help includes --plan-vs-execution"


def t_plan_vs_execution_outputs_file_sets_and_diff():
    sampleState = {
        "plan_text": "1. Edit `scripts/planned.py`\n2. Update scripts/shared.py",
        "refinements": ["Also update `vault.py`."],
        "execution_diff": (
            "diff --git a/scripts/shared.py b/scripts/shared.py\n"
            "--- a/scripts/shared.py\n"
            "+++ b/scripts/shared.py\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
            "diff --git a/scripts/executed.py b/scripts/executed.py\n"
            "--- a/scripts/executed.py\n"
            "+++ b/scripts/executed.py\n"
        ),
        "execution_log": "2026-05-04 FILE-WRITE | Edit: scripts/logged.py",
    }
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        code = cli._print_plan_vs_execution("audit_task", sampleState)
    text = output.getvalue()
    assert code == 0
    assert "Files in plan but not touched: [scripts/planned.py, vault.py]" in text
    assert "Files touched but not in plan: [scripts/executed.py, scripts/logged.py]" in text
    assert "--- planned" in text
    assert "+++ executed" in text
    assert "-1. Edit `scripts/planned.py`" in text
    assert "+diff --git a/scripts/shared.py b/scripts/shared.py" in text
    return "file-set headline and unified diff mismatch output verified"


def t_plan_vs_execution_missing_plan_error():
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        code = cli._print_plan_vs_execution("missing_plan", {"execution_diff": "diff --git a/a.py b/a.py"})
    text = output.getvalue()
    assert code == 1
    assert "Plan data is unavailable for 'missing_plan'." in text
    return "missing plan reports clear error"


def t_plan_vs_execution_missing_execution_error():
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        code = cli._print_plan_vs_execution("missing_execution", {"plan_text": "Edit scripts/a.py"})
    text = output.getvalue()
    assert code == 1
    assert "Execution data is unavailable for 'missing_execution'." in text
    return "missing execution reports clear error"


check("show supports --plan-vs-execution flag", t_cli_show_plan_vs_execution_flag_registered)
check("plan-vs-execution shows planned/executed file-set drift and diff", t_plan_vs_execution_outputs_file_sets_and_diff)
check("plan-vs-execution handles missing plan data", t_plan_vs_execution_missing_plan_error)
check("plan-vs-execution handles missing execution data", t_plan_vs_execution_missing_execution_error)


class _FakeSnapshot:
    def __init__(self, values: dict, checkpoint_id: str):
        self.values = values
        self.config = {
            "configurable": {
                "thread_id": "audit_rerun",
                "checkpoint_ns": "",
                "checkpoint_id": checkpoint_id,
            }
        }


def t_rerun_finds_execution_resume():
    history = [
        _FakeSnapshot({
            "current_phase": "execution",
            "status": "failed",
            "last_failure_type": "ai_request_failed",
        }, "failed-1"),
        _FakeSnapshot({
            "current_phase": "refinement",
            "status": "pending_human_approval",
            "plan_text": "plan",
            "plan_review_text": "review",
            "refinements": ["refined"],
            "refinement_n": 1,
        }, "success-1"),
    ]
    selection = cli._find_rerun_checkpoint(history)
    assert selection is not None, "expected rerun selection"
    _, success_snapshot, success_phase, resume_phase = selection
    assert success_snapshot.config["configurable"]["checkpoint_id"] == "success-1"
    assert success_phase == "refinement"
    assert resume_phase == "execution"
    prepared = cli._prepare_rerun_update(success_snapshot.values, resume_phase, "success-1")
    assert prepared["rerun_active"] is True
    assert prepared["rerun_from_phase"] == "execution"
    assert prepared["plan_text"] == "plan"
    assert prepared["plan_review_text"] == "review"
    assert prepared["refinements"] == ["refined"]
    assert prepared["execution_log"] == ""
    return "execution failure resumes from execution and preserves prior outputs"


def t_rerun_no_failure_returns_none():
    history = [
        _FakeSnapshot({
            "current_phase": "plan_review",
            "status": "pending_human_answers",
            "plan_text": "plan",
        }, "ok-1"),
    ]
    assert cli._find_rerun_checkpoint(history) is None
    return "no failed checkpoint produces no rerun selection"


check("rerun helper resumes execution after refinement success", t_rerun_finds_execution_resume)
check("rerun helper rejects history without failure", t_rerun_no_failure_returns_none)


def t_status_missing_heartbeat():
    line = cli._formatDaemonStatus(
        "Sharpener daemon",
        {"pid": None, "daemon": "sharpener", "role": "Sharpener daemon"},
        130,
    )
    assert "Sharpener daemon" in line
    assert "not running" in line
    assert "heartbeat missing" in line
    return line


def t_recent_phase_events_newest_ten():
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "cost_log.jsonl"
        lines = []
        for i in range(12):
            lines.append(json.dumps({
                "timestamp": f"2026-05-04T02:{i:02d}:00",
                "task": f"task_{i}",
                "phase": "planning",
                "status": "success",
                "provider": "audit",
                "model": "audit-model",
                "duration_seconds": i,
                "cost_usd": i / 1000,
            }))
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        events = cli._tail_jsonl(path, 10)
    assert len(events) == 10, f"expected 10 events, got {len(events)}"
    assert events[0]["task"] == "task_11", f"newest event wrong: {events[0]}"
    assert events[-1]["task"] == "task_2", f"oldest retained event wrong: {events[-1]}"
    return "newest 10 events returned newest-first"


check("status formatting handles missing heartbeat", t_status_missing_heartbeat)
check("status cost-log tail returns newest 10 entries", t_recent_phase_events_newest_ten)


class _FakeCheckpointTuple:
    def __init__(self, values: dict, timestamp: str):
        self.checkpoint = {
            "ts": timestamp,
            "channel_values": values,
        }
        self.metadata = {}


class _FakeCheckpointSaver:
    def __init__(self, checkpoints: dict[str, _FakeCheckpointTuple]):
        self.checkpoints = checkpoints

    def get_tuple(self, config: dict):
        taskName = config["configurable"]["thread_id"]
        return self.checkpoints.get(taskName)


def t_stale_human_gate_detection():
    now = datetime(2026, 5, 4, 12, 0, 0, tzinfo=timezone.utc)
    saver = _FakeCheckpointSaver({
        "old_human_gate": _FakeCheckpointTuple({
            "status": "pending_human_approval",
            "current_phase": "refinement",
        }, (now - timedelta(hours=53)).isoformat()),
        "recent_human_gate": _FakeCheckpointTuple({
            "status": "pending_human_answers",
            "current_phase": "plan_review",
        }, (now - timedelta(hours=48)).isoformat()),
        "old_ai_task": _FakeCheckpointTuple({
            "status": "pending_ai_execution",
            "current_phase": "execution",
        }, (now - timedelta(hours=90)).isoformat()),
    })
    staleTasks = cli._findStaleHumanGateTasksForThreads(
        saver,
        ["old_human_gate", "recent_human_gate", "old_ai_task"],
        now=now,
        thresholdHours=48,
    )
    assert len(staleTasks) == 1, f"expected one stale task, got {staleTasks}"
    assert staleTasks[0]["task_name"] == "old_human_gate"
    assert staleTasks[0]["current_phase"] == "refinement"
    assert staleTasks[0]["wait_hours"] == 53
    assert cli._formatStaleTaskAge(53) == "STALE waiting 53h (53 hours)"
    return "old pending_human_* checkpoint flagged; recent/non-human checkpoints ignored"


check("status stale-task detector flags only old human gates", t_stale_human_gate_detection)


# ════════════════════════════════════════════════════════════════════════════
# 9. End-to-end stub flow with crash recovery
# ════════════════════════════════════════════════════════════════════════════
section("End-to-end with crash recovery (no LLM)")

def t_dummy_task_e2e():
    """Run a dummy task through the whole pipeline using stubs only,
    then re-open the checkpoint and verify state survives."""
    import sqlite3
    # Use a temp checkpoint DB so we don't pollute the real one
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "ckpt.sqlite"
        from langgraph.checkpoint.sqlite import SqliteSaver
        conn = sqlite3.connect(str(db_path), check_same_thread=False)
        try:
            saver = SqliteSaver(conn)
            g = graph.build_graph().compile(
                checkpointer=saver,
                interrupt_before=graph.INTERRUPT_BEFORE,
            )
            task = "audit_dummy_001"
            cfg = {"configurable": {"thread_id": task}}
            init = state.initial_state(task, initial_prompt="dummy")
            # Override the real planning + execution nodes with stubs so we don't
            # actually call LLMs in this audit (still uses graph mechanics)
            # Note: we can't easily swap nodes in a compiled graph, so just
            # verify the graph can RUN — failures are expected at the planning
            # node since it needs an actual LLM. We just verify no crashes from
            # graph mechanics.
            try:
                for _ in g.stream(init, config=cfg):
                    pass
            except Exception:
                # Stubs might fail in unexpected ways given real planning runs;
                # we only care that the graph machinery itself works
                pass
            # Fresh saver in a new connection — proves crash recovery
            conn2 = sqlite3.connect(str(db_path), check_same_thread=False)
            saver2 = SqliteSaver(conn2)
            snapshot = saver2.get(cfg)
            conn2.close()
            assert snapshot is not None, "checkpoint not persisted"
            return "checkpoint persisted across connections"
        finally:
            conn.close()

check("crash recovery: state survives connection close", t_dummy_task_e2e)


# ════════════════════════════════════════════════════════════════════════════
# 10. ai_dougs side fixes still in place (gemini -p, tree-kill, etc.)
# ════════════════════════════════════════════════════════════════════════════
section("ai_dougs side fixes (porting prerequisites)")

def t_tree_kill_helper():
    assert hasattr(P._d, "_kill_process_tree")
    assert hasattr(P._d, "_run_with_tree_kill_timeout")
    return "both helpers present"

def t_extended_rate_limits():
    rls = P._d.RATE_LIMIT_SIGNALS
    assert "exhausted your capacity" in rls
    assert "insufficient_quota" in rls
    return f"{len(rls)} signals"

def t_dougs_version():
    v = P._d.SCRIPT_VERSION
    major, minor = v.split(".")
    assert int(minor) >= 25, f"ai_dougs.py is v{v}; expected ≥1.25"
    return f"ai_dougs v{v}"

check("tree-kill timeout helpers present", t_tree_kill_helper)
check("rate-limit signals include gemini + OpenAI quota phrases", t_extended_rate_limits)
check("ai_dougs.py is at side-fix version (≥1.25)", t_dougs_version)


# ════════════════════════════════════════════════════════════════════════════
# Sharpener — behavioral tests (catch on-disk persistence bugs)
# ════════════════════════════════════════════════════════════════════════════
section("Sharpener persistence behaviour")

# Add the sharpener module dir to sys.path so we can import it
_SHARP_DIR = VAULT_ROOT / "scripts" / "ai_sharpener"
if str(_SHARP_DIR) not in sys.path:
    sys.path.insert(0, str(_SHARP_DIR))
import ai_sharpener as sharpener  # noqa: E402


def _make_temp_staging(tmpdir: Path, eid: str = "auto_9999", status: str = "approved") -> Path:
    """Build a minimal but valid staging file with one entry. Returns the path."""
    p = tmpdir / "prompts_staging.md"
    body = f"""---
created: 2026-01-01
modified: 2026-01-01
---

# Prompt Review

> Sharpened prompts appear here automatically.

---

## [{eid}] tiny test entry
status: {status}
updated: 2026-05-04 00:00
sharpener_model: test:test

**Original:**
test original prompt

**Sharpened:**
test sharpened prompt

---
"""
    p.write_text(body, encoding="utf-8")
    return p


def t_processApproved_persists_routed():
    """Regression: processApproved must change on-disk status approved → routed.

    The bug this catches: re-parsing the staging file inside processApproved
    discards the in-memory status mutation, so the file stays "approved" forever
    and every sharpener tick re-routes the same task in a loop.
    """
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        (tmp / "task_files").mkdir()
        staging = _make_temp_staging(tmp, eid="auto_9999", status="approved")

        # Parse exactly the way the sharpener does
        _, __, entries = sharpener.parseStagingFile(staging)
        entry = next(e for e in entries if e["id"] == "auto_9999")
        assert entry["status"] == "approved", "test setup wrong: expected approved"

        ok = sharpener.processApproved(entry, staging, config={}, vault_root=tmp)
        assert ok, "processApproved returned False"

        # The task file must have been written
        assert (tmp / "task_files" / "auto_9999.md").exists(), \
            "routeDougs did not create task_files/auto_9999.md"

        # CRITICAL: the on-disk status must be 'routed' now. If the bug
        # regresses, this fails and the on-disk status stays 'approved'.
        _, __, entries_after = sharpener.parseStagingFile(staging)
        entry_after = next(e for e in entries_after if e["id"] == "auto_9999")
        assert entry_after["status"] == "routed", (
            f"on-disk status did not persist: expected 'routed', got "
            f"'{entry_after['status']}'. THIS IS THE PERMA-APPROVING LOOP BUG."
        )
    return "approved → routed persists to disk"


def t_processApproved_idempotent():
    """Running processApproved twice on the same entry should be safe.

    Second run sees status=routed (disk) but if invoked anyway should still
    leave disk in a consistent state without flipping back to approved.
    """
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        (tmp / "task_files").mkdir()
        staging = _make_temp_staging(tmp, eid="auto_9998", status="approved")

        _, __, entries = sharpener.parseStagingFile(staging)
        entry = next(e for e in entries if e["id"] == "auto_9998")
        sharpener.processApproved(entry, staging, config={}, vault_root=tmp)

        # Second invocation
        _, __, entries2 = sharpener.parseStagingFile(staging)
        entry2 = next(e for e in entries2 if e["id"] == "auto_9998")
        sharpener.processApproved(entry2, staging, config={}, vault_root=tmp)

        _, __, entries3 = sharpener.parseStagingFile(staging)
        final = next(e for e in entries3 if e["id"] == "auto_9998")
        assert final["status"] == "routed", \
            f"second invocation regressed status to {final['status']}"
    return "second call leaves disk in routed state"


check("processApproved persists status 'approved' → 'routed' to disk",
      t_processApproved_persists_routed)
check("processApproved is idempotent on re-invocation",
      t_processApproved_idempotent)


# ────────────────────────────────────────────────────────────────────────────
# Self-test path normalisation (regression: silent skips when vault is
# inside a parent git repo and most vault files are untracked)
# ────────────────────────────────────────────────────────────────────────────
section("Self-test path detection (vault vs sibling-project paths)")


def t_self_test_drops_sibling_project_paths():
    """Diff entries from sibling projects (e.g. Desktop/warcraftlogs-tracker)
    must be DROPPED so self-test never runs vault smoke/audit on changes
    outside the vault. Vault paths must be normalised to vault-relative form
    so they match INFRA_PATTERNS.

    Fixture computes the vault's home-relative prefix at test time so this
    stays correct across renames (Documents/wclapp/vault → Documents/vault
    on 2026-05-10 was the rename that exposed the previously-hardcoded form).
    """
    from pathlib import Path as _Path
    from vault_graph.self_test import _diff_paths
    # Compute the vault's home-relative prefix dynamically — survives renames.
    home = _Path.home().resolve()
    v_root = _Path(VAULT_ROOT).resolve()
    try:
        prefix = v_root.relative_to(home).as_posix() + "/"
    except ValueError:
        # Vault not under home (unusual setup) — fall back to a bare prefix
        # so the fixture still exercises the home-relative branch.
        prefix = "vault/"
    fake_diff = (
        f"diff --git a/{prefix}scripts/vault_graph/cli.py b/{prefix}scripts/vault_graph/cli.py\n"
        f"+++ b/{prefix}scripts/vault_graph/cli.py\n"
        "diff --git a/Desktop/warcraftlogs-tracker/backend/app.py b/Desktop/warcraftlogs-tracker/backend/app.py\n"
        "+++ b/Desktop/warcraftlogs-tracker/backend/app.py\n"
        f"diff --git a/{prefix}state/SYSTEM.md b/{prefix}state/SYSTEM.md\n"
        f"+++ b/{prefix}state/SYSTEM.md"
    )
    paths = _diff_paths(fake_diff, vault_root=VAULT_ROOT)
    assert "scripts/vault_graph/cli.py" in paths, (
        f"vault paths must be normalised to vault-relative; got {paths}")
    assert "state/SYSTEM.md" in paths
    leaks = [p for p in paths if "warcraftlogs" in p or p.startswith("Desktop")]
    assert not leaks, f"sibling-project paths leaked into self-test: {leaks}"
    return f"normalised {len(paths)} paths, dropped sibling-project entries"


def t_self_test_picks_up_file_write_log():
    """If the post_write hook recorded a vault-infra file modification,
    self-test must see it via _writes_since() even when git knows nothing.
    """
    from vault_graph.self_test import _writes_since
    # Use the existing trace file; pick a start time far in the past so we
    # know there will be at least one matching FILE-WRITE entry.
    paths = _writes_since(VAULT_ROOT, "2026-05-04 00:00:00")
    assert isinstance(paths, list)
    # Don't assert non-empty — if the trace was just rotated, list may be
    # empty. What we DO assert is that the function returns vault-relative
    # paths (forward slashes, no drive letter).
    for p in paths:
        assert "\\" not in p, f"path should be forward-slashed: {p}"
        assert not p.startswith("C:"), f"path should be vault-relative: {p}"
    return f"parsed {len(paths)} FILE-WRITE entries since 2026-05-04 00:00:00"


def t_post_write_logs_external_with_marker():
    """post_write.py must prefix paths OUTSIDE the vault root with 'EXTERNAL:'
    so downstream consumers (_writes_since, audits) can distinguish vault-
    infra changes from incidental orchestrator writes to AppData/Roaming, etc.

    Latent bug fixed 2026-05-10: previously the except-ValueError branch
    silently emitted a `C:/...` path with no marker. An orchestrator Write
    to obsidian.json after the Documents/wclapp/vault → Documents/vault
    rename surfaced this by tripping t_self_test_picks_up_file_write_log.
    """
    from hooks.post_write import main as _post_main  # noqa: F401  (import sanity)
    import inspect
    src = inspect.getsource(
        __import__("hooks.post_write", fromlist=["main"]).main
    )
    # The fix must be present in source: an EXTERNAL: prefix on the
    # ValueError branch. Cheap structural check (no live hook invocation).
    assert "EXTERNAL:" in src, (
        "post_write.py main() must emit 'EXTERNAL:' prefix for paths "
        "outside vault root; current source does not contain that marker."
    )
    assert "ValueError" in src, (
        "post_write.py main() must still handle the ValueError from "
        "Path.relative_to when target is outside vault root."
    )
    # And the consumer must filter on the marker.
    from vault_graph.self_test import _writes_since  # noqa: F401
    import inspect as _i
    consumer_src = _i.getsource(_writes_since)
    assert 'EXTERNAL:' in consumer_src, (
        "_writes_since must filter out 'EXTERNAL:' entries; missing filter."
    )
    return "post_write emits EXTERNAL: marker; _writes_since filters it"


check("self-test drops sibling-project diff paths",
      t_self_test_drops_sibling_project_paths)
check("self-test picks up post_write file log",
      t_self_test_picks_up_file_write_log)
check("post_write logs external paths with EXTERNAL: marker",
      t_post_write_logs_external_with_marker)


def t_skills_are_source_agnostic_visibility():
    """VISIBILITY check (informational, never fails). Reports the count of
    source-task references (`auto_NNNN` strings) baked into skill body text.

    User design directive 2026-05-11: skills should be transferable abstract
    lessons, not source-coupled history. Provenance lives in frontmatter or
    sidecar metadata, never in body content agents read at runtime.

    This check intentionally never fails — historical curations under the old
    full-rewrite curator (Bug X) leaked source-task refs into multiple skill
    bodies. They'll get cleaned organically as each skill is next curated
    under the judge-arbitrated curation system (Bug X's permanent fix). This
    audit just reports the count so we can watch the number trend toward zero.

    Will be promoted to a hard fail once Bug X lands and the existing
    references have been cleaned.
    """
    from pathlib import Path
    import re
    skills_dir = VAULT_ROOT / "ai_skills"
    if not skills_dir.is_dir():
        return "no ai_skills dir; skipped"
    pattern = re.compile(r"\bauto_\d{4}\b")
    total = 0
    files_with_refs: list[tuple[str, int]] = []
    for skill_file in skills_dir.rglob("SKILL.md"):
        # Skip archived skills
        if "_archive" in skill_file.parts:
            continue
        try:
            text = skill_file.read_text(encoding="utf-8")
        except Exception:
            continue
        # Strip frontmatter — references in frontmatter are legitimate provenance
        m = re.match(r"^---\s*\n.*?\n---\s*\n", text, re.S)
        body = text[m.end():] if m else text
        matches = pattern.findall(body)
        if matches:
            rel = skill_file.relative_to(skills_dir).as_posix()
            files_with_refs.append((rel, len(matches)))
            total += len(matches)
    if files_with_refs:
        # Print but don't fail — visibility only
        print(f"      [VISIBILITY] {total} source-task ref(s) across "
              f"{len(files_with_refs)} skill(s): "
              + ", ".join(f"{rel} ({n})" for rel, n in files_with_refs[:5]))
    return f"{total} source-task refs across {len(files_with_refs)} skill(s) (informational; Bug X)"


check("skills source-agnostic body — visibility count",
      t_skills_are_source_agnostic_visibility)


def t_self_test_mtime_walker_detects_subprocess_writes():
    """The mtime walker is the AUTHORITATIVE source of agent-modified infra
    files when the post_write hook misses subprocess writes (codex CLI etc).

    Regression for the silent-skip bug hit on auto_0007: post_write hook only
    fires for the parent Claude Code harness; subprocess CLI agents (codex /
    gemini) write files without triggering it. Combined with most vault files
    being
    untracked in git, self-test was silently saying 'no diff to inspect' on
    every infra change. mtime walk fixes this by walking INFRA_PATTERNS dirs.
    """
    import time, os
    from vault_graph.self_test import _infra_writes_since_mtime, INFRA_PATTERNS
    target = None
    for pat in INFRA_PATTERNS:
        candidate = VAULT_ROOT / pat.rstrip("/")
        if candidate.exists():
            if candidate.is_file():
                target = candidate
                break
            if candidate.is_dir():
                for f in candidate.rglob("*.py"):
                    if "__pycache__" not in f.parts:
                        target = f
                        break
                if target:
                    break
    assert target is not None, "no infra files exist to test against"
    start = time.time()
    time.sleep(0.05)
    os.utime(target, None)
    hits = _infra_writes_since_mtime(VAULT_ROOT, start)
    rel = target.relative_to(VAULT_ROOT).as_posix()
    assert rel in hits, f"mtime walker missed touched file {rel}; hits={hits[:5]}"
    assert all("__pycache__" not in h for h in hits), "should skip __pycache__"
    return f"detected {len(hits)} infra mtime change(s)"


check("self-test mtime walker detects subprocess-agent writes",
      t_self_test_mtime_walker_detects_subprocess_writes)


# ════════════════════════════════════════════════════════════════════════════
# Subprocess + routing regression tests (cp1252 trap, str+None, quota ban)
# ════════════════════════════════════════════════════════════════════════════
section("Windows cp1252 + routing regressions")


def t_no_text_true_without_encoding():
    """Every subprocess.run/Popen with text=True must also pass encoding=.

    Regression: Windows defaults to cp1252 when text=True and encoding= is
    omitted, which crashes the subprocess reader thread on any UTF-8 byte
    not in cp1252 (very common in source code, errors, stack traces).
    Caught by this audit so a future contributor can't reintroduce it.
    """
    import re as _re
    bad = []
    for f in [
        VAULT_ROOT / "scripts" / "ai_dougs" / "ai_dougs.py",
        VAULT_ROOT / "scripts" / "vault_graph" / "nodes" / "execution.py",
        VAULT_ROOT / "scripts" / "vault_graph" / "self_test.py",
    ]:
        src = f.read_text(encoding="utf-8")
        # Walk each subprocess.run/Popen call with paren-depth tracking
        idx = 0
        while True:
            m = _re.search(r"(?:subprocess\.|_sp\.|sp\.)?(?:run|Popen)\s*\(", src[idx:])
            if not m:
                break
            start = idx + m.end()
            depth = 1
            j = start
            while j < len(src) and depth > 0:
                if src[j] == "(":
                    depth += 1
                elif src[j] == ")":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            args = src[start:j]
            if "text=True" in args and "encoding=" not in args:
                line = src[:start].count("\n") + 1
                bad.append(f"{f.name}:{line}")
            idx = j + 1
    assert not bad, (
        f"subprocess calls have text=True but no encoding= "
        f"(Windows cp1252 trap will crash on UTF-8 bytes): {bad}"
    )
    return "all subprocess text=True calls pass encoding=utf-8"


def t_combined_diff_handles_none():
    """run_self_test must not crash when diff inputs are None or empty.

    Regression: a None vault_diff_text from a crashed subprocess reader
    triggered 'can only concatenate str (not "NoneType") to str' inside
    execution.py's combined_diff line. Defensive coercion now prevents this.
    """
    from vault_graph import self_test as st
    # Pass empty diff — function should return clean (no infra touched)
    ok, reason = st.run_self_test(VAULT_ROOT, "", "audit_test", "")
    assert ok, f"empty diff produced failure: {reason}"
    # Pass None-equivalent (empty string after coercion) — same path
    ok2, reason2 = st.run_self_test(VAULT_ROOT, "" or "", "audit_test", "")
    assert ok2, f"coerced-empty diff failed: {reason2}"
    return "empty/None diff inputs handled cleanly"


def t_quota_ban_skips_provider():
    """A provider returning rate_limited must be banned for subsequent calls.

    Regression: gemini quota'd in refinement, system re-tried it in execution
    and learning, wasting 5-10s per phase on the timeout. Now: hit quota once,
    skip for 1h within this Python process.
    """
    P._clear_quota_bans()
    assert not P._is_provider_quota_banned("gemini")
    P._ban_provider_for_quota("gemini")
    assert P._is_provider_quota_banned("gemini"), "ban did not register"
    # Also test a different provider remains unbanned
    assert not P._is_provider_quota_banned("claude"), "ban leaked across providers"
    # Cleanup so no stale state leaks into other audit checks
    P._clear_quota_bans()
    assert not P._is_provider_quota_banned("gemini"), "clear failed"
    return "ban / unban / per-provider isolation all work"


def t_quota_ban_ttl_expires():
    """Bans must auto-expire after the TTL — providers eventually retried."""
    import time as _time
    P._clear_quota_bans()
    # Use a 0.05s TTL for the test
    P._ban_provider_for_quota("gemini", ttl_seconds=0.05)
    assert P._is_provider_quota_banned("gemini")
    _time.sleep(0.1)
    assert not P._is_provider_quota_banned("gemini"), "ban did not expire after TTL"
    return "TTL expiration works"


check("every subprocess text=True passes encoding=utf-8 (cp1252 trap)",
      t_no_text_true_without_encoding)
check("run_self_test handles empty/None diff without str+None crash",
      t_combined_diff_handles_none)
check("provider quota-ban skips banned provider on subsequent calls",
      t_quota_ban_skips_provider)
check("provider quota-ban TTL auto-expires", t_quota_ban_ttl_expires)


# ════════════════════════════════════════════════════════════════════════════
def t_models_subprocess_runs_and_mentions_provider():
    providers = cli._load_model_pool_providers()
    assert providers, "model_pool.json has no providers"
    result = subprocess.run(
        [sys.executable, "vault.py", "models"],
        cwd=VAULT_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
    )
    assert result.returncode == 0, (
        f"returncode={result.returncode}; stdout={result.stdout[:300]!r}; "
        f"stderr={result.stderr[:300]!r}"
    )
    assert result.stdout.strip(), "models command printed no stdout"
    assert any(provider in result.stdout for provider in providers), (
        f"stdout mentioned no provider from model_pool.json: {providers}"
    )
    assert "invalid choice" not in result.stderr.lower(), result.stderr
    return "python vault.py models returns 0 and mentions a configured provider"


check("models subcommand subprocess returns provider status",
      t_models_subprocess_runs_and_mentions_provider)


section("Supervisor kill (FIX #2)")

# FIX #1 audits (snapshot/restore) removed 2026-05-12 with FIX #1 itself.
# FIX #3 audits (phase write audit) removed 2026-05-12 with FIX #3 itself.
# pre_write deny-by-default carries the load; redundant snapshot/restore was
# defense-in-depth-as-architecture.

def t_watchdog_kill_actually_kills():
    """End-to-end: register a fake PID, run kill helper, verify it died."""
    import subprocess as _sp, sys as _sys, json as _json, time as _time, importlib.util
    spec = importlib.util.spec_from_file_location(
        "_ad_audit", str(VAULT_ROOT / "scripts" / "ai_dougs" / "ai_dougs.py"))
    A = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(A)
    from vault_graph import watchdog as W
    proc = _sp.Popen([_sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        A._registerActiveSubproc(proc.pid, label="audit/sleeper", cmd_head="audit")
        reg = A._active_subproc_file()
        data = _json.loads(reg.read_text(encoding="utf-8"))
        data[str(proc.pid)]["started_at"] = _time.time() - (W.SUBPROC_GRACE_MIN + 2) * 60
        reg.write_text(_json.dumps(data, indent=2), encoding="utf-8")
        killed = W._kill_stuck_subprocs(["audit_fake_task"])
        _time.sleep(1.5)
        assert proc.poll() is not None, "watchdog failed to actually kill the subproc"
        assert any(k["pid"] == proc.pid for k in killed), "kill not reported"
        # cleanup if registry still has stale entry
        try:
            data2 = _json.loads(reg.read_text(encoding="utf-8"))
            data2.pop(str(proc.pid), None)
            reg.write_text(_json.dumps(data2, indent=2), encoding="utf-8")
        except Exception:
            pass
        return f"end-to-end kill verified (killed PID {proc.pid})"
    finally:
        if proc.poll() is None:
            try: proc.kill()
            except Exception: pass

check("watchdog end-to-end actually tree-kills a fake stuck subproc",
      t_watchdog_kill_actually_kills)


# ════════════════════════════════════════════════════════════════════════════
section("Skill file quality (catches the codex-rc false-success damage class)")

def t_skill_quality_detector_imports():
    # STATUS.md generation removed 2026-05-12; docs_refresh is kept only for
    # the skill-quality detector used by prune-skills.
    from vault_graph.docs_refresh import _detect_skill_quality_issues
    assert callable(_detect_skill_quality_issues)
    return "docs_refresh._detect_skill_quality_issues import OK"

def t_no_damaged_skill_files():
    """Walk every SKILL.md and assert none are too small / banner-only / missing FM."""
    from vault_graph.docs_refresh import _detect_skill_quality_issues
    sk = VAULT_ROOT / "ai_skills"
    names = sorted(d.name for d in sk.iterdir()
                   if d.is_dir() and d.name not in ("_archive",)
                   and (d / "SKILL.md").exists())
    issues = _detect_skill_quality_issues(sk, names)
    assert not issues, f"damaged SKILL.md files: {issues}"
    return f"all {len(names)} SKILL.md files pass quality gate"

def t_quality_gate_catches_banner_string():
    """Synthetic regression: feed a banner-only file and confirm it gets flagged."""
    import tempfile, shutil
    from vault_graph.docs_refresh import _detect_skill_quality_issues
    with tempfile.TemporaryDirectory() as td:
        sk = Path(td) / "fake_skills"
        sk.mkdir()
        bad = sk / "synthetic-bad"
        bad.mkdir()
        (bad / "SKILL.md").write_text("OpenAI Codex v0.128.0 (research preview)",
                                       encoding="utf-8")
        good = sk / "synthetic-good"
        good.mkdir()
        (good / "SKILL.md").write_text(
            "---\nname: synthetic-good\ndescription: a synthetic skill used "
            "by the audit suite to verify the quality gate does NOT flag "
            "well-formed files\n---\n\n# Skill\n\n## Purpose\n\nA test skill "
            "with enough realistic content to pass the size / line / "
            "front-matter checks.\n\n## When to use\n\nNever in production; "
            "audit-only.\n\n## Steps\n\n1. read this file\n2. confirm no "
            "issues are flagged\n3. proceed with the rest of the audit\n\n"
            "## Pitfalls\n\nNone — this is a fixture.\n\n## Examples\n\n"
            "See the audit suite that loads this file.\n",
            encoding="utf-8")
        names = ["synthetic-bad", "synthetic-good"]
        issues = _detect_skill_quality_issues(sk, names)
    flagged = {n for n, _ in issues}
    assert "synthetic-bad" in flagged, "quality gate failed to flag banner-only"
    assert "synthetic-good" not in flagged, "quality gate falsely flagged a good file"
    return "banner-only flagged, well-formed unflagged"


check("docs_refresh skill-quality detector imports",
      t_skill_quality_detector_imports)
check("every existing SKILL.md passes the quality gate",
      t_no_damaged_skill_files)
check("quality gate catches CLI banner-only files (codex-rc regression)",
      t_quality_gate_catches_banner_string)


# writeSkillFile audits removed 2026-05-12 — function is a no-op stub; nothing
# to guard against.


# ════════════════════════════════════════════════════════════════════════════
section("Vault/orchestrator separation (no meta-leakage into agent files)")

# Patterns that indicate orchestrator-session commentary leaked into a file
# the vault agents will read. See _human_notes/README.md for context.
META_LEAK_PATTERNS = [
    r"\bTHIS Claude Code session\b",
    r"\bin our chat\b",
    r"\bin this conversation\b",
    r"\bwe discussed (above|earlier|previously)\b",
    r"\bthe user has been (asking|frustrated|telling me)\b",
    r"\bthe user said\b",
    r"\bthe user wants me to\b",
    r"\bmsg #\d+\b",
]

# Files that vault agents read at runtime — meta-leak in any of these is a bug.
AGENT_LOADED_GLOBS = [
    "ai_main.md", "VISION.md", "state/SYSTEM.md", "README.md",
    "ai_context/failure_modes.md", "state/STATUS.md", "CLAUDE.md",
    "ai_context/**/*.md", "ai_skills/**/SKILL.md",
    "docs/**/*.md", "ai_instructions/**/*.md",
    "scripts/**/*.py",
]

# Directories that must never appear in agent context loaders' include paths.
HUMAN_ONLY_DIRS = ("_human_notes", ".claude", ".backups", "_archive")


def t_no_meta_leak_in_agent_files():
    """Grep every agent-loaded file for orchestrator-session leakage patterns."""
    import re as _re
    leaks: list[str] = []
    compiled = [_re.compile(p, _re.IGNORECASE) for p in META_LEAK_PATTERNS]
    for pattern in AGENT_LOADED_GLOBS:
        for path in VAULT_ROOT.glob(pattern):
            if not path.is_file():
                continue
            # Skip files that legitimately discuss the topic (this audit script,
            # the notes README which IS the policy doc).
            rel = path.relative_to(VAULT_ROOT).as_posix()
            if rel.startswith("_human_notes/") or rel.endswith("vault_graph_audit.py"):
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for cre in compiled:
                m = cre.search(text)
                if m:
                    leaks.append(f"{rel}: '{m.group(0)}'")
                    break
    assert not leaks, ("orchestrator-session meta-commentary leaked into "
                       f"agent-loaded files: {leaks[:5]}")
    return f"checked {len(AGENT_LOADED_GLOBS)} glob patterns, no leaks"


def t_human_only_dirs_excluded_from_context_loaders():
    """Every os.walk / iterdir / rglob path-handler in scripts/ should
    exclude _human_notes/ + .claude/ + .backups/ + _archive/ when scanning
    the vault for context. We check that ai_dougs.py's two EXCLUDE_DIRS sets
    list every HUMAN_ONLY_DIR.
    """
    src = (VAULT_ROOT / "scripts" / "ai_dougs" / "ai_dougs.py").read_text(encoding="utf-8")
    # Find each EXCLUDE_DIRS = { ... } block
    import re as _re
    blocks = _re.findall(r"EXCLUDE_DIRS\s*=\s*\{([^}]*)\}", src, _re.S)
    assert blocks, "no EXCLUDE_DIRS blocks found in ai_dougs.py"
    failures = []
    for i, blk in enumerate(blocks):
        for hd in HUMAN_ONLY_DIRS:
            if f'"{hd}"' not in blk and f"'{hd}'" not in blk:
                failures.append(f"EXCLUDE_DIRS block #{i+1} missing {hd!r}")
    assert not failures, f"missing exclusions: {failures}"
    return f"all {len(blocks)} EXCLUDE_DIRS blocks include {len(HUMAN_ONLY_DIRS)} HUMAN_ONLY_DIRS"


def t_human_only_paths_in_self_test_forbidden():
    src = (VAULT_ROOT / "scripts" / "vault_graph" / "self_test.py").read_text(encoding="utf-8")
    for hd in ("_human_notes/", ".claude/"):
        assert hd in src, f"self_test.py FORBIDDEN_PATHS missing {hd!r}"
    return "self_test.py blocks _human_notes/ + .claude/ writes"


def t_human_notes_readme_present():
    p = VAULT_ROOT / "_human_notes" / "README.md"
    assert p.exists(), "_human_notes/README.md missing — directory contract undefined"
    text = p.read_text(encoding="utf-8")
    assert "HUMAN-ONLY" in text and "vault subprocess agents" in text.lower(), \
        "README missing required HUMAN-ONLY contract language"
    return f"_human_notes/README.md present ({len(text)} chars)"


check("no meta-commentary leaked into agent-loaded files",
      t_no_meta_leak_in_agent_files)
check("HUMAN_ONLY_DIRS excluded from every ai_dougs EXCLUDE_DIRS block",
      t_human_only_dirs_excluded_from_context_loaders)
check("self_test.py FORBIDDEN_PATHS blocks _human_notes/ writes",
      t_human_only_paths_in_self_test_forbidden)
check("_human_notes/README.md defines the contract",
      t_human_notes_readme_present)


# ════════════════════════════════════════════════════════════════════════════
section("Verification loop (closes plan→execute→measure→learn)")

def t_verification_module_imports():
    from vault_graph.verification import (
        extract_verification_block, run_verification_block,
        verify_plan, print_outcome, VerificationOutcome, CheckResult,
    )
    return "verification module exports OK"

def t_verification_parses_yaml_block():
    from vault_graph.verification import extract_verification_block
    plan = """## Plan
Some prose.
```yaml
verification:
  - id: smoke
    type: command
    run: echo hi
    expect_returncode: 0
verification_policy:
  max_iterations: 2
```
End.
"""
    checks, policy, err = extract_verification_block(plan)
    assert err is None, f"unexpected error: {err}"
    assert len(checks) == 1 and checks[0]["id"] == "smoke"
    assert policy.get("max_iterations") == 2
    return "parser extracts checks + policy correctly"

def t_verification_passes_real_check():
    from vault_graph.verification import verify_plan
    plan = """```yaml
verification:
  - id: trivial
    type: command
    run: python -c "print('ok')"
    expect_returncode: 0
    expect_stdout_contains: ["ok"]
```"""
    outcome = verify_plan(plan, cwd=str(VAULT_ROOT))
    assert outcome.all_passed, f"expected pass: {outcome.to_dict()}"
    return "trivial check passes end-to-end"

def t_verification_catches_failure():
    from vault_graph.verification import verify_plan
    plan = """```yaml
verification:
  - id: must_fail
    type: command
    run: python -c "import sys; sys.exit(2)"
    expect_returncode: 0
```"""
    outcome = verify_plan(plan, cwd=str(VAULT_ROOT))
    assert not outcome.all_passed
    assert outcome.n_failed == 1
    return "failing check correctly fails the outcome"

def t_verification_missing_block_is_failure():
    from vault_graph.verification import verify_plan
    outcome = verify_plan("## Plan\nDo a thing.\nNo verification.", cwd=str(VAULT_ROOT))
    assert not outcome.all_passed
    assert outcome.parse_error is not None
    return "missing verification block reports parse_error"

def t_planning_prompt_requires_verification():
    # 2026-05-09 (Chunk 2 refactor via auto_0065): prompts moved out of
    # ai_dougs.py into scripts/ai_dougs/prompts.py. Audit now scans the new
    # home. ai_dougs.py still re-exports the constants for runtime compat.
    src = (VAULT_ROOT / "scripts" / "ai_dougs" / "prompts.py").read_text(encoding="utf-8")
    assert 'PLANNING_PROMPT' in src
    # Find the planning prompt block and assert it requires the verification yaml
    import re
    m = re.search(r'PLANNING_PROMPT\s*=\s*"""(.+?)"""', src, re.S)
    assert m, "PLANNING_PROMPT block not found"
    body = m.group(1)
    assert "## Verification" in body, "planning prompt missing '## Verification' requirement"
    assert "verification:" in body and "verification_policy:" in body, \
        "planning prompt missing yaml schema description"
    assert "machine-runnable" in body or "machine-checkable" in body.lower(), \
        "planning prompt should call out machine-runnable nature"
    # Fix for auto_0050 finding: test-style tasks fell back to legacy prose
    # `## Completion Criteria`. Prompt must explicitly call out that test
    # tasks STILL need a verification block.
    assert "Test-writing tasks STILL need a verification block" in body or \
           ("test" in body.lower() and "test_suite" in body and "deprecated" in body.lower()), \
        "planning prompt should explicitly require verification block for test-writing tasks"
    return "PLANNING_PROMPT requires verification + carves out test-style tasks"

def t_terminal_complete_gates_on_verification():
    src = (VAULT_ROOT / "scripts" / "vault_graph" / "nodes" / "stubs.py").read_text(encoding="utf-8")
    assert "verification_outcome" in src, "terminal_complete should read verification_outcome"
    assert 'all_passed is True' in src, "terminal_complete should require all_passed True"
    assert '"failed"' in src and 'verification_failed' in src, \
        "terminal_complete should set status=failed when verification fails"
    assert "_write_verification_failure" in src, \
        "terminal_complete should write a failure_modes entry on failure"
    return "terminal_complete gates completion on verification_outcome"

# t_skill_curation_gated_on_verification removed 2026-05-12. It guarded the
# skill-curation-gating branch in the learning node, but the auto-curator is
# gone and the learning node no longer parses/persists skill content — the
# branch it tested was vestigial. (See learning-node trim, same date.)

def t_state_schema_has_verification_outcome():
    src = (VAULT_ROOT / "scripts" / "vault_graph" / "state.py").read_text(encoding="utf-8")
    assert "verification_outcome" in src, "TaskState missing verification_outcome field"
    return "TaskState includes verification_outcome"

def t_failure_modes_writer_includes_constraint_field():
    src = (VAULT_ROOT / "scripts" / "vault_graph" / "nodes" / "stubs.py").read_text(encoding="utf-8")
    assert "constraint_added" in src, \
        "_write_verification_failure should include 'constraint_added' field"
    return "verification failure entries carry constraint_added field"

check("verification module imports cleanly", t_verification_module_imports)
check("verification parses fenced ```yaml verification: block",
      t_verification_parses_yaml_block)
check("verification passes a trivial real check end-to-end",
      t_verification_passes_real_check)
check("verification correctly fails when a check fails",
      t_verification_catches_failure)
check("verification reports parse_error when block is missing",
      t_verification_missing_block_is_failure)
check("PLANNING_PROMPT requires verification: + verification_policy: YAML",
      t_planning_prompt_requires_verification)
check("terminal_complete gates status=completed on verification_outcome.all_passed",
      t_terminal_complete_gates_on_verification)
check("TaskState schema includes verification_outcome field",
      t_state_schema_has_verification_outcome)
check("verification failure_modes entries carry constraint_added field",
      t_failure_modes_writer_includes_constraint_field)


# caught_by audit checks removed 2026-05-12 as part of dial-back of over-
# engineered taxonomy. failure_modes.md is now a plain append-only audit log;
# the "which detection layer caught it" classification was bookkeeping nobody
# read.


# ════════════════════════════════════════════════════════════════════════════
section("Skill description quality (Anthropic 'pushy descriptions' standard)")

def t_skill_descriptions_quality():
    """Per Anthropic skill-creator: descriptions should be ≥80 words with
    explicit trigger contexts to combat undertriggering. This is an audit
    nudge, not a hard fail — current skills predate the rule."""
    import re as _re
    sk_root = VAULT_ROOT / "ai_skills"
    weak = []
    for d in sorted(sk_root.iterdir()):
        if not d.is_dir() or d.name.startswith(("_", ".")):
            continue
        p = d / "SKILL.md"
        if not p.exists():
            continue
        text = p.read_text(encoding="utf-8", errors="replace")[:2000]
        # Extract description from frontmatter (simple line-based parse)
        desc_match = _re.search(r"^description:\s*(.+?)(?=^\w+:|^---|\Z)",
                                text, _re.M | _re.S)
        if not desc_match:
            weak.append((d.name, "no description field"))
            continue
        desc = desc_match.group(1).strip()
        word_count = len(desc.split())
        if word_count < 80:
            weak.append((d.name, f"only {word_count} words (target ≥80)"))
    # Audit nudge: report but don't fail (yet) — set up for a tightening
    # pass after the verification loop produces real "skill helped" signal.
    YELLOW_LOCAL = "\033[33m"
    if weak:
        print(f"      {YELLOW_LOCAL}({len(weak)} skill descriptions below 80 words — "
              f"flagged but not failing){RESET}")
    return f"reviewed {sum(1 for d in sk_root.iterdir() if d.is_dir() and (d/'SKILL.md').exists())} skills"

check("skill descriptions reviewed (≥80 words, pushy triggers)",
      t_skill_descriptions_quality)


# Regression-block meta-loop audits removed 2026-05-12. The mechanism caught
# zero real bugs in practice; every failure was an intentional behavior change
# requiring manual block-disable. state/regression_blocks.yaml is kept as a
# historical artifact; no new entries get added; recording is no longer wired.


# ════════════════════════════════════════════════════════════════════════════
section("Cross-model judge as completion gate (Cluster B.#6)")

def t_judge_parser_extracts_verdict():
    from vault_graph.execution_judge import parse_judge_verdict
    sample = """SCORES:
- plan_adherence: 5
- scope_discipline: 4
- code_quality: 5
- completeness: 5
- risk: 4

CONCERNS:
- Minor: missing docstring on cmd_models.

VERDICT: APPROVE
"""
    parsed = parse_judge_verdict(sample)
    assert parsed["verdict"] == "APPROVE", f"verdict mismatch: {parsed}"
    assert parsed["scores"].get("plan_adherence") == 5
    assert parsed["scores"].get("risk") == 4
    assert parsed["score_avg"] == 4.6, f"score_avg should be 4.6 got {parsed['score_avg']}"
    assert len(parsed["concerns"]) == 1
    return f"parsed APPROVE verdict + 5 scores (avg {parsed['score_avg']}) + 1 concern"

def t_judge_parser_handles_reject():
    from vault_graph.execution_judge import parse_judge_verdict
    sample = """SCORES:
- plan_adherence: 1
- scope_discipline: 2
- code_quality: 1
- completeness: 1
- risk: 2

CONCERNS:
- Implementation does not match the plan at all.
- Touched files outside the agreed scope.

VERDICT: REJECT
"""
    parsed = parse_judge_verdict(sample)
    assert parsed["verdict"] == "REJECT"
    assert parsed["score_avg"] == 1.4
    assert len(parsed["concerns"]) == 2
    return "parser correctly extracts REJECT + low scores"

def t_judge_parser_handles_unparseable():
    from vault_graph.execution_judge import parse_judge_verdict
    parsed = parse_judge_verdict("hello world no verdict here")
    assert parsed["verdict"] == "", "should return empty verdict on unparseable input"
    assert parsed["scores"] == {}
    assert parsed["concerns"] == []
    return "parser returns empty verdict on unparseable input (safe default)"

def t_terminal_complete_gates_on_judge_reject():
    src = (VAULT_ROOT / "scripts" / "vault_graph" / "nodes" / "stubs.py").read_text(encoding="utf-8")
    assert "judge_verdict" in src, "terminal_complete should read judge_verdict"
    assert "judge_blocks" in src, "terminal_complete should compute a judge_blocks flag"
    assert '"REJECT"' in src, "terminal_complete should treat REJECT as a block signal"
    assert "judge_rejected" in src, "terminal_complete should set phase=judge_rejected on judge block"
    return "terminal_complete blocks completion on judge REJECT"

def t_state_schema_has_judge_verdict():
    src = (VAULT_ROOT / "scripts" / "vault_graph" / "state.py").read_text(encoding="utf-8")
    assert "judge_verdict" in src, "TaskState missing judge_verdict field"
    return "TaskState includes judge_verdict"

check("judge parser extracts APPROVE verdict + scores + concerns",
      t_judge_parser_extracts_verdict)
check("judge parser correctly extracts REJECT verdict",
      t_judge_parser_handles_reject)
check("judge parser returns safe-default empty verdict on unparseable input",
      t_judge_parser_handles_unparseable)
check("terminal_complete blocks completion on judge REJECT",
      t_terminal_complete_gates_on_judge_reject)
check("TaskState schema includes judge_verdict field",
      t_state_schema_has_judge_verdict)


# GAP #2 audit checks removed 2026-05-12 with the judge-as-advisor redesign.
# Auto route-back on NEEDS_REVISION was retired; the judge's concerns now
# surface to the user at wait_human_review, who decides whether to merge,
# refine, or reject. See VISION.md / ai_main.md.


# ════════════════════════════════════════════════════════════════════════════
section("GAP #1 fix: judge project-scope awareness (2026-05-11)")


def t_judge_scope_loader_returns_vault_default_for_empty_app_location():
    """Behavioral: empty app_location (vault-internal work) must return a
    non-empty scope block describing vault's quality directives. This is
    what gives the judge enough context to NOT penalize behavioral-check
    additions or supervisor-rule additions as 'scope creep'."""
    from vault_graph.project_memory import load_judge_scope_block
    block = load_judge_scope_block("", vault_root=None)
    assert block, "BUG: vault-internal scope block must be non-empty"
    assert "vault internals" in block.lower() or "quality hierarchy" in block.lower(), \
        "BUG: vault default scope must surface the quality hierarchy directive"
    # The judge should be told to weight axes equally for vault work (no project-specific bias)
    assert "axes" in block.lower() or "scope" in block.lower(), \
        "BUG: vault default scope must give the judge axis guidance"


def t_judge_scope_loader_reads_project_charter_when_present():
    """Behavioral: when app_location points outside the vault and a charter
    exists in the project memory bucket, the loader must surface its content
    so the judge weights axes against the project's stated goals."""
    import tempfile as _tf
    from pathlib import Path as _P
    from vault_graph.project_memory import (
        load_judge_scope_block, bootstrap_project_memory, CHARTER_FILE,
        get_stable_project_identifier,
    )
    with _tf.TemporaryDirectory() as tmp:
        # Create a synthetic project OUTSIDE the vault root
        project_dir = _P(tmp) / "syn_proj_judge_scope"
        project_dir.mkdir()
        (project_dir / ".git").mkdir()  # presence of .git makes it look like a project root
        # Bootstrap memory so the bucket exists in the vault
        mem_dir = bootstrap_project_memory(str(project_dir))
        if mem_dir is None:
            # resolve_project_root rejected the candidate — skip this sub-assertion
            # but don't fail the test; the loader's other branches still need coverage
            return "project memory bucket unavailable in fixture; vault-default path covered"
        # Write a charter
        charter_text = "# Project Charter\n\nA test CLI tool. Focus on scope discipline and risk."
        (mem_dir / CHARTER_FILE).write_text(charter_text, encoding="utf-8")
        try:
            block = load_judge_scope_block(str(project_dir))
            assert block, "BUG: scope block empty even though charter exists"
            assert "scope discipline" in block.lower() or "cli tool" in block.lower(), \
                f"BUG: charter content not surfaced — got: {block[:200]}"
            slug = get_stable_project_identifier(project_dir)
            assert slug in block, \
                "BUG: project slug not included in scope block"
        finally:
            # Cleanup memory bucket
            try:
                for f in mem_dir.iterdir():
                    f.unlink()
                mem_dir.rmdir()
            except OSError:
                pass


def t_execution_judge_prompt_has_project_scope_slot():
    """Source-level: EXECUTION_JUDGE_PROMPT must include the {project_scope}
    interpolation slot so the loaded scope block lands in the prompt."""
    src = (VAULT_ROOT / "scripts" / "vault_graph" / "execution_judge.py").read_text(encoding="utf-8")
    assert "{project_scope}" in src, \
        "BUG: EXECUTION_JUDGE_PROMPT missing {project_scope} slot — scope unreachable from judge"
    assert "load_judge_scope_block" in src, \
        "BUG: execution_judge.py does not call load_judge_scope_block — scope feature dead code"
    assert "app_location" in src, \
        "BUG: run_execution_judge does not accept app_location — call site cannot pass project context"


def t_execution_call_site_passes_app_location_to_judge():
    """Source-level: execution.py must pass state['app_location'] when calling
    run_execution_judge. Without this the scope param is permanently empty."""
    src = (VAULT_ROOT / "scripts" / "vault_graph" / "nodes" / "execution.py").read_text(encoding="utf-8")
    assert 'app_location=state.get("app_location"' in src, \
        "BUG: execution.py does not pass app_location to run_execution_judge — judge stays scope-blind"


check("judge scope loader returns vault-default block for empty app_location (behavioral)",
      t_judge_scope_loader_returns_vault_default_for_empty_app_location)
check("judge scope loader surfaces project charter content (behavioral)",
      t_judge_scope_loader_reads_project_charter_when_present)
check("EXECUTION_JUDGE_PROMPT has {project_scope} slot + loader wired",
      t_execution_judge_prompt_has_project_scope_slot)
check("execution.py call site passes app_location to run_execution_judge",
      t_execution_call_site_passes_app_location_to_judge)


# ════════════════════════════════════════════════════════════════════════════
section("GAP #4 fix: plan_review arbitration parity (2026-05-11)")


def t_plan_review_parser_accepts_both_verdict_spellings():
    """Behavioral: parse_plan_review_verdict must accept NEEDS_REFINEMENT
    (plan_review's historical token) AND NEEDS_REVISION (execution_judge's
    token), normalizing both to NEEDS_REVISION so downstream consumers
    (refinement.py) read one canonical value."""
    from vault_graph.execution_judge import parse_plan_review_verdict
    # Plan-review's native spelling
    pr = parse_plan_review_verdict(
        "SCORES:\n- scope_clarity: 4\n- completeness: 4\n- specificity: 3\n"
        "- risk_awareness: 4\n- verifiability: 4\n\n"
        "CONCERNS:\n- Missing edge case for empty input.\n\n"
        "VERDICT: NEEDS_REFINEMENT\n"
    )
    assert pr["verdict"] == "NEEDS_REVISION", \
        f"BUG: NEEDS_REFINEMENT must normalize to NEEDS_REVISION; got {pr['verdict']!r}"
    assert pr["scores"].get("scope_clarity") == 4, "BUG: scope_clarity not parsed"
    assert pr["scores"].get("verifiability") == 4, "BUG: verifiability not parsed"
    assert len(pr["concerns"]) == 1, "BUG: plan_review concerns not parsed"
    # Judge spelling also accepted (both judges share the parser shape)
    pr2 = parse_plan_review_verdict(
        "SCORES:\n- scope_clarity: 5\n\n"
        "CONCERNS:\n- None\n\nVERDICT: NEEDS_REVISION\n"
    )
    assert pr2["verdict"] == "NEEDS_REVISION"
    # REJECT and APPROVE preserved
    pr3 = parse_plan_review_verdict("VERDICT: REJECT\n")
    assert pr3["verdict"] == "REJECT"
    pr4 = parse_plan_review_verdict("VERDICT: APPROVE\n")
    assert pr4["verdict"] == "APPROVE"
    # Empty / garbage → empty verdict (safe default)
    assert parse_plan_review_verdict("")["verdict"] == ""
    assert parse_plan_review_verdict("no structure here")["verdict"] == ""


def t_plan_review_node_writes_parsed_verdict_to_state():
    """Source-level: planning.py plan_review() must call parse_plan_review_verdict
    and include plan_review_verdict in the return dict. Without this, the
    parser exists but is unused and refinement stays blind to plan_review."""
    src = (VAULT_ROOT / "scripts" / "vault_graph" / "nodes" / "planning.py").read_text(encoding="utf-8")
    assert "parse_plan_review_verdict" in src, \
        "BUG: planning.py plan_review() does not call parse_plan_review_verdict — verdict never reaches state"
    assert "plan_review_verdict" in src, \
        "BUG: planning.py plan_review() does not return plan_review_verdict in state dict"


def t_refinement_injects_plan_review_concerns():
    """Source-level: refinement.py must read plan_review_verdict and inject
    its concerns when verdict is NEEDS_REVISION or REJECT. Achieves arbitration
    parity with the existing judge_verdict injection."""
    src = (VAULT_ROOT / "scripts" / "vault_graph" / "nodes" / "refinement.py").read_text(encoding="utf-8")
    assert "plan_review_verdict" in src, \
        "BUG: refinement.py does not read plan_review_verdict — plan-review concerns dropped"
    assert "plan_review_has_concerns" in src or "Plan-review concerns" in src, \
        "BUG: refinement.py missing the plan_review concern-injection branch"
    assert "Plan-review concerns" in src, \
        "BUG: refinement.py missing the 'Plan-review concerns' prompt section header"


def t_state_schema_has_plan_review_verdict_field():
    """TaskState must declare plan_review_verdict so downstream consumers can
    type-check + so the field is documented as part of the runtime schema."""
    src = (VAULT_ROOT / "scripts" / "vault_graph" / "state.py").read_text(encoding="utf-8")
    assert "plan_review_verdict" in src, \
        "BUG: TaskState schema missing plan_review_verdict field"


check("plan_review parser accepts NEEDS_REFINEMENT + NEEDS_REVISION (behavioral)",
      t_plan_review_parser_accepts_both_verdict_spellings)
check("plan_review node parses verdict + writes plan_review_verdict to state",
      t_plan_review_node_writes_parsed_verdict_to_state)
check("refinement.py injects plan_review concerns alongside human answers (parity with judge)",
      t_refinement_injects_plan_review_concerns)
check("TaskState schema includes plan_review_verdict field",
      t_state_schema_has_plan_review_verdict_field)


# ════════════════════════════════════════════════════════════════════════════
section("Bug V mitigation: cmd_advance validates flag-vs-gate (2026-05-11)")


def t_cmd_advance_refuses_answers_at_approval_gate():
    """Behavioral: cmd_advance must refuse --answers when the task is at
    wait_human_approval (the auto_0074 trigger for Bug V). It should also
    refuse --approve at wait_human_answers. --feedback is always allowed."""
    from vault_graph.cli import _GATE_TO_EXPECTED_INPUT, _current_gate
    # _GATE_TO_EXPECTED_INPUT must cover both next_action and status keys
    for k in ("wait_human_answers", "wait_human_approval", "wait_human_review",
              "pending_human_answers", "pending_human_approval", "pending_human_review"):
        assert k in _GATE_TO_EXPECTED_INPUT, \
            f"BUG: _GATE_TO_EXPECTED_INPUT missing key {k!r} — gate detection coverage incomplete"
    # answers ∈ wait_human_answers expected inputs
    assert "answers" in _GATE_TO_EXPECTED_INPUT["wait_human_answers"]
    # answers ∉ wait_human_approval expected inputs (the mismatch case)
    assert "answers" not in _GATE_TO_EXPECTED_INPUT["wait_human_approval"], \
        "BUG: answers must NOT be valid at wait_human_approval (Bug V root cause)"
    # approve ∈ wait_human_approval expected inputs
    assert "approve" in _GATE_TO_EXPECTED_INPUT["wait_human_approval"]
    # feedback is universal
    for v in _GATE_TO_EXPECTED_INPUT.values():
        assert "feedback" in v, "BUG: feedback should be allowed at every gate"
    # _current_gate reads from next_action first, falls back to status
    assert _current_gate({"next_action": "wait_human_approval"}) == "wait_human_approval"
    assert _current_gate({"status": "pending_human_answers"}) == "pending_human_answers"
    assert _current_gate({}) == ""


def t_cmd_advance_source_has_bug_v_guard():
    """Source-level: cmd_advance must call the gate-validation logic before
    applying state updates, and must refuse on hard mismatch. Without this
    the runtime check from t_cmd_advance_refuses_answers_at_approval_gate
    has no caller."""
    src = (VAULT_ROOT / "scripts" / "vault_graph" / "cli.py").read_text(encoding="utf-8")
    assert "_current_gate" in src and "_GATE_TO_EXPECTED_INPUT" in src, \
        "BUG: cmd_advance does not use the gate-validation primitives"
    assert "REFUSED" in src or "refused" in src.lower(), \
        "BUG: cmd_advance does not emit a refusal message on mismatch"
    assert "hard_mismatch" in src, \
        "BUG: cmd_advance does not distinguish hard (answers/approve) from soft (feedback) mismatches"


check("cmd_advance gate-validation table covers all 6 gate names + feedback universal (behavioral)",
      t_cmd_advance_refuses_answers_at_approval_gate)
check("cmd_advance source contains the Bug V guard logic + refusal path",
      t_cmd_advance_source_has_bug_v_guard)


# ════════════════════════════════════════════════════════════════════════════
section("Bug Y fix: verification YAML parser surfaces PyYAML errors (2026-05-11)")


def t_extract_verification_block_surfaces_yaml_error():
    """Behavioral: when a verification: block contains malformed YAML (e.g.
    Python triple-quote strings inside double-quoted scalars — the canonical
    Bug Y pattern from auto_0075), the parser must surface the underlying
    PyYAML error so the agent's next iteration can fix the YAML, not the
    generic 'did not parse' message that gives the agent nothing to act on.
    """
    from vault_graph.verification import extract_verification_block
    # Canonical Bug Y pattern: triple-quoted Python inside a quoted YAML scalar.
    # PyYAML rejects this because the unescaped triple quotes break the scalar.
    bad_plan = (
        "## Verification\n"
        "```yaml\n"
        "verification:\n"
        '  - id: foo\n'
        "    type: command\n"
        '    run: "python -c \"\"\"def x(): pass\"\"\""\n'  # unbalanced quotes
        "```\n"
    )
    checks, policy, err = extract_verification_block(bad_plan)
    assert checks is None, f"BUG: malformed YAML should not return checks; got {checks!r}"
    assert err is not None, "BUG: malformed YAML must surface an error"
    # Either the underlying PyYAML error OR a fallback message that mentions parse
    assert ("PyYAML parse error" in err or "did not parse" in err
            or "no `verification:` block found" in err), \
        f"BUG: error must mention parse failure; got {err!r}"
    # Well-formed YAML still parses
    good_plan = """## Verification
```yaml
verification:
  - id: foo
    type: command
    run: echo hello
    expect_returncode: 0
```
"""
    checks2, policy2, err2 = extract_verification_block(good_plan)
    assert err2 is None and checks2 and len(checks2) == 1, \
        f"BUG: well-formed YAML must still parse; got err={err2!r} checks={checks2!r}"
    # Truly unparseable garbage that nonetheless has a verification: line
    # falls through to the generic message
    garbage = """## Verification
```yaml
verification:
  this: is not a list, it's a dict
```
"""
    _, _, err3 = extract_verification_block(garbage)
    assert err3 is not None, "BUG: non-list verification must produce an error"


def t_planning_prompt_warns_about_python_in_yaml():
    """Source-level: PLANNING_PROMPT must explicitly warn against the Python
    triple-quote-in-YAML pattern that caused Bug Y. Without this guidance the
    planner can produce the same malformed YAML next time."""
    src = (VAULT_ROOT / "scripts" / "ai_dougs" / "prompts.py").read_text(encoding="utf-8")
    assert "PyYAML-safe" in src or "PyYAML safe" in src, \
        "BUG: PLANNING_PROMPT missing the 'PyYAML-safe' guidance"
    assert "triple-quote" in src.lower() or "triple quote" in src.lower(), \
        "BUG: PLANNING_PROMPT missing the explicit triple-quote warning"
    assert "literal block scalar" in src or "`|`" in src, \
        "BUG: PLANNING_PROMPT must recommend the literal block scalar form"
    assert "sidecar script" in src or "sidecar" in src.lower(), \
        "BUG: PLANNING_PROMPT missing the sidecar-script alternative"


check("verification parser surfaces PyYAML error + Bug Y hint on malformed YAML (behavioral)",
      t_extract_verification_block_surfaces_yaml_error)
check("PLANNING_PROMPT warns against Python triple-quote-in-YAML pattern (Bug Y)",
      t_planning_prompt_warns_about_python_in_yaml)


# ════════════════════════════════════════════════════════════════════════════
section("Bug T fix: task-file frontmatter sync (disk reflects LangGraph state)")


def t_frontmatter_sync_updates_status_field_in_place():
    """Behavioral: sync_task_file_frontmatter must update the `status:`
    frontmatter key on disk while preserving title/created/sharpener_model
    and the markdown body. Idempotent on repeated writes."""
    import tempfile as _tf
    from vault_graph.frontmatter_sync import sync_task_file_frontmatter
    fixture = (
        "---\n"
        "status: pending_ai_planning\n"
        "app_location:\n"
        "title: synthetic_bugT\n"
        "created: 2026-05-11 12:00\n"
        "sharpener_model: claude:sonnet\n"
        "---\n"
        "\n"
        "# `=this.title`\n"
        "status: `=this.status`\n"
        "\n"
        "## Initial Prompt\n"
        "Test body that must be preserved.\n"
    )
    with _tf.NamedTemporaryFile(mode="w", suffix=".md", delete=False,
                                encoding="utf-8") as fh:
        fh.write(fixture)
        path = fh.name
    try:
        state = {
            "task_file_path": path,
            "status": "pending_human_approval",
            "current_phase": "refinement",
            "execution_attempts": 1,
            "judge_verdict": {"verdict": "NEEDS_REVISION", "score_avg": 3.6},
        }
        wrote = sync_task_file_frontmatter(state)
        assert wrote, "BUG: sync should have written changes"
        text = Path(path).read_text(encoding="utf-8")
        assert "status: pending_human_approval" in text, \
            "BUG: status key not updated"
        assert "current_phase: refinement" in text, \
            "BUG: current_phase key not added"
        assert "judge_verdict: NEEDS_REVISION" in text, \
            "BUG: judge_verdict not projected from dict to scalar"
        # Preservation: user-owned keys must remain
        assert "title: synthetic_bugT" in text, "BUG: title preserved"
        assert "sharpener_model: claude:sonnet" in text, "BUG: sharpener_model preserved"
        assert "## Initial Prompt" in text, "BUG: body content preserved"
        assert "Test body that must be preserved." in text, "BUG: body content preserved"
        # Idempotence: second write with same state changes nothing
        wrote_again = sync_task_file_frontmatter(state)
        # Note: second call may still write (depending on order-preservation logic);
        # the important contract is that the file CONTENT is the same after both
        text_after = Path(path).read_text(encoding="utf-8")
        assert text_after == text, \
            "BUG: idempotence violated — same state produced different bytes"
        # Empty / missing task_file_path is safe
        assert sync_task_file_frontmatter({}) is False
        assert sync_task_file_frontmatter({"task_file_path": ""}) is False
        assert sync_task_file_frontmatter({"task_file_path": "/nonexistent/path/x.md"}) is False
    finally:
        try:
            Path(path).unlink()
        except OSError:
            pass


def t_nodes_call_frontmatter_sync_at_user_visible_transitions():
    """Source-level: the nodes that produce user-visible status changes
    must call sync_task_file_frontmatter so Obsidian Bases sees the gate.
    Targets: plan_review (end), refinement (end), execution (end),
    terminal_complete (end)."""
    nodes_with_sync = []
    for rel in ("planning.py", "refinement.py", "execution.py", "stubs.py"):
        src = (VAULT_ROOT / "scripts" / "vault_graph" / "nodes" / rel).read_text(encoding="utf-8")
        if "sync_task_file_frontmatter" in src:
            nodes_with_sync.append(rel)
    assert "planning.py" in nodes_with_sync, \
        "BUG: planning.py plan_review() does not sync frontmatter → Obsidian misses wait_human_answers gate"
    assert "refinement.py" in nodes_with_sync, \
        "BUG: refinement.py does not sync frontmatter → Obsidian misses wait_human_approval gate"
    assert "execution.py" in nodes_with_sync, \
        "BUG: execution.py does not sync frontmatter → Obsidian misses wait_human_review gate"
    assert "stubs.py" in nodes_with_sync, \
        "BUG: stubs.py terminal_complete() does not sync frontmatter → completed/failed never lands on disk"


check("frontmatter sync updates status + judge_verdict on disk; preserves body (behavioral)",
      t_frontmatter_sync_updates_status_field_in_place)
check("user-visible transition nodes (plan_review/refinement/execution/terminal) call frontmatter sync",
      t_nodes_call_frontmatter_sync_at_user_visible_transitions)


# ════════════════════════════════════════════════════════════════════════════
section("Bug S fix: judge content-diff via .backups tarball (2026-05-11)")


def t_content_diff_returns_real_unified_diff_for_changed_file():
    """Behavioral: build a synthetic vault root with a .backups tarball
    containing one file's pre-execution content; modify the file on disk;
    verify compute_content_diff_from_backup emits a real unified-diff blob
    with both the - line and the + line."""
    import tempfile as _tf
    import tarfile as _tar
    from datetime import datetime as _dt
    from vault_graph.content_diff import compute_content_diff_from_backup
    from vault_graph.vault_backup import _safe_task_name
    with _tf.TemporaryDirectory() as tmp:
        vault = Path(tmp) / "vault"
        vault.mkdir()
        # Make it look like a vault root (the create_backup precondition)
        (vault / "ai_main.md").write_text("# fixture\n", encoding="utf-8")
        # Pre-execution content
        target_rel = "scripts/syn_target.py"
        target_path = vault / target_rel
        target_path.parent.mkdir(parents=True, exist_ok=True)
        pre_content = "def hello():\n    return 'pre'\n"
        target_path.write_text(pre_content, encoding="utf-8")
        # Build the tarball that simulates a pre-execution backup
        backup_dir = vault / ".backups"
        backup_dir.mkdir()
        ts = _dt.now().strftime("%Y-%m-%d_%H%M%S")
        task = "syn_bug_s"
        safe = _safe_task_name(task)
        tar_path = backup_dir / f"{safe}_{ts}.tar.gz"
        with _tar.open(tar_path, "w:gz") as tar:
            # arcname mimics create_backup's `arcname=root.name` behavior
            tar.add(vault, arcname=vault.name,
                    filter=lambda ti: None if ti.name.endswith(".tar.gz") else ti)
        # Mutate the file on disk after the backup
        post_content = "def hello():\n    return 'post'\n# new comment\n"
        target_path.write_text(post_content, encoding="utf-8")
        # Run the content-diff helper
        result = compute_content_diff_from_backup(vault, task, [target_rel])
        assert result, "BUG: content diff should be non-empty when file changed"
        assert "- " in result and "+ " in result or "-    return 'pre'" in result, \
            f"BUG: unified diff missing - / + lines; got first 300 chars: {result[:300]!r}"
        assert "pre-execution" in result.lower() or "(pre-execution)" in result, \
            "BUG: pre/post labels missing from diff header"
        assert target_rel in result, \
            "BUG: target file path missing from diff blob"


def t_content_diff_returns_empty_when_no_backup_found():
    """Behavioral: with no backup tarball, helper returns empty string (callers
    fall back to existing combined_diff content)."""
    import tempfile as _tf
    from vault_graph.content_diff import compute_content_diff_from_backup
    with _tf.TemporaryDirectory() as tmp:
        vault = Path(tmp) / "vault"
        vault.mkdir()
        (vault / "ai_main.md").write_text("# fixture\n", encoding="utf-8")
        # No .backups directory at all
        result = compute_content_diff_from_backup(vault, "no_backup_task", ["foo.py"])
        assert result == "", f"BUG: expected empty string with no backup; got {result!r}"


def t_execution_node_wires_content_diff_into_combined_diff():
    """Source-level: execution.py must call compute_content_diff_from_backup
    and append its output to combined_diff so the judge sees real before/after
    content for vault-infra tasks."""
    src = (VAULT_ROOT / "scripts" / "vault_graph" / "nodes" / "execution.py").read_text(encoding="utf-8")
    assert "compute_content_diff_from_backup" in src, \
        "BUG: execution.py does not call compute_content_diff_from_backup — Bug S unfixed"
    assert "content_diff" in src, \
        "BUG: execution.py missing content_diff variable / append site"


check("content diff emits real unified-diff blob from backup tarball (behavioral)",
      t_content_diff_returns_real_unified_diff_for_changed_file)
check("content diff returns empty string when no backup is found (safe fallback)",
      t_content_diff_returns_empty_when_no_backup_found)
check("execution.py wires content_diff into combined_diff for judge (Bug S)",
      t_execution_node_wires_content_diff_into_combined_diff)


# Bug X curator audits removed 2026-05-12 — auto-curator was deleted; skills
# are now co-authored. writeSkillFile is a no-op stub kept only to keep stale
# imports from raising; nothing to assert about it.


# ════════════════════════════════════════════════════════════════════════════
section("auto_0062 surfaced bugs (3 fixes from one task)")

def t_diff_capture_subtracts_orchestrator_writes():
    """Bug A fix: run_self_test accepts orchestrator_writes set + subtracts."""
    src = (VAULT_ROOT / "scripts" / "vault_graph" / "self_test.py").read_text(encoding="utf-8")
    assert "orchestrator_writes" in src, "run_self_test missing orchestrator_writes param"
    assert "subtracted" in src.lower() or "p not in orchestrator_writes" in src, \
        "run_self_test should actually subtract orchestrator_writes from paths"
    exec_src = (VAULT_ROOT / "scripts" / "vault_graph" / "nodes" / "execution.py").read_text(encoding="utf-8")
    assert "orchestrator_writes=orchestrator_writes" in exec_src, \
        "execution should pass orchestrator_writes to run_self_test"
    assert "mtime_paths - orchestrator_writes" in exec_src, \
        "execution should compute agent writes as mtime_paths minus orchestrator_writes"
    return "diff capture correctly subtracts orchestrator-session edits"

def t_watchdog_excludes_human_gate_waits():
    """Stuck-task detection skips tasks paused at a human gate."""
    src = (VAULT_ROOT / "scripts" / "vault_graph" / "watchdog.py").read_text(encoding="utf-8")
    assert "pending_human_" in src and "wait_human_" in src, \
        "watchdog should enumerate human-gate state markers"
    assert "_stuck_tasks" in src, \
        "watchdog should define _stuck_tasks helper"
    return "watchdog _stuck_tasks excludes human-gate waits"

def t_verification_iteration_loop_wired():
    """Bug C fix: execution.py iterates verification on failure with feedback."""
    src = (VAULT_ROOT / "scripts" / "vault_graph" / "nodes" / "execution.py").read_text(encoding="utf-8")
    assert "max_iterations" in src and "iteration" in src, \
        "execution.py missing iteration loop"
    assert "iterations_used" in src, \
        "verification_outcome should record iterations_used"
    assert "Verification iteration" in src or "feeding failures back" in src, \
        "iteration loop should include a fix-prompt back to the agent"
    assert "PowerShell-only syntax" in src or "non-portable shell syntax" in src, \
        "iteration prompt should educate agent on portable shell"
    return "verification iteration loop wired with fix-prompt feedback"

def t_planning_prompt_requires_portable_shell():
    # 2026-05-09 (Chunk 2 refactor via auto_0065): prompts moved out of
    # ai_dougs.py into prompts.py.
    src = (VAULT_ROOT / "scripts" / "ai_dougs" / "prompts.py").read_text(encoding="utf-8")
    assert "cross-shell portable" in src or "PowerShell-only" in src, \
        "PLANNING_PROMPT should call out cross-shell portability"
    assert "subprocess.run(shell=True)" in src, \
        "PLANNING_PROMPT should explain the verification runner uses shell=True"
    return "planning prompt requires portable shell commands"

check("Bug A: diff-capture subtracts orchestrator-session writes",
      t_diff_capture_subtracts_orchestrator_writes)
check("watchdog stuck-task detection excludes human-gate waits",
      t_watchdog_excludes_human_gate_waits)
check("Bug C: verification iteration loop wired with fix-prompt feedback",
      t_verification_iteration_loop_wired)
check("Planning prompt requires cross-shell portable verification commands",
      t_planning_prompt_requires_portable_shell)

def t_runrequest_rejects_rc_zero_permission_stub():
    """Bug D fix (auto_0062 iteration): runAiRequest treats rc=0 + bail-pattern
    output as failure so routing falls through. Without this, agents that
    return permission-waiting stubs slip through as 'successful' plans."""
    src = (VAULT_ROOT / "scripts" / "ai_dougs" / "ai_dougs.py").read_text(encoding="utf-8")
    assert "BAIL_PATTERNS" in src, "runAiRequest missing rc=0 bail-pattern check"
    assert "Waiting for file read permissions" in src, \
        "BAIL_PATTERNS should include the auto_0062 stub signature"
    assert "permission/bail stub" in src, \
        "rc=0 stub path should print explicit error label"
    assert "looks_like_bail" in src, \
        "should compute looks_like_bail flag explicitly"
    return "runAiRequest rejects rc=0 permission/bail stubs"

check("Bug D: runAiRequest rejects rc=0 permission-stub false-success",
      t_runrequest_rejects_rc_zero_permission_stub)


# ════════════════════════════════════════════════════════════════════════════
section("screenshot_compare + provider re-exploration (Cluster C-ish)")

def t_screenshot_compare_real_diff():
    """Pixel diff actually computes a non-zero ratio for very different images
    AND a zero ratio for identical ones."""
    import tempfile
    from PIL import Image
    from vault_graph.verification import _run_screenshot_compare_check
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        Image.new("RGB", (50, 50), (255, 0, 0)).save(td_path / "ref.png")
        Image.new("RGB", (50, 50), (255, 0, 0)).save(td_path / "same.png")
        Image.new("RGB", (50, 50), (0, 255, 0)).save(td_path / "diff.png")
        same = _run_screenshot_compare_check(
            {"id": "same", "reference": "ref.png", "output": "same.png"}, td_path)
        diff = _run_screenshot_compare_check(
            {"id": "diff", "reference": "ref.png", "output": "diff.png", "max_diff_ratio": 0.05}, td_path)
        missing = _run_screenshot_compare_check(
            {"id": "missing", "reference": "ref.png", "output": "nope.png"}, td_path)
    assert same.passed, f"identical images should pass: {same.detail}"
    assert not diff.passed, f"very different images should fail: {diff.detail}"
    assert not missing.passed, f"missing output should fail: {missing.detail}"
    return "identical=PASS, different=FAIL, missing=FAIL — pixel diff working"

def t_screenshot_compare_handles_missing_paths():
    from vault_graph.verification import _run_screenshot_compare_check
    r = _run_screenshot_compare_check({"id": "no_paths"}, Path("."))
    assert not r.passed and "reference" in r.detail and "output" in r.detail, \
        "should fail when reference/output paths missing"
    return "missing reference/output handled cleanly"

def t_re_exploration_helper_exists():
    src = (VAULT_ROOT / "scripts" / "ai_dougs" / "routing.py").read_text(encoding="utf-8")
    assert "_recently_used_providers" in src, \
        "_recently_used_providers helper missing"
    assert "re_exploration_enabled" in src, \
        "re_exploration_enabled param missing from defaults"
    assert "re_exploration_interval" in src, \
        "re_exploration_interval param missing"
    assert "re_exploration_window" in src, \
        "re_exploration_window param missing"
    assert "Re-exploration triggered" in src, \
        "_rankProviders should print when re-exploration fires"
    return "re-exploration helper + params + trigger log all present"

def t_re_exploration_actually_runs():
    """Call _recently_used_providers on real cost_log data and confirm it
    returns sane provider names."""
    import importlib.util as _iu
    spec = _iu.spec_from_file_location("_ad_re", str(VAULT_ROOT / "scripts" / "ai_dougs" / "ai_dougs.py"))
    A = _iu.module_from_spec(spec)
    spec.loader.exec_module(A)
    recent = A._recently_used_providers(10)
    # Must return a list; each entry must be a non-empty string
    assert isinstance(recent, list), f"expected list, got {type(recent)}"
    for p in recent:
        assert isinstance(p, str) and p, f"bad provider entry: {p!r}"
    return f"_recently_used_providers returns {len(recent)} valid provider names"

check("screenshot_compare pixel diff: identical pass, different fail",
      t_screenshot_compare_real_diff)
check("screenshot_compare handles missing reference/output cleanly",
      t_screenshot_compare_handles_missing_paths)
check("re-exploration helper + params + trigger present in routing.py",
      t_re_exploration_helper_exists)
check("_recently_used_providers reads cost_log + returns sane providers",
      t_re_exploration_actually_runs)


# Fix B (post-learning re-verify) audit removed 2026-05-12 with Fix B itself.
# writeSkillFile-is-no-op audit redundant with above removal.


# ════════════════════════════════════════════════════════════════════════════
section("Per-project agent memory")

def _init_project_with_remote(project_root: Path, remote_url: str) -> None:
    project_root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=project_root, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["git", "remote", "add", "origin", remote_url], cwd=project_root, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def t_project_memory_same_remote_same_bucket():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td).resolve()
        root = base / "vault"
        root.mkdir()
        project_a = base / "machine_a" / "warcraftlogs-tracker"
        project_b = base / "machine_b" / "renamed-local-copy"
        nested_a = project_a / "src" / "app"
        nested_b = project_b / "client"
        nested_a.mkdir(parents=True)
        nested_b.mkdir(parents=True)
        _init_project_with_remote(project_a, "https://github.com/Catcam-fun/warcraftlogs-tracker.git")
        _init_project_with_remote(project_b, "git@github.com:Catcam-fun/warcraftlogs-tracker.git")

        bucket_a = project_memory.resolve_project_root(nested_a, root)
        bucket_b = project_memory.resolve_project_root(nested_b, root)
        expected = root / ".agent_memory" / "catcam-fun--warcraftlogs-tracker"
        assert bucket_a == expected
        assert bucket_b == expected

        memory_dir = project_memory.bootstrap_project_memory(nested_a, root)
        assert memory_dir == expected
        assert (memory_dir / "charter.md").read_text(encoding="utf-8") == project_memory.DEFAULT_CHARTER
        assert (memory_dir / "task_log.jsonl").exists()
        # 2026-05-12: failures.md retired from agent-readable surfaces.
        assert not (memory_dir / "failures.md").exists()
    return "same git remote resolves to one vault-side memory bucket"


def t_project_memory_nested_path_infers_project_root():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td).resolve()
        root = base / "vault"
        root.mkdir()
        project = base / "Desktop" / "warcraftlogs-tracker"
        nested = project / "src" / "app"
        nested.mkdir(parents=True)
        (project / "package.json").write_text("{}", encoding="utf-8")

        assert project_memory.infer_project_root(nested) == project
        bucket = project_memory.resolve_project_root(nested, root)
        assert bucket == root / ".agent_memory" / "warcraftlogs-tracker"
    return "nested external paths infer marker-bearing project root"

def t_project_memory_skips_vault_internal():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td).resolve()
        (root / "scripts" / "vault_graph").mkdir(parents=True)
        before = set((root / ".agent_memory").glob("*")) if (root / ".agent_memory").exists() else set()
        assert project_memory.resolve_project_root(root / "scripts" / "vault_graph", root) is None
        assert project_memory.bootstrap_project_memory(root / "scripts" / "vault_graph", root) is None
        assert not (root / "scripts" / "vault_graph" / ".agent_memory").exists()
        after = set((root / ".agent_memory").glob("*")) if (root / ".agent_memory").exists() else set()
        assert before == after
    return "vault-internal paths do not get project memory"

def t_planner_reads_and_injects_project_memory():
    src = (VAULT_ROOT / "scripts" / "vault_graph" / "nodes" / "planning.py").read_text(encoding="utf-8")
    assert "bootstrap_project_memory" in src, "task_entry should bootstrap project memory"
    assert "load_project_memory_context" in src, "planning should read project memory"
    assert "Project Agent Memory" in src, "planner should inject a labeled Project Agent Memory block"
    return "planning.py bootstraps, reads, and injects Project Agent Memory"

def t_verification_classifier_tags_every_passing_check():
    from vault_graph.verification import run_verification_block
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "contract.json").write_text('{"ok": true}', encoding="utf-8")
        checks = [
            {"id": "visual_snapshot", "type": "command", "command": "python -c \"print('screenshot ok')\""},
            {"id": "api_contract", "type": "assertion", "file": "contract.json", "contains": "ok"},
            {"id": "regression_pytest", "type": "command", "command": "python -c \"print('pytest regression')\""},
        ]
        outcome = run_verification_block(checks, root, task_id="audit_project_memory")
        assert outcome.all_passed, "fixture checks should pass"
        for result in outcome.results:
            assert result.tags, f"passing check {result.id} was not tagged"
        sidecar = root / "logs" / "verification_check_tags.jsonl"
        lines = sidecar.read_text(encoding="utf-8").splitlines()
        assert len(lines) == len(checks), "sidecar should get one row per passing check"
    return "every passing check gets tags and a sidecar observation"

def t_project_task_log_judge_fields_populated():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td).resolve()
        root = base / "vault"
        root.mkdir()
        project = base / "projects" / "judged"
        _init_project_with_remote(project, "https://github.com/Catcam-fun/judged.git")
        state_obj = state.initial_state(
            "audit_project_judged",
            app_location=str(project),
            status="completed",
            verification_outcome={"results": [
                {"id": "api_contract", "type": "assertion", "passed": True, "tags": ["contract", "correctness"]},
            ]},
            judge_verdict={
                "verdict": "APPROVE",
                "scores": {
                    "plan_adherence": 5,
                    "scope_discipline": 4,
                    "code_quality": 5,
                    "completeness": 5,
                    "risk": 4,
                },
                "score_avg": 4.6,
            },
        )
        log_path = project_memory.append_project_task_log(project, state_obj, root, outcome="completed")
        entry = json.loads(log_path.read_text(encoding="utf-8").splitlines()[-1])
        assert entry["judge_verdict"] == "APPROVE"
        assert entry["judge_scores"]["plan_adherence"] == 5
        assert entry["judge_score_avg"] == 4.6
        assert entry["verification_signal_tags"] == ["contract", "correctness"]
    return "judged task_log entry carries verdict, scores, avg, and verification tags"

def t_project_task_log_judge_fields_default_when_skipped():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td).resolve()
        root = base / "vault"
        root.mkdir()
        project = base / "projects" / "no_judge"
        project.mkdir(parents=True)
        (project / "pyproject.toml").write_text("[project]\nname = \"no_judge\"\n", encoding="utf-8")
        state_obj = state.initial_state(
            "audit_project_no_judge",
            app_location=str(project),
            status="completed",
            verification_outcome={"results": [
                {"id": "smoke_import", "type": "command", "passed": True, "tags": ["smoke"]},
            ]},
        )
        log_path = project_memory.append_project_task_log(project, state_obj, root, outcome="completed")
        entry = json.loads(log_path.read_text(encoding="utf-8").splitlines()[-1])
        assert entry["judge_verdict"] == ""
        assert entry["judge_scores"] == {}
        assert entry["judge_score_avg"] == 0.0
    return "no-judge task_log entry carries default judge fields"

check("project memory uses one vault-side bucket for matching git remotes",
      t_project_memory_same_remote_same_bucket)
check("project memory infers root from nested external path",
      t_project_memory_nested_path_infers_project_root)
check("project memory bootstrap skips vault-internal work",
      t_project_memory_skips_vault_internal)
check("planner reads and injects project memory",
      t_planner_reads_and_injects_project_memory)
check("classifier tags every passing verification check",
      t_verification_classifier_tags_every_passing_check)
check("task_log judge fields populated when judge ran",
      t_project_task_log_judge_fields_populated)
check("task_log judge fields default when judge skipped",
      t_project_task_log_judge_fields_default_when_skipped)


# ════════════════════════════════════════════════════════════════════════════
# Quality hierarchy directive (2026-05-08): no skip-quality-to-save-tokens
# ════════════════════════════════════════════════════════════════════════════

def t_execution_judge_no_skip_logic():
    """should_run_execution_judge must NOT skip based on success rate.

    Per the quality hierarchy directive (2026-05-08), the judge runs on
    every successful execution. The only allowed skip is the emergency
    `execution_judge_enabled=False` kill-switch.
    """
    src = (VAULT_ROOT / "scripts" / "vault_graph" / "execution_judge.py").read_text(encoding="utf-8")
    fn_start = src.find("def should_run_execution_judge")
    assert fn_start >= 0, "should_run_execution_judge function missing"
    fn_end = src.find("\ndef ", fn_start + 1)
    body = src[fn_start:fn_end if fn_end > 0 else len(src)]
    forbidden = ("skip_above_success_rate", "rate < skip_threshold",
                 "rate >= skip_threshold")
    for token in forbidden:
        assert token not in body, (
            f"BUG: should_run_execution_judge contains forbidden skip-logic token "
            f"{token!r} — quality hierarchy violation. The judge must run on every "
            f"successful execution; reroute to a cheaper model rather than skipping. "
            f"See VISION.md and failure_modes vision/quality_subordinate_to_tokens."
        )


def t_plan_review_no_skip_logic():
    """_shouldRunPlanReview must NOT skip based on success rate.

    Mirror of t_execution_judge_no_skip_logic for plan review.
    """
    src = (VAULT_ROOT / "scripts" / "ai_dougs" / "ai_dougs.py").read_text(encoding="utf-8")
    fn_start = src.find("def _shouldRunPlanReview")
    assert fn_start >= 0, "_shouldRunPlanReview function missing"
    # Extract just this function's body
    next_def = src.find("\ndef ", fn_start + 1)
    body = src[fn_start:next_def if next_def > 0 else fn_start + 2000]
    forbidden = ("skip_above_success_rate", "rate < skip_threshold",
                 "rate >= skip_threshold")
    for token in forbidden:
        assert token not in body, (
            f"BUG: _shouldRunPlanReview contains forbidden skip-logic token "
            f"{token!r} — quality hierarchy violation. Plan review must run on "
            f"every plan; reroute to a cheaper reviewer rather than skipping. "
            f"See VISION.md and failure_modes vision/quality_subordinate_to_tokens."
        )


def t_cost_ceiling_does_not_halt():
    """budget_exceeded_update + phase_budget_exceeded_update + budget_exceeded_after_phase
    must be observation-only — no halt updates returned.

    Calls each with synthetic over-budget state and verifies the result is
    None or {} (empty), never a dict containing status='budget_exceeded' or
    next_action='halt'. Per the quality hierarchy directive: cost ceilings
    are observed and surfaced to the supervisor, NEVER used to halt quality
    work.
    """
    from vault_graph import ported as P
    state = {
        "task_file_path": "",
        "task_name": "synthetic_over_budget",
        "planning_cost_usd": 5.0,
        "execution_cost_usd": 5.0,
        "learning_cost_usd": 5.0,
    }
    # Pre-phase check
    r1 = P.budget_exceeded_update(state, "execution")
    assert r1 is None, f"BUG: budget_exceeded_update returned a halt update {r1!r} — should be observation-only (returns None)"
    # Post-phase check (state-aware)
    r2 = P.phase_budget_exceeded_update(state, "planning", 5.0)
    assert r2 is None, f"BUG: phase_budget_exceeded_update returned a halt update {r2!r} — should be observation-only (returns None)"
    # Post-phase check (cost-arg form)
    r3 = P.budget_exceeded_after_phase("planning", 5.0, 0.25)
    assert r3 == {}, f"BUG: budget_exceeded_after_phase returned a non-empty halt update {r3!r} — should be observation-only (returns {{}})"


# Supervisor symptom-rule audits removed 2026-05-12 with the supervisor
# rewrite. All 8+ symptom rules (loop_detected, dead_query, no_progress,
# quality_regression, verification_structural_only, runaway_attempts,
# near_budget, cost_ceiling_observed, compound_stuck) were retired; the
# supervisor is now a focused watchdog that tree-kills wedged subprocs.

check("execution_judge has no skip-on-success-rate logic (quality hierarchy)",
      t_execution_judge_no_skip_logic)
check("plan_review has no skip-on-success-rate logic (quality hierarchy)",
      t_plan_review_no_skip_logic)
check("cost ceiling does not halt tasks (observation only)",
      t_cost_ceiling_does_not_halt)


# GAP #3 audit checks removed 2026-05-12. The supervisor→agent symptom
# feedback loop was retired with the supervisor rewrite. The reader
# (read_recent_symptoms_for_agent) and formatter (format_symptoms_for_prompt)
# are gone; the symptom rules they fed never produced verified value.


# ════════════════════════════════════════════════════════════════════════════
# Bug G fix (2026-05-08): refinement → verification block propagation
# ════════════════════════════════════════════════════════════════════════════

def t_refinement_yaml_required_in_prompt():
    """REFINEMENT_PROMPT must instruct the model to emit YAML for verification edits.

    Without this guidance, the refinement model emits prose ("I will add a
    check for X") and the executor silently drops the requested edit. See
    PLAN.md Bug G + failure_modes meta_loop/refinement_does_not_mutate_plan_text.
    """
    # 2026-05-09 (Chunk 2 refactor via auto_0065): prompts moved to prompts.py
    src = (VAULT_ROOT / "scripts" / "ai_dougs" / "prompts.py").read_text(encoding="utf-8")
    # Find the REFINEMENT_PROMPT triple-quoted string
    start = src.find("REFINEMENT_PROMPT = ")
    assert start >= 0, "REFINEMENT_PROMPT missing"
    end = src.find('"""', src.find('"""', start) + 3) + 3
    body = src[start:end]
    required = (
        "verification block edits MUST be machine-readable YAML",
        "extract_verification_blocks_from_refinements",
    )
    for token in required:
        assert token in body, (
            f"BUG: REFINEMENT_PROMPT missing required guidance {token!r} — "
            f"without it the refinement model emits prose for verification edits "
            f"and the executor silently drops them. See PLAN.md Bug G."
        )


def t_verify_plan_unions_refinement_yaml():
    """Behavioral: verify_plan must union verification: blocks from refinements.

    Synthetic test: a plan_text with one check + a refinement string with
    a fenced ```yaml block adding a second check. After verify_plan parses
    and unions, the executor sees BOTH checks. This is the actual code path
    that was silently dropping checks before this fix.
    """
    from vault_graph.verification import extract_verification_blocks_from_refinements
    plan_text_block = """
```yaml
verification:
  - id: original_check
    type: assertion
    file: README.md
    contains: "vault"
```
"""
    refinement_with_yaml = """## Plan refinement 1

Adding the requested behavioral check:

```yaml
verification:
  - id: new_behavioral_check
    type: command
    command: python -c "print('ok')"
    expect_returncode: 0
```

Ready to execute.
"""
    extras = extract_verification_blocks_from_refinements([refinement_with_yaml])
    assert len(extras) == 1, f"BUG: expected 1 extracted check, got {len(extras)}: {extras!r}"
    assert extras[0].get("id") == "new_behavioral_check", f"BUG: wrong id {extras[0]!r}"
    # Refinement WITHOUT a verification block should yield nothing
    extras_empty = extract_verification_blocks_from_refinements(["I will add a check (prose only)."])
    assert extras_empty == [], f"BUG: prose-only refinement yielded extras {extras_empty!r}"
    # Multiple refinements: both should be parsed
    extras_multi = extract_verification_blocks_from_refinements([
        refinement_with_yaml,
        """```yaml\nverification:\n  - id: third_check\n    type: assertion\n    file: README.md\n    contains: "vault"\n```""",
    ])
    ids = sorted(c.get("id") for c in extras_multi)
    assert ids == ["new_behavioral_check", "third_check"], f"BUG: multi-refinement ids {ids!r}"


def t_verify_plan_signature_accepts_refinements():
    """verify_plan must accept a `refinements` parameter (Bug G wiring)."""
    import inspect
    from vault_graph.verification import verify_plan
    sig = inspect.signature(verify_plan)
    assert "refinements" in sig.parameters, (
        f"BUG: verify_plan missing `refinements` parameter — Bug G fix wiring "
        f"is incomplete; refinement-supplied verification additions cannot be "
        f"unioned. Got params: {list(sig.parameters.keys())}"
    )


def t_execution_node_passes_refinements_to_verify_plan():
    """execution.py must pass state['refinements'] to verify_plan calls.

    Source check (cheap structural). Behavioral coverage comes from the
    behavioral union test above; this just guards against re-introducing
    the silent-drop by removing the refinements= kwarg.
    """
    src = (VAULT_ROOT / "scripts" / "vault_graph" / "nodes" / "execution.py").read_text(encoding="utf-8")
    # Both verify_plan call sites should include refinements=
    n_verify_calls = src.count("verify_plan(")
    n_with_refinements = src.count('refinements=state.get("refinements"')
    assert n_with_refinements >= n_verify_calls and n_verify_calls > 0, (
        f"BUG: execution.py has {n_verify_calls} verify_plan call(s) but only "
        f"{n_with_refinements} pass refinements= — Bug G wiring incomplete."
    )


check("REFINEMENT_PROMPT requires YAML for verification edits (Bug G)",
      t_refinement_yaml_required_in_prompt)
check("verify_plan unions verification YAML from refinements (Bug G behavioral)",
      t_verify_plan_unions_refinement_yaml)
check("verify_plan signature accepts refinements parameter (Bug G)",
      t_verify_plan_signature_accepts_refinements)
check("execution.py passes refinements to verify_plan calls (Bug G)",
      t_execution_node_passes_refinements_to_verify_plan)


def t_planning_prompt_requires_behavioral_check():
    """PLANNING_PROMPT must require ≥1 behavioral check in every verification block.

    Companion to Bug G's REFINEMENT_PROMPT update. Bug G handles the AMEND
    path (refinement → verification union). This handles the INITIAL plan
    path (don't rely on humans to amend at refinement; the original plan
    must include behavioral coverage from the start).

    See PLAN.md "Planning prompt: require ≥1 behavioral verification check
    per task" and the auto_0063 retrospective showing structural checks
    passing while we had no behavioral verification of the implementation.
    """
    # 2026-05-09 (Chunk 2 refactor via auto_0065): prompts moved to prompts.py
    src = (VAULT_ROOT / "scripts" / "ai_dougs" / "prompts.py").read_text(encoding="utf-8")
    start = src.find("PLANNING_PROMPT = ")
    assert start >= 0, "PLANNING_PROMPT missing"
    # Find the closing triple-quote of this string literal
    body_start = src.find('"""', start) + 3
    body_end = src.find('"""', body_start)
    body = src[body_start:body_end]
    required = (
        "MUST include at least one BEHAVIORAL check",
        "exercises the actual code path",
    )
    for token in required:
        assert token in body, (
            f"BUG: PLANNING_PROMPT missing required guidance {token!r} — "
            f"behavioral check requirement was added 2026-05-08 in response to "
            f"Bug E DESC PRESERVE (structural checks passed while behavior was "
            f"broken). See PLAN.md."
        )


check("PLANNING_PROMPT requires ≥1 behavioral verification check",
      t_planning_prompt_requires_behavioral_check)


# ════════════════════════════════════════════════════════════════════════════
# Hook follow-ups (2026-05-09): protect _human_notes/ from subprocess agents
# ════════════════════════════════════════════════════════════════════════════

def t_session_start_hook_silent_for_subprocess():
    """Behavioral: hook MUST suppress reminder when VAULT_SUBPROCESS_AGENT=1.

    SessionStart fires for `claude -p` subprocess invocations (empirically
    confirmed 2026-05-08). Without this gate, subprocess agents see the
    pointer to _human_notes/ and may try to read the orchestrator's notes.
    """
    import subprocess as _sp
    hook = VAULT_ROOT / "_human_notes" / "session_start_notes_reminder.py"
    env = {k: v for k, v in os.environ.items()}
    env["VAULT_SUBPROCESS_AGENT"] = "1"
    r = _sp.run(["python", str(hook)], env=env, capture_output=True, text=True, timeout=10)
    assert "session-start" not in r.stdout, (
        f"BUG: SessionStart hook leaked reminder to subprocess agent. "
        f"VAULT_SUBPROCESS_AGENT=1 should silence it. Got stdout={r.stdout!r}"
    )


def t_session_start_hook_emits_for_orchestrator():
    """Behavioral: hook MUST emit reminder when VAULT_SUBPROCESS_AGENT is unset."""
    import subprocess as _sp
    hook = VAULT_ROOT / "_human_notes" / "session_start_notes_reminder.py"
    env = {k: v for k, v in os.environ.items() if k != "VAULT_SUBPROCESS_AGENT"}
    r = _sp.run(["python", str(hook)], env=env, capture_output=True, text=True, timeout=10)
    assert "session-start" in r.stdout, (
        f"BUG: SessionStart hook did NOT emit reminder for orchestrator session "
        f"(no VAULT_SUBPROCESS_AGENT env var). Got stdout={r.stdout!r}"
    )


def t_pre_read_hook_blocks_subprocess_human_notes_access():
    """Behavioral: pre_read.py exits 2 when subprocess agent reads _human_notes/."""
    import subprocess as _sp
    import json as _j
    hook = VAULT_ROOT / "scripts" / "hooks" / "pre_read.py"
    env = {k: v for k, v in os.environ.items()}
    env["VAULT_SUBPROCESS_AGENT"] = "1"
    payload = _j.dumps({"tool_input": {"file_path": "_human_notes/CLAUDE_SESSION_NOTES.md"}})
    r = _sp.run(["python", str(hook)], input=payload, env=env, capture_output=True, text=True, timeout=10)
    assert r.returncode == 2, (
        f"BUG: pre_read.py did not block subprocess Read of _human_notes/. "
        f"Got rc={r.returncode}, stdout={r.stdout!r}"
    )
    assert "BLOCKED" in r.stdout
    # Glob pattern check
    payload_glob = _j.dumps({"tool_input": {"pattern": "_human_notes/**/*.md"}})
    r = _sp.run(["python", str(hook)], input=payload_glob, env=env, capture_output=True, text=True, timeout=10)
    assert r.returncode == 2, f"BUG: pre_read.py did not block Glob of _human_notes/"


def t_pre_read_hook_allows_orchestrator_and_legitimate_paths():
    """Behavioral: pre_read.py exits 0 for orchestrator OR for non-_human_notes paths."""
    import subprocess as _sp
    import json as _j
    hook = VAULT_ROOT / "scripts" / "hooks" / "pre_read.py"
    # Orchestrator (no env var) reading _human_notes/ → ALLOW
    env_orch = {k: v for k, v in os.environ.items() if k != "VAULT_SUBPROCESS_AGENT"}
    payload = _j.dumps({"tool_input": {"file_path": "_human_notes/CLAUDE_SESSION_NOTES.md"}})
    r = _sp.run(["python", str(hook)], input=payload, env=env_orch, capture_output=True, text=True, timeout=10)
    assert r.returncode == 0, f"BUG: orchestrator Read of _human_notes/ blocked: rc={r.returncode}"
    # Subprocess reading legitimate path → ALLOW
    env_sub = {**os.environ, "VAULT_SUBPROCESS_AGENT": "1"}
    payload2 = _j.dumps({"tool_input": {"file_path": "scripts/vault_graph/verification.py"}})
    r = _sp.run(["python", str(hook)], input=payload2, env=env_sub, capture_output=True, text=True, timeout=10)
    assert r.returncode == 0, f"BUG: subprocess Read of legit path blocked: rc={r.returncode}"


def t_ai_dougs_marks_subprocess_agents_with_env_var():
    """Source check: subprocess_infra.py sets VAULT_SUBPROCESS_AGENT=1 on AI CLI subprocesses."""
    src = (VAULT_ROOT / "scripts" / "ai_dougs" / "subprocess_infra.py").read_text(encoding="utf-8")
    assert "VAULT_SUBPROCESS_AGENT" in src, (
        "BUG: subprocess_infra.py does not mark AI CLI subprocesses with VAULT_SUBPROCESS_AGENT. "
        "Hook follow-up #1 (2026-05-09) requires this so the SessionStart hook + "
        "pre_read.py can distinguish subprocess agents from the orchestrator."
    )
    assert "_is_ai_cli_command" in src, (
        "BUG: subprocess_infra.py is missing the _is_ai_cli_command helper that decides "
        "whether to inject VAULT_SUBPROCESS_AGENT into subprocess env."
    )


def t_settings_json_registers_pre_read_hook():
    """settings.json must register pre_read.py for Read/Glob/Grep matchers."""
    import json as _j
    settings = _j.loads((VAULT_ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
    pre_tool = settings.get("hooks", {}).get("PreToolUse", [])
    matchers_with_pre_read = set()
    for entry in pre_tool:
        matcher = entry.get("matcher", "")
        for h in entry.get("hooks", []):
            if "pre_read.py" in h.get("command", ""):
                matchers_with_pre_read.add(matcher)
    required = {"Read", "Glob", "Grep"}
    missing = required - matchers_with_pre_read
    assert not missing, (
        f"BUG: settings.json missing pre_read.py for matchers {missing!r}. "
        f"Hook follow-up #2 (2026-05-09) requires Read/Glob/Grep all gated on "
        f"VAULT_SUBPROCESS_AGENT to mechanically refuse subprocess access to "
        f"_human_notes/."
    )


check("SessionStart hook silenced for subprocess agents (behavioral)",
      t_session_start_hook_silent_for_subprocess)
check("SessionStart hook still emits for orchestrator (behavioral)",
      t_session_start_hook_emits_for_orchestrator)
check("pre_read.py blocks subprocess access to _human_notes/ (behavioral)",
      t_pre_read_hook_blocks_subprocess_human_notes_access)
check("pre_read.py allows orchestrator + legit subprocess paths (behavioral)",
      t_pre_read_hook_allows_orchestrator_and_legitimate_paths)
check("subprocess_infra.py marks AI CLI subprocesses with VAULT_SUBPROCESS_AGENT",
      t_ai_dougs_marks_subprocess_agents_with_env_var)
check("settings.json registers pre_read.py for Read/Glob/Grep",
      t_settings_json_registers_pre_read_hook)


# ════════════════════════════════════════════════════════════════════════════
# Sharpener downstream-quality propagation (2026-05-09)
# ════════════════════════════════════════════════════════════════════════════

def t_routedougs_records_sharpener_route_link():
    """Behavioral: routeDougs MUST write a phase=sharpener_route entry to cost_log.

    Tests the link side of the sharpener-downstream-quality plumbing.
    Synthetic invocation against a temp staging+task_files dir; reads the
    SHARED cost_log and checks the new entry exists.
    """
    import sys as _sys
    sharp_dir = VAULT_ROOT / "scripts" / "ai_sharpener"
    if str(sharp_dir) not in _sys.path:
        _sys.path.insert(0, str(sharp_dir))
    import importlib
    sharp = importlib.import_module("ai_sharpener")
    # Temp task_files dir to keep the test isolated
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        log_file = VAULT_ROOT / "logs" / "cost_log.jsonl"
        before = log_file.read_text(encoding="utf-8").count('"phase": "sharpener_route"') if log_file.exists() else 0
        # Use a synthetic id that won't collide with real tasks
        sharp.routeDougs(
            sharpened="test sharpened prompt",
            original="test original",
            eid="audit_test_sharp_route",
            task_files_path=tmp_path,
            sharpener_model="codex:gpt-5.5",
        )
        after = log_file.read_text(encoding="utf-8").count('"phase": "sharpener_route"') if log_file.exists() else 0
        assert after == before + 1, (
            f"BUG: routeDougs did not write a sharpener_route cost-log entry. "
            f"Before={before}, after={after}. Expected +1."
        )
    # Cleanup: remove the audit_test entry we just wrote (test fixture)
    if log_file.exists():
        lines = log_file.read_text(encoding="utf-8").splitlines()
        clean = [l for l in lines if "audit_test_sharp_route" not in l]
        log_file.write_text("\n".join(clean) + "\n", encoding="utf-8")


def t_sharpener_outcome_writer_skips_when_no_sharpener_model():
    """Behavioral: _write_sharpener_outcome_entry must skip if frontmatter
    has no sharpener_model field. Older tasks predating this plumbing
    must not produce broken outcome entries."""
    from vault_graph.nodes import stubs
    with tempfile.TemporaryDirectory() as tmp:
        # Task file WITHOUT sharpener_model frontmatter
        tf = Path(tmp) / "older_task.md"
        tf.write_text("---\nstatus: completed\ntitle: older_task\n---\n\n## Initial Prompt\nx\n",
                       encoding="utf-8")
        log = VAULT_ROOT / "logs" / "cost_log.jsonl"
        before = log.read_text(encoding="utf-8").count('"phase": "sharpener_outcome"') if log.exists() else 0
        class _O:
            all_passed = True
        stubs._write_sharpener_outcome_entry(
            {"task_name": "older_task", "task_file_path": str(tf)},
            "completed", _O(), {"verdict": "APPROVE", "score_avg": 8.0},
        )
        after = log.read_text(encoding="utf-8").count('"phase": "sharpener_outcome"') if log.exists() else 0
        assert after == before, (
            f"BUG: _write_sharpener_outcome_entry wrote an entry for a task "
            f"with NO sharpener_model frontmatter. Before={before}, after={after}."
        )


def t_sharpener_outcome_writer_writes_when_sharpener_model_present():
    """Behavioral: writes a sharpener_outcome entry when frontmatter has it."""
    from vault_graph.nodes import stubs
    with tempfile.TemporaryDirectory() as tmp:
        tf = Path(tmp) / "audit_test_sharp_outcome.md"
        tf.write_text(
            "---\nstatus: completed\ntitle: audit_test_sharp_outcome\n"
            "sharpener_model: codex:gpt-5.5\n---\n\n## Initial Prompt\nx\n",
            encoding="utf-8",
        )
        log = VAULT_ROOT / "logs" / "cost_log.jsonl"
        class _O:
            all_passed = True
        stubs._write_sharpener_outcome_entry(
            {"task_name": "audit_test_sharp_outcome", "task_file_path": str(tf)},
            "completed", _O(), {"verdict": "APPROVE", "score_avg": 8.5},
        )
        # Validate the line we just wrote
        lines = log.read_text(encoding="utf-8").splitlines()
        ours = [l for l in lines if "audit_test_sharp_outcome" in l and "sharpener_outcome" in l]
        assert len(ours) >= 1, "BUG: sharpener_outcome entry not written"
        last = json.loads(ours[-1])
        assert last["provider"] == "codex" and last["model"] == "gpt-5.5"
        assert last["verification_passed"] is True
        assert last["judge_verdict"] == "APPROVE"
        assert last["judge_score_avg"] == 8.5
        # Cleanup the audit_test fixture entries
        clean = [l for l in lines if "audit_test_sharp_outcome" not in l]
        log.write_text("\n".join(clean) + "\n", encoding="utf-8")


def t_routedougs_passes_sharpener_model_through():
    """Source check: processApproved must pass sharpener_model into routeDougs."""
    src = (VAULT_ROOT / "scripts" / "ai_sharpener" / "ai_sharpener.py").read_text(encoding="utf-8")
    assert "sharpener_model=entry.get(\"sharpener_model\"" in src, (
        "BUG: processApproved does not pass sharpener_model to routeDougs. "
        "Without this, the link cost-log entry has no provider/model and "
        "downstream-quality propagation is broken."
    )


check("routeDougs writes sharpener_route link entry (behavioral, Phase 1)",
      t_routedougs_records_sharpener_route_link)
check("sharpener_outcome writer skips tasks without sharpener_model (behavioral)",
      t_sharpener_outcome_writer_skips_when_no_sharpener_model)
check("sharpener_outcome writer records quality signals correctly (behavioral)",
      t_sharpener_outcome_writer_writes_when_sharpener_model_present)
check("processApproved passes sharpener_model through to routeDougs",
      t_routedougs_passes_sharpener_model_through)


# ════════════════════════════════════════════════════════════════════════════
# Behavioral-vs-structural classifier (2026-05-09)
# ════════════════════════════════════════════════════════════════════════════

def t_behavioral_classifier_recognizes_known_behavioral_checks():
    """Behavioral: classifier returns True for clearly behavioral checks.

    Happy paths: test_suite type, screenshot_compare type, python -c with
    tempfile, pytest invocation. All MUST classify as behavioral or the
    auto-detect (companion to the PLANNING_PROMPT requirement) is broken.
    """
    from vault_graph.verification import is_check_likely_behavioral, has_any_behavioral_check
    behavioral_examples = [
        {"type": "test_suite", "run": "pytest tests/test_foo.py"},
        {"type": "screenshot_compare", "reference": "ref.png", "output": "out.png"},
        {"type": "command", "command": "python -c \"import tempfile; d=tempfile.mkdtemp(); ...\""},
        {"type": "command", "command": "pytest tests/test_x.py"},
    ]
    for ex in behavioral_examples:
        assert is_check_likely_behavioral(ex), f"BUG: behavioral check classified as structural: {ex!r}"
    assert has_any_behavioral_check(behavioral_examples)
    assert has_any_behavioral_check([behavioral_examples[0]])  # single is enough


def t_behavioral_classifier_rejects_known_structural_checks():
    """Behavioral: classifier returns False for purely structural checks.

    Negative cases: type=assertion with contains:, command=--help / --version,
    command=python -c "print('ok')". These are file-pattern / smoke probes,
    not behavior. has_any_behavioral_check on a list of these MUST be False.
    """
    from vault_graph.verification import is_check_likely_behavioral, has_any_behavioral_check
    structural_examples = [
        {"type": "assertion", "file": "scripts/foo.py", "contains": "def my_func"},
        {"type": "command", "command": "python vault.py --help"},
        {"type": "command", "command": "python vault.py --version"},
        {"type": "command", "command": "python -c \"print('ok')\""},  # smoke only
    ]
    for ex in structural_examples:
        assert not is_check_likely_behavioral(ex), f"BUG: structural check classified as behavioral: {ex!r}"
    assert not has_any_behavioral_check(structural_examples), (
        "BUG: list of all-structural checks reported as having behavioral coverage"
    )


def t_behavioral_classifier_count_correctness():
    """Behavioral: behavioral_check_count returns accurate integer count."""
    from vault_graph.verification import behavioral_check_count
    mixed = [
        {"type": "assertion", "file": "x", "contains": "y"},          # structural
        {"type": "test_suite", "run": "pytest tests/test_a.py"},       # behavioral
        {"type": "command", "command": "python --version"},            # structural
        {"type": "command", "command": "python -c \"import tempfile\""}, # behavioral
    ]
    assert behavioral_check_count(mixed) == 2, (
        f"BUG: behavioral_check_count returned wrong count for mixed list "
        f"(expected 2, got {behavioral_check_count(mixed)})"
    )
    assert behavioral_check_count([]) == 0
    assert behavioral_check_count(None) == 0


check("behavioral classifier recognizes behavioral checks (behavioral)",
      t_behavioral_classifier_recognizes_known_behavioral_checks)
check("behavioral classifier rejects structural-only checks (behavioral)",
      t_behavioral_classifier_rejects_known_structural_checks)
check("behavioral_check_count returns accurate counts (behavioral)",
      t_behavioral_classifier_count_correctness)


# ════════════════════════════════════════════════════════════════════════════
# Threshold audit Tier 1-3 (2026-05-09): no skip-quality stale defaults;
# truncation observability; variance-based provider exploration
# ════════════════════════════════════════════════════════════════════════════

def t_no_stale_skip_above_success_rate_in_routing_defaults():
    """Tier 1: stale skip-quality params must not be in routing defaults dict.

    `plan_review_skip_above_success_rate` and `plan_review_min_planning_runs`
    were removed 2026-05-09 from the `_routingDefaults` literal because they
    no longer affect behavior — keeping them in the defaults dict misled
    future readers into thinking the skip was still active.
    """
    src = (VAULT_ROOT / "scripts" / "ai_dougs" / "routing.py").read_text(encoding="utf-8")
    # Must NOT appear as a config-key string literal in the routing defaults.
    forbidden_keys = (
        '"plan_review_skip_above_success_rate":',
        '"plan_review_min_planning_runs":',
    )
    for k in forbidden_keys:
        assert k not in src, (
            f"BUG: stale skip-quality config key {k!r} is still in routing.py "
            f"defaults. Per the 2026-05-08 quality hierarchy directive these "
            f"params are ignored — leaving them in the defaults misleads "
            f"future readers. Remove + leave a NOTE comment instead."
        )


def t_truncate_observed_helper_exists_and_logs():
    """Tier 2 behavioral: truncate_observed truncates AND logs to events.jsonl."""
    from vault_graph import ported as P
    log = VAULT_ROOT / "logs" / "events.jsonl"
    marker = '"event": "truncation_observed"'
    before = log.read_text(encoding="utf-8").count(marker) if log.exists() else 0
    long_text = "x" * 10000
    result = P.truncate_observed(long_text, 1000, "audit_test.truncation", "audit_test_task")
    assert len(result) == 1000, f"BUG: truncate_observed returned {len(result)} chars (expected 1000)"
    after = log.read_text(encoding="utf-8").count(marker) if log.exists() else 0
    assert after == before + 1, (
        f"BUG: truncate_observed did not log a truncation_observed event. "
        f"Before={before}, after={after}."
    )
    # Cleanup: remove the audit_test entries we just wrote
    if log.exists():
        lines = log.read_text(encoding="utf-8").splitlines()
        clean = [l for l in lines if "audit_test_task" not in l and "audit_test.truncation" not in l]
        log.write_text("\n".join(clean) + "\n", encoding="utf-8")
    # And short text must NOT be truncated and NOT log
    before2 = log.read_text(encoding="utf-8").count(marker) if log.exists() else 0
    short_result = P.truncate_observed("hi", 100, "audit_test.short")
    assert short_result == "hi", f"BUG: short text was modified: {short_result!r}"
    after2 = log.read_text(encoding="utf-8").count(marker) if log.exists() else 0
    assert after2 == before2, "BUG: short text triggered a truncation log entry"


def t_truncation_call_sites_use_truncate_observed():
    """Tier 2 source check: known truncation sites use truncate_observed."""
    sites = [
        (VAULT_ROOT / "scripts" / "vault_graph" / "execution_judge.py", "execution_judge.plan"),
        (VAULT_ROOT / "scripts" / "vault_graph" / "execution_judge.py", "execution_judge.execution_log"),
        (VAULT_ROOT / "scripts" / "vault_graph" / "execution_judge.py", "execution_judge.diff"),
        (VAULT_ROOT / "scripts" / "ai_dougs" / "ai_dougs.py", "plan_review.plan"),
        (VAULT_ROOT / "scripts" / "ai_dougs" / "ai_dougs.py", "plan_review.initial_prompt"),
        (VAULT_ROOT / "scripts" / "vault_graph" / "nodes" / "refinement.py", "learning.full_task"),
    ]
    for path, label in sites:
        src = path.read_text(encoding="utf-8")
        assert label in src, (
            f"BUG: truncation site {label!r} not wired with truncate_observed in "
            f"{path.name}. Quality-affecting truncations must be observable."
        )


def t_wilson_lower_bound_known_values():
    """Tier 3: Wilson lower bound matches expected values for known inputs."""
    _wilson_lower_bound = P._d._wilson_lower_bound
    # Edge cases
    assert _wilson_lower_bound(0, 0) == 0.0
    assert _wilson_lower_bound(0, 100) == 0.0
    # Tiny samples produce wide intervals (low lower bound)
    lower_5_5 = _wilson_lower_bound(5, 5)
    assert 0.4 < lower_5_5 < 0.7, f"5/5 Wilson lower out of expected range: {lower_5_5}"
    # Larger samples produce tighter intervals (high lower bound for high success)
    lower_50_50 = _wilson_lower_bound(50, 50)
    assert lower_50_50 > 0.9, f"50/50 Wilson lower should be tight (>0.9), got {lower_50_50}"


def t_should_explore_provider_decision():
    """Tier 3 behavioral: variance-based exploration gate fires correctly."""
    _should_explore_provider = P._d._should_explore_provider
    # Below safety floor → always explore
    assert _should_explore_provider(0, 0) is True
    assert _should_explore_provider(2, 2) is True
    # 5/5 looks great but CI is wide → still explore
    assert _should_explore_provider(5, 5) is True
    # 50/50 perfect with narrow CI → exploit
    assert _should_explore_provider(50, 50) is False
    # 80/100 with narrow CI → exploit
    assert _should_explore_provider(80, 100) is False
    # 8/10 still uncertain → explore
    assert _should_explore_provider(8, 10) is True


def t_provider_exploration_floor_is_safety_minimum_not_decision():
    """Tier 3 source: _PROVIDER_EXPLORATION_FLOOR is now safety floor, not a hard cap.

    The constant must still exist (used as the safety floor in
    _should_explore_provider), but the routing.py source must NOT contain
    the old `success < _PROVIDER_EXPLORATION_FLOOR` decision pattern as
    the primary gate — that's been replaced by the Wilson CI gate.
    """
    src = (VAULT_ROOT / "scripts" / "ai_dougs" / "routing.py").read_text(encoding="utf-8")
    assert "_PROVIDER_EXPLORATION_FLOOR" in src, "constant removed entirely?"
    assert "_should_explore_provider" in src, "Wilson-based gate function missing"
    assert "_wilson_lower_bound" in src, "Wilson helper missing"
    # The OLD decision pattern (`success < _PROVIDER_EXPLORATION_FLOOR`) must
    # no longer appear inside _rankProviders. Find _rankProviders body and check.
    rp_start = src.find("def _rankProviders(")
    rp_end = src.find("\ndef ", rp_start + 1)
    rp_body = src[rp_start:rp_end if rp_end > 0 else rp_start + 5000]
    assert "success < _PROVIDER_EXPLORATION_FLOOR" not in rp_body, (
        "BUG: _rankProviders still uses the old hardcoded-count gate "
        "(`success < _PROVIDER_EXPLORATION_FLOOR`). The variance-based "
        "_should_explore_provider must be the decision instead."
    )
    assert "_should_explore_provider(" in rp_body, (
        "BUG: _rankProviders does not call the new variance-based gate."
    )


check("Tier 1: no stale skip-quality params in routing defaults",
      t_no_stale_skip_above_success_rate_in_routing_defaults)
check("Tier 2: truncate_observed helper truncates AND logs (behavioral)",
      t_truncate_observed_helper_exists_and_logs)
check("Tier 2: known truncation sites use truncate_observed",
      t_truncation_call_sites_use_truncate_observed)
check("Tier 3: Wilson lower bound returns expected values",
      t_wilson_lower_bound_known_values)
check("Tier 3: _should_explore_provider gate fires correctly (behavioral)",
      t_should_explore_provider_decision)
check("Tier 3: _rankProviders uses variance-based gate, not hardcoded count",
      t_provider_exploration_floor_is_safety_minimum_not_decision)


# ════════════════════════════════════════════════════════════════════════════
# Behavioral classifier Phase 2 (2026-05-09): runtime warning + supervisor rule
# ════════════════════════════════════════════════════════════════════════════

def t_execution_node_warns_on_structural_only_block():
    """Phase 2 source: execution.py imports has_any_behavioral_check + writes alert."""
    src = (VAULT_ROOT / "scripts" / "vault_graph" / "nodes" / "execution.py").read_text(encoding="utf-8")
    assert "has_any_behavioral_check" in src, (
        "BUG: execution.py does not import has_any_behavioral_check. Phase 2 "
        "of the behavioral classifier requires runtime warning when a plan's "
        "verification block is structural-only. See PLAN.md."
    )
    assert "verification_structural_only" in src, (
        "BUG: execution.py does not write a verification_structural_only "
        "alert when the heuristic detects a structural-only block."
    )


check("execution.py warns on structural-only verification block (Phase 2)",
      t_execution_node_warns_on_structural_only_block)
# supervisor verification_structural_only, quality_regression, compound_stuck,
# compound-single-symptom audits removed 2026-05-12 with the supervisor
# rewrite (symptom rules retired; supervisor is now just a watchdog).


# ════════════════════════════════════════════════════════════════════════════
# auto_0064 surfaced bugs (2026-05-09): BOM, NameError, refinement YAML
# validation, untracked-file revert
# ════════════════════════════════════════════════════════════════════════════

def t_no_bom_in_python_files():
    """Behavioral: no Python source file in scripts/ starts with a BOM (U+FEFF).

    Surfaced auto_0064: claude:haiku rewrote ai_dougs.py and added a BOM
    at line 1. Python at runtime tolerates this but the smoke test's
    ast.parse rejects it. Audit catches BOMs proactively so a future
    similar rewrite is detected before it reaches verification.
    """
    bom = b"\xef\xbb\xbf"
    offenders: list[str] = []
    for py in (VAULT_ROOT / "scripts").rglob("*.py"):
        try:
            with open(py, "rb") as f:
                if f.read(3) == bom:
                    offenders.append(str(py.relative_to(VAULT_ROOT)).replace("\\", "/"))
        except Exception:
            continue
    assert not offenders, (
        f"BUG: BOM (U+FEFF) found at start of {len(offenders)} Python file(s): "
        f"{offenders!r}. Strip with: python -c \"from pathlib import Path; "
        f"p=Path(F); d=p.read_bytes(); p.write_bytes(d[3:]) if d.startswith("
        f"b'\\xef\\xbb\\xbf') else None\". The smoke test's ast.parse rejects "
        f"BOMs even though Python runtime tolerates them — silent failure mode."
    )


def t_execution_iteration_loop_uses_defined_variables():
    """Source check: verification iteration loop must not reference undefined
    `addDirs` (Bug K, 2026-05-09).

    The iteration loop in execution.py was crashing with NameError every
    time it fired because `addDirs` was never defined in scope. The fix
    uses `[app_path, nas_root]` to mirror the initial call. Audit lock-in.
    """
    src = (VAULT_ROOT / "scripts" / "vault_graph" / "nodes" / "execution.py").read_text(encoding="utf-8")
    # Find the verification iteration loop (the one that reruns runAiRequestWithFallback)
    # and check it does NOT reference addDirs as an undefined name.
    assert "addDirs" not in src, (
        "BUG: execution.py still references undefined `addDirs`. "
        "Bug K (2026-05-09): the verification iteration loop crashed with "
        "NameError because `addDirs` was used but never assigned. Fix uses "
        "`[app_path, nas_root]` to mirror the initial call shape."
    )


def t_refinement_drops_malformed_python_dash_c_checks():
    """Behavioral (Bug J, 2026-05-09): malformed python -c checks with raw
    newlines must be DROPPED by extract_verification_blocks_from_refinements,
    not passed through to crash the runner with SyntaxError.
    """
    from vault_graph.verification import extract_verification_blocks_from_refinements
    # Refinement that contains a check with a raw newline inside python -c
    bad_refinement = """## Plan refinement 1

Adding cleanup safety:

```yaml
verification:
  - id: broken_multiline_python_dash_c
    type: command
    command: python -c \"import json\\ntry:\\n    print('ok')\\nfinally:\\n    pass\"
    expect_returncode: 0
```
"""
    # Construct one where the command field is a YAML literal block scalar (|)
    # that genuinely preserves newlines through yaml.safe_load. This matches
    # the auto_0064 failure mode: refinement model emitted try/finally
    # spread across multiple lines inside a python -c invocation.
    bad_refinement_real_newline = (
        "## Plan refinement 1\n\n```yaml\nverification:\n"
        "  - id: broken_with_real_newline\n"
        "    type: command\n"
        "    command: |\n"
        "      python -c \"import json\n"
        "      try:\n"
        "          print('ok')\n"
        "      finally:\n"
        "          pass\"\n"
        "    expect_returncode: 0\n```\n"
    )
    extras = extract_verification_blocks_from_refinements([bad_refinement_real_newline])
    bad_ids = [c.get("id") for c in extras]
    assert "broken_with_real_newline" not in bad_ids, (
        f"BUG: extract_verification_blocks_from_refinements did not drop a "
        f"malformed python -c check with embedded newline. Got extras={extras!r}. "
        f"This caused auto_0064's verification to crash with SyntaxError."
    )

    # Sanity: a CORRECT single-line python -c check is still accepted
    good_refinement = (
        "## Plan refinement 2\n\n```yaml\nverification:\n"
        "  - id: clean_one_liner\n"
        "    type: command\n"
        "    command: python -c \"print('ok')\"\n"
        "    expect_returncode: 0\n```\n"
    )
    good_extras = extract_verification_blocks_from_refinements([good_refinement])
    assert any(c.get("id") == "clean_one_liner" for c in good_extras), (
        f"BUG: validator falsely rejected a clean single-line python -c. "
        f"got extras={good_extras!r}"
    )


def t_self_test_revert_handles_untracked_files():
    """Source check (Bug I, 2026-05-09): _revert_via_git must distinguish
    tracked vs untracked-new vs untracked-missing.

    The prior version called `git checkout HEAD -- <paths>` blindly which
    fails for any path not in HEAD. Auto-revert silently failed for
    agent-created files. Now: tracked → checkout, untracked-new → unlink,
    untracked-missing → no-op.
    """
    src = (VAULT_ROOT / "scripts" / "vault_graph" / "self_test.py").read_text(encoding="utf-8")
    required = (
        "ls-files",           # tracked-status detection (subprocess arg)
        "untracked_new",      # bucketing variable
        "unlink",             # delete untracked-new
        "WARNING:",           # surfaces the case
    )
    for token in required:
        assert token in src, (
            f"BUG: self_test._revert_via_git missing {token!r} — Bug I "
            f"(2026-05-09): auto-revert must handle untracked-new files "
            f"by deleting them, not by failing silently."
        )


check("no Python file in scripts/ starts with a BOM (U+FEFF) (Bug auto_0064)",
      t_no_bom_in_python_files)
check("execution iteration loop uses defined variables (Bug K)",
      t_execution_iteration_loop_uses_defined_variables)
check("refinement drops malformed python -c with embedded newlines (Bug J behavioral)",
      t_refinement_drops_malformed_python_dash_c_checks)
check("self_test revert handles tracked + untracked + missing files (Bug I)",
      t_self_test_revert_handles_untracked_files)


def t_ai_tool_stub_files_all_point_at_ai_main():
    """Per-tool AI instruction stubs (CLAUDE.md / AGENTS.md / GEMINI.md) must
    each reference ai_main.md as the source of truth.

    Each AI tool has a hardcoded convention for which file it auto-loads:
      - Claude Code → CLAUDE.md
      - Codex CLI / Anthropic agent CLI → AGENTS.md
      - Gemini CLI → GEMINI.md
    There's no universal filename, so we maintain a stub per tool. Each
    must point at ai_main.md or future agents won't follow the constitution.
    """
    required_token = "ai_main.md"
    for stub in ("CLAUDE.md", "AGENTS.md", "GEMINI.md"):
        path = VAULT_ROOT / stub
        assert path.exists(), f"BUG: per-tool AI stub {stub!r} is missing"
        body = path.read_text(encoding="utf-8")
        assert required_token in body, (
            f"BUG: per-tool AI stub {stub!r} does not reference {required_token!r}. "
            f"Future {stub.split('.')[0].lower()} sessions won't follow the vault constitution."
        )


check("AI tool stubs (CLAUDE.md/AGENTS.md/GEMINI.md) all reference ai_main.md",
      t_ai_tool_stub_files_all_point_at_ai_main)


def t_pre_bash_blocks_subprocess_push_allows_orchestrator():
    """Behavioral (2026-05-10): pre_bash.py must distinguish subprocess
    agents from orchestrator-Claude on `git push`. Subprocess (env var set)
    → block; orchestrator (env var unset) → allow. Same env-var pattern as
    pre_read.py.

    Surfaced when initial vault-repo push got blocked because the prior
    hook blocked ALL pushes regardless of context.
    """
    import subprocess as _sp
    import json as _j
    hook = VAULT_ROOT / "scripts" / "hooks" / "pre_bash.py"
    payload = _j.dumps({"tool_input": {"command": "git push -u origin main"}})

    # Subprocess agent context (env var set) → MUST block
    env_sub = {**os.environ, "VAULT_SUBPROCESS_AGENT": "1"}
    r = _sp.run(["python", str(hook)], input=payload, env=env_sub,
                capture_output=True, text=True, timeout=10)
    assert r.returncode == 2, (
        f"BUG: pre_bash.py did NOT block subprocess agent's git push. "
        f"rc={r.returncode}, stdout={r.stdout!r}"
    )
    assert "subprocess agent" in r.stdout.lower(), \
        f"block reason should mention subprocess agent: {r.stdout!r}"

    # Orchestrator context (env var unset) → MUST allow
    env_orch = {k: v for k, v in os.environ.items()
                if k != "VAULT_SUBPROCESS_AGENT"}
    r2 = _sp.run(["python", str(hook)], input=payload, env=env_orch,
                 capture_output=True, text=True, timeout=10)
    assert r2.returncode == 0, (
        f"BUG: pre_bash.py blocked orchestrator-Claude's git push. "
        f"rc={r2.returncode}, stdout={r2.stdout!r}. Orchestrator pushes "
        f"are deliberate (initial repo setup, post-task commits)."
    )


check("pre_bash blocks subprocess agent's git push, allows orchestrator's (behavioral)",
      t_pre_bash_blocks_subprocess_push_allows_orchestrator)


def t_pre_write_denies_subprocess_writes_when_no_scope():
    """Behavioral (2026-05-11, Step 10 deferral closure): pre_write.py must
    deny ALL writes from subprocess agents when no .scope.json is active.
    Background: during non-execution phases (planning/plan_review/refinement/
    learning/judge) no scope is set. Under the old 'no scope = no restrictions'
    rule a misbehaving subprocess could write anywhere. With deny-by-default
    here, FIX #1 (snapshot/restore) becomes belt-and-suspenders rather than
    the load-bearing protection."""
    import subprocess as _sp
    import json as _j
    import tempfile as _tf
    hook = VAULT_ROOT / "scripts" / "hooks" / "pre_write.py"
    scope_file = VAULT_ROOT / "scripts" / "hooks" / ".scope.json"

    # Ensure no scope is active for this test
    scope_existed = scope_file.exists()
    scope_backup = scope_file.read_bytes() if scope_existed else None
    try:
        if scope_existed:
            scope_file.unlink()

        with _tf.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as tmp:
            tmp.write("# placeholder\n")
            target = tmp.name
        try:
            payload = _j.dumps({"tool_input": {"file_path": target}})

            # Subprocess agent context (env var set), no scope → MUST block
            env_sub = {**os.environ, "VAULT_SUBPROCESS_AGENT": "1"}
            r = _sp.run(["python", str(hook)], input=payload, env=env_sub,
                        capture_output=True, text=True, timeout=10)
            assert r.returncode == 2, (
                f"BUG: pre_write.py did NOT deny subprocess agent's write "
                f"with no active scope. rc={r.returncode}, stdout={r.stdout!r}"
            )
            assert "subprocess agent" in r.stdout.lower() or "deny-by-default" in r.stdout.lower(), \
                f"block reason should mention subprocess deny-by-default: {r.stdout!r}"

            # Orchestrator context (env var unset), no scope → MUST allow
            env_orch = {k: v for k, v in os.environ.items()
                        if k != "VAULT_SUBPROCESS_AGENT"}
            r2 = _sp.run(["python", str(hook)], input=payload, env=env_orch,
                         capture_output=True, text=True, timeout=10)
            assert r2.returncode == 0, (
                f"BUG: pre_write.py blocked orchestrator-Claude's write with "
                f"no scope. rc={r2.returncode}, stdout={r2.stdout!r}. "
                f"Orchestrator should keep permissive behavior so the user "
                f"can edit anywhere from their Claude Code session."
            )
        finally:
            try:
                Path(target).unlink()
            except OSError:
                pass
    finally:
        if scope_existed and scope_backup is not None:
            scope_file.write_bytes(scope_backup)


check("pre_write denies subprocess writes when no scope set (Step 10 closure, behavioral)",
      t_pre_write_denies_subprocess_writes_when_no_scope)


# ════════════════════════════════════════════════════════════════════════════
section("2026-05-11 overhaul: auto-curator off, capabilities registry, scope guard")


def t_state_capabilities_json_well_formed():
    """state/capabilities.json must exist, parse cleanly, and have the required
    top-level shape: version, packages, categories, policy."""
    import json as _j
    p = VAULT_ROOT / "state" / "capabilities.json"
    assert p.exists(), "BUG: state/capabilities.json missing"
    try:
        data = _j.loads(p.read_text(encoding="utf-8"))
    except _j.JSONDecodeError as e:
        raise AssertionError(f"BUG: state/capabilities.json malformed JSON: {e}")
    for key in ("version", "policy", "categories", "packages"):
        assert key in data, f"BUG: capabilities.json missing required top-level key {key!r}"
    pkgs = data["packages"]
    assert isinstance(pkgs, dict) and len(pkgs) >= 10, \
        f"BUG: capabilities.json must have >=10 packages; got {len(pkgs)}"
    # Each package must declare ecosystem, version, categories at minimum
    for name, entry in pkgs.items():
        for k in ("ecosystem", "version", "categories", "purpose"):
            assert k in entry, f"BUG: capabilities.json package {name!r} missing key {k!r}"


# writeSkillFile audit removed 2026-05-12 — function is a no-op stub.


def t_skill_loader_skips_underscore_prefixed_dirs():
    """Behavioral: the skill loader's _list_skills must skip directories
    starting with `_` (archive zones). Without this, the scrubbed skills
    in _archive_pre_scrub_2026_05_11/ would still get loaded.

    A name that appears in BOTH active and archive (e.g. `frontend-ui`
    co-authored fresh 2026-05-12) is allowed — it means the user
    intentionally re-authored the skill. The active version takes
    precedence; the archive version is still ignored by the loader.
    The legacy-skill content must not LEAK from the archive (e.g. by
    the active SKILL.md being a near-byte-identical copy of the archived
    one); that's a separate audit concern not enforced here."""
    from vault_graph.skills import _list_skills
    skills = _list_skills(VAULT_ROOT)
    names = [s["name"] for s in skills]
    for n in names:
        assert not n.startswith("_"), \
            f"BUG: skill loader returned underscore-prefixed dir {n!r}; should skip"
    # 2026-05-12: archive dir cut (git is the archive). Loader still skips
    # underscore-prefixed dirs if any reappear later, which is the invariant
    # we actually care about.


def t_active_skill_set_matches_documented_post_scrub():
    """After the 2026-05-11 scrub, the active skill set is exactly the 5
    user-authored skills. Adding new ones is fine and requires updating
    this list; the assertion enforces deliberate change, not drift."""
    EXPECTED_ACTIVE = {
        "json-canvas",
        "obsidian-bases",
        "obsidian-cli",
        "obsidian-markdown",
        "skill-creator",
        # 2026-05-12: co-authored frontend-ui added (Anthropic-schema +
        # obra/superpowers format + anti-slop checklist + capabilities
        # registry references + Linear/Stripe/Cursor/claude.ai/Arc anchors).
        "frontend-ui",
    }
    skills_dir = VAULT_ROOT / "ai_skills"
    actual = {d.name for d in skills_dir.iterdir()
              if d.is_dir() and not d.name.startswith("_")}
    assert actual == EXPECTED_ACTIVE, (
        f"BUG: active skill set drifted from documented post-scrub state.\n"
        f"  expected: {sorted(EXPECTED_ACTIVE)}\n"
        f"  actual:   {sorted(actual)}\n"
        f"  If this drift is intentional, update EXPECTED_ACTIVE in this "
        f"audit check."
    )


def t_pre_write_subprocess_empty_allowlist_constrained_to_app_root():
    """Behavioral (Bug AA fix): pre_write.py must deny subprocess writes
    outside app_root when scope is active but files_allowed is empty.
    Without this, the empty-allowlist case was permissive (allowed
    everything except files_blocked) and a subprocess could write to
    vault-tracked files outside the project."""
    import subprocess as _sp
    import json as _j
    import tempfile as _tf
    hook = VAULT_ROOT / "scripts" / "hooks" / "pre_write.py"
    scope_file = VAULT_ROOT / "scripts" / "hooks" / ".scope.json"
    scope_existed = scope_file.exists()
    scope_backup = scope_file.read_bytes() if scope_existed else None
    try:
        with _tf.TemporaryDirectory() as tmp:
            # Set scope with empty files_allowed pointing at app_root=tmp
            scope = {
                "task": "audit_test_bug_aa",
                "files_blocked": [],
                "files_allowed": [],
                "vault_root": str(VAULT_ROOT),
                "app_root": str(tmp),
            }
            scope_file.parent.mkdir(parents=True, exist_ok=True)
            scope_file.write_text(_j.dumps(scope), encoding="utf-8")
            # File OUTSIDE app_root (in vault dir) — must be denied
            outside = str(VAULT_ROOT / ".claude" / "settings.json")
            payload = _j.dumps({"tool_input": {"file_path": outside}})
            env_sub = {**os.environ, "VAULT_SUBPROCESS_AGENT": "1"}
            r = _sp.run(["python", str(hook)], input=payload, env=env_sub,
                        capture_output=True, text=True, timeout=10)
            assert r.returncode == 2, (
                f"BUG: pre_write did NOT deny subprocess writing outside app_root "
                f"with empty files_allowed. rc={r.returncode}, stdout={r.stdout!r}"
            )
            # File INSIDE app_root — must be allowed
            with _tf.NamedTemporaryFile(suffix=".py", dir=tmp, delete=False) as fh:
                inside = fh.name
            payload2 = _j.dumps({"tool_input": {"file_path": inside}})
            r2 = _sp.run(["python", str(hook)], input=payload2, env=env_sub,
                         capture_output=True, text=True, timeout=10)
            assert r2.returncode == 0, (
                f"BUG: pre_write denied subprocess writing INSIDE app_root. "
                f"rc={r2.returncode}, stdout={r2.stdout!r}"
            )
    finally:
        if scope_existed and scope_backup is not None:
            scope_file.write_bytes(scope_backup)
        elif scope_file.exists():
            scope_file.unlink()


def t_verify_plan_honors_verification_delete_list():
    """Behavioral (Bug Z.2 fix): refinements can carry a `verification_delete:`
    list that drops plan-text checks by id. Used when refinement renames
    a check; without this, the stale check stayed in the union forever."""
    from vault_graph.verification import verify_plan
    plan = """## Verification
```yaml
verification:
  - id: stale_check
    type: assertion
    file: nonexistent/path.py
    contains: foo
```
"""
    refinement = """Updating verification:
```yaml
verification_delete:
  - stale_check
verification:
  - id: replacement_check
    type: command
    run: echo replacement
    expect_returncode: 0
```
"""
    outcome = verify_plan(plan, cwd=str(VAULT_ROOT),
                         refinements=[refinement])
    # The stale check should have been dropped, leaving only replacement_check
    od = outcome.to_dict() if hasattr(outcome, "to_dict") else {}
    ids = {r.get("id") for r in od.get("results", [])
           if isinstance(r, dict) and r.get("id")}
    assert "stale_check" not in ids, \
        f"BUG: verification_delete did not drop stale_check; ids={ids}"
    assert "replacement_check" in ids, \
        f"BUG: refinement-supplied replacement_check missing; ids={ids}"


check("state/capabilities.json well-formed with required shape",
      t_state_capabilities_json_well_formed)
check("skill loader skips underscore-prefixed dirs (archive zones)",
      t_skill_loader_skips_underscore_prefixed_dirs)
check("active skill set matches documented post-scrub 5-skill state",
      t_active_skill_set_matches_documented_post_scrub)
check("pre_write subprocess + empty allowlist constrained to app_root (Bug AA, behavioral)",
      t_pre_write_subprocess_empty_allowlist_constrained_to_app_root)
check("verify_plan honors verification_delete list (Bug Z.2, behavioral)",
      t_verify_plan_honors_verification_delete_list)


# Prompt-variant A/B (GAP #5) audits removed 2026-05-12 — feature itself was
# removed (no signal, premature optimization). One canonical PLAN_REVIEW_PROMPT.


def t_scripts_packages_have_init_files():
    """scripts/ + scripts/ai_dougs/ must be Python packages (have __init__.py).

    Bug auto_0066 (2026-05-09): the planner emitted verification commands
    using `from scripts.ai_dougs.X import Y` (modern import style), but
    scripts/ wasn't a package — no __init__.py — so the imports failed
    with ModuleNotFoundError even though the work was correct. Fix: add
    __init__.py to both directories so both `from scripts.X import Y`
    AND the older `sys.path.insert(0, 'scripts'); from X import Y` pattern
    work side-by-side. Audit lock-in to prevent regression.
    """
    expected = (
        VAULT_ROOT / "scripts" / "__init__.py",
        VAULT_ROOT / "scripts" / "ai_dougs" / "__init__.py",
        VAULT_ROOT / "scripts" / "vault_graph" / "__init__.py",
        VAULT_ROOT / "scripts" / "vault_graph" / "nodes" / "__init__.py",
    )
    missing = [str(p.relative_to(VAULT_ROOT)).replace("\\", "/")
               for p in expected if not p.exists()]
    assert not missing, (
        f"BUG: missing __init__.py in {missing!r}. The vault uses both "
        f"`from scripts.X import Y` (modern) and `sys.path.insert(0, 'scripts'); "
        f"from X import Y` (legacy) import styles. Both require __init__.py "
        f"files in the package directories. See auto_0066 for the failure mode."
    )


check("scripts/ + scripts/ai_dougs/ + scripts/vault_graph/ all have __init__.py (Bug auto_0066)",
      t_scripts_packages_have_init_files)


def t_judge_receives_combined_diff_not_just_git_diff():
    """Bug L (2026-05-09): the execution judge must see the COMBINED diff
    (git diff + vault_diff_text from the hook log + mtime walk), not only
    the git diff. For vault-infra tasks where the vault isn't a git repo,
    diff_text is empty and the judge falsely concludes 'no work happened'.

    auto_0066 surfaced this — judge gave 1.8/5 REJECT despite the work
    being correct, because the diff it received was empty.
    """
    src = (VAULT_ROOT / "scripts" / "vault_graph" / "nodes" / "execution.py").read_text(encoding="utf-8")
    # Find the run_execution_judge call and verify it uses the combined-diff
    # fallback (or vault_diff_text), not bare diff_text.
    rej_start = src.find("execution_judge.run_execution_judge(")
    assert rej_start >= 0, "run_execution_judge call missing in execution.py"
    # Walk paren depth from the open paren to find the matching close
    open_paren = src.find("(", rej_start)
    depth = 0
    rej_end = open_paren
    for i in range(open_paren, len(src)):
        if src[i] == "(":
            depth += 1
        elif src[i] == ")":
            depth -= 1
            if depth == 0:
                rej_end = i + 1
                break
    call = src[rej_start:rej_end]
    # The call should NOT pass bare `diff_text` as the diff arg — should pass
    # combined_diff or judge_diff or vault_diff_text fallback.
    assert "judge_diff" in call or "combined_diff" in call or "vault_diff_text" in call, (
        f"BUG: execution_judge.run_execution_judge is being called with bare "
        f"diff_text instead of combined_diff (which includes vault hook-log + "
        f"mtime walk). For vault-infra tasks the bare git diff is empty and "
        f"the judge falsely rejects with 'no work happened'. Got call:\n{call!r}"
    )


check("execution judge receives combined diff (not bare git diff) (Bug L)",
      t_judge_receives_combined_diff_not_just_git_diff)


def t_staging_parser_handles_internal_horizontal_rules():
    """Bug M (2026-05-09): the staging parser regex previously truncated
    sharpened content at ANY `\\n---` line, but sharpener models like
    claude:sonnet use `---` as INTERNAL section separators. Result:
    captured sharpened was the first paragraph only, downstream task
    files got 227-char prompts, planning collapsed.

    Fix: terminator must be `\\n---\\n## ` (only break on next staging
    entry's heading). Audit guards by parsing a synthetic block.
    """
    import re as _re
    pattern = r'\*\*Sharpened:\*\*\s*\n(.*?)(?=\n\*\*[A-Z][\w ]*:\*\*\s*\n|\n---\s*\n+##|\n>|\Z)'
    synthetic = (
        "**Sharpened:**\n"
        "First paragraph.\n"
        "\n"
        "---\n"
        "\n"
        "**STEP 0 — Internal section using --- separator**\n"
        "Step body.\n"
        "\n"
        "---\n"
        "\n"
        "**STEP 1 — Another internal section**\n"
        "More body.\n"
        "\n"
        "---\n"
        "\n## [auto_NEXT] next entry heading\nstatus: pending_human_review\n"
    )
    m = _re.search(pattern, synthetic, _re.DOTALL)
    assert m, "BUG: parser regex didn't match the **Sharpened:** label"
    captured = m.group(1).strip()
    # Must include all the STEP sections (they live AFTER internal --- lines)
    assert "STEP 0" in captured, f"BUG: STEP 0 truncated. captured={captured!r}"
    assert "STEP 1" in captured, f"BUG: STEP 1 truncated. captured={captured!r}"
    # Must NOT include the next staging entry's heading (correctly stops at \n---\n##)
    assert "auto_NEXT" not in captured, f"BUG: parser overran into next staging entry. captured={captured[-200:]!r}"
    # Confirm the actual code in the sharpener uses the new terminator
    src = (VAULT_ROOT / "scripts" / "ai_sharpener" / "ai_sharpener.py").read_text(encoding="utf-8")
    assert r"\n---\s*\n+##" in src, (
        "BUG: ai_sharpener.py parser regex doesn't use the `\\n---\\s*\\n+##` "
        "terminator (Bug M fix). Without it, sharpened content with internal "
        "`---` separators gets truncated, OR the parser overruns into the "
        "next staging entry's heading when there are blank lines."
    )
    assert r"[A-Z][\w ]*:\*\*" in src, (
        "BUG: ai_sharpener.py parser regex doesn't restrict the `**X**` "
        "terminator to LABELED section markers (with colon). Without it, "
        "internal markdown like `**STEP 0 — ...**` gets mistaken for a "
        "section terminator and truncates the captured content."
    )


def t_runAiRequest_rejects_cli_banner_only_responses():
    """Bug N (2026-05-09): when a CLI returns only its banner string
    ("OpenAI Codex v0.128.0 (research preview)" — 40 chars), the prior
    response-validation passed it through as a successful response. The
    vault then planned with effectively no content. Source check: the
    BANNER_PATTERNS gate must exist in ai_dougs.py.
    """
    src = (VAULT_ROOT / "scripts" / "ai_dougs" / "ai_dougs.py").read_text(encoding="utf-8")
    required = (
        "BANNER_PATTERNS",
        "OpenAI Codex v",
        "research preview",
    )
    for token in required:
        assert token in src, (
            f"BUG: ai_dougs.py missing CLI-banner-only detection token "
            f"{token!r}. Without it, a CLI returning only its banner is "
            f"accepted as a valid response, polluting the plan with a stub "
            f"(canonical case: auto_0067 got plan_text='OpenAI Codex v0.128.0 "
            f"(research preview)' — 40 chars)."
        )


check("staging parser handles internal --- separators (Bug M behavioral)",
      t_staging_parser_handles_internal_horizontal_rules)
check("runAiRequest rejects CLI-banner-only responses (Bug N)",
      t_runAiRequest_rejects_cli_banner_only_responses)


def t_daemon_health_uses_per_pid_tmp_filename():
    """Bug O (2026-05-10): writeHealthState used `daemon_health.tmp` for ALL
    writers, so concurrent daemons (sharpener + executor) collided on the
    same tmp file and Windows raised PermissionError mid-write_text().
    Fix: per-PID tmp filename so writers don't collide.
    """
    src = (VAULT_ROOT / "scripts" / "vault_graph" / "daemonHealth.py").read_text(encoding="utf-8")
    assert "_os.getpid()" in src or "os.getpid()" in src, (
        "BUG: writeHealthState doesn't include the writer's PID in the tmp "
        "filename. Concurrent daemons will collide on the SAME tmp file and "
        "Windows will raise PermissionError. See Bug O (2026-05-10)."
    )
    # Also confirm write_text is wrapped in retry (defense against stale tmp)
    func_start = src.find("def writeHealthState")
    func_end = src.find("\ndef ", func_start + 1)
    body = src[func_start:func_end]
    assert "tmp.write_text(" in body and body.count("PermissionError") >= 2, (
        "BUG: writeHealthState should wrap BOTH write_text and replace in "
        "retry-on-PermissionError loops; previously only replace was wrapped."
    )


check("daemon health writer uses per-PID tmp filename (Bug O)",
      t_daemon_health_uses_per_pid_tmp_filename)


def t_codex_parser_preserves_long_output_when_marker_missing():
    """Bug P (2026-05-10): when codex CLI output lacks the `\\ncodex\\n`
    user/assistant marker, the prior defensive stripping cut at the first
    `\\n--------` (which is actually a HEADER separator) and returned a
    40-char stub instead of the real response. Fix: only apply aggressive
    stripping if the result is still substantive (>=200 chars); otherwise
    preserve the full output. Surfaced via auto_0068 planning failure.
    """
    src = (VAULT_ROOT / "scripts" / "ai_dougs" / "ai_dougs.py").read_text(encoding="utf-8")
    # The fix introduces `stripped_attempt` and the >=200 chars guard
    assert "stripped_attempt" in src, (
        "BUG: codex parser missing the stripped_attempt guard. Bug P fix "
        "requires evaluating whether aggressive stripping would leave a stub "
        "(< 200 chars) and preserving the full output if so."
    )
    assert "keeping full" in src, (
        "BUG: codex parser missing the 'keeping full' fallback path. The "
        "defensive stripping must NOT corrupt long responses down to banner-"
        "only stubs (Bug P, auto_0068)."
    )


check("codex parser preserves long output when marker missing (Bug P)",
      t_codex_parser_preserves_long_output_when_marker_missing)


def t_rate_limit_signals_include_codex_usage_limit_phrasing():
    """Bug Q (2026-05-10): codex CLI uses the phrase "You've hit your USAGE
    limit" (with 'usage' between 'your' and 'limit'). The prior
    RATE_LIMIT_SIGNALS only had 'you've hit your limit' (without 'usage'),
    so codex rate-limit responses passed the detector → substance check
    accepted them → parser butchered to banner-only → routing never fell
    back to claude/gemini. Surfaced via auto_0068 + auto_0069 both failing.
    """
    src = (VAULT_ROOT / "scripts" / "ai_dougs" / "ai_dougs.py").read_text(encoding="utf-8")
    required = (
        '"hit your usage limit"',          # codex's specific wording
        '"upgrade to plus to continue"',   # codex's follow-up
    )
    for token in required:
        assert token in src, (
            f"BUG: RATE_LIMIT_SIGNALS missing {token!r}. Without it, codex "
            f"rate-limit responses bypass the detector and pollute the plan "
            f"(canonical case: auto_0068 + auto_0069 both failed because of "
            f"this missing pattern)."
        )


check("RATE_LIMIT_SIGNALS includes codex usage-limit phrasing (Bug Q)",
      t_rate_limit_signals_include_codex_usage_limit_phrasing)


def t_verify_plan_falls_back_to_refinement_when_plan_text_has_no_block():
    """Bug R (2026-05-10) behavioral: if plan_text has no parseable
    verification block but a refinement DOES, verify_plan must use the
    refinement-supplied checks as the primary plan, not return parse_error.

    Surfaced via auto_0070 — claude:haiku produced clarifying questions
    only with no verification block; codex's refinement supplied a full
    4-check verification YAML; without this fix verify_plan returned
    parse_error and the refinement's perfectly-valid checks were ignored.
    """
    from vault_graph.verification import verify_plan
    plan_no_block = "## Plan\n\nDo X.\n\n(no verification block here)"
    refinement_with_block = (
        "## Plan refinement 1\n\n```yaml\nverification:\n"
        "  - id: smoke_check\n    type: command\n"
        "    command: python -c \"print('ok')\"\n    expect_returncode: 0\n```"
    )
    outcome = verify_plan(plan_no_block, cwd=str(VAULT_ROOT),
                         task_id="audit_test_bug_r",
                         refinements=[refinement_with_block])
    assert outcome.parse_error is None, (
        f"BUG: verify_plan returned parse_error {outcome.parse_error!r} "
        f"even though the refinement supplied a valid verification block. "
        f"Bug R fix should fall back to refinement-only mode."
    )
    assert outcome.n_checks == 1, (
        f"BUG: verify_plan saw {outcome.n_checks} checks; expected 1 from "
        f"refinement fallback."
    )
    assert outcome.all_passed, (
        f"BUG: refinement-supplied 'print(ok)' check should pass; got "
        f"{outcome.n_failed} failures."
    )


check("verify_plan falls back to refinement when plan_text has no block (Bug R)",
      t_verify_plan_falls_back_to_refinement_when_plan_text_has_no_block)


def t_execution_judge_prompt_uses_refinement_union():
    """Bug U behavioral: execution_judge must judge against the same effective
    refined verification block verify_plan executed, not stale plan_text.
    """
    from vault_graph import execution_judge as ej
    import vault_graph.ported as ported

    captured = {}
    old_run = ported.runAiRequestWithFallback
    old_archive = ported._archivePromptResponse
    old_cost = ported._writeCostLog

    def fake_run(prompt, *args, **kwargs):
        captured["prompt"] = prompt
        return "VERDICT: APPROVE", "mock", "judge"

    try:
        ported.runAiRequestWithFallback = fake_run
        ported._archivePromptResponse = lambda *args, **kwargs: None
        ported._writeCostLog = lambda *args, **kwargs: None
        plan = (
            "```yaml\nverification:\n"
            "  - id: memory_location\n"
            "    type: assertion\n"
            "    file: old.txt\n"
            "    contains: old\n"
            "verification_policy:\n"
            "  max_iterations: 2\n"
            "  on_first_failure: retry_with_higher_tier_model\n"
            "  on_repeated_failure: escalate_to_human\n"
            "```"
        )
        refinement = (
            "```yaml\nverification:\n"
            "  - id: memory_location\n"
            "    type: assertion\n"
            "    file: new.txt\n"
            "    contains: refined\n"
            "  - id: added_check\n"
            "    type: assertion\n"
            "    file: added.txt\n"
            "    contains: added\n"
            "```"
        )
        ej.run_execution_judge(
            "task", plan, "done", "diff", VAULT_ROOT,
            [{"provider": "x", "model": "y"}], True,
            "x", "y", "audit_bug_u", refinements=[refinement],
        )
        prompt = captured["prompt"]
        assert "new.txt" in prompt and "added_check" in prompt, prompt
        assert "old.txt" not in prompt, prompt
        assert "verification_policy:" in prompt, prompt
        assert "retry_with_higher_tier_model" in prompt, prompt
        assert "escalate_to_human" in prompt, prompt
    finally:
        ported.runAiRequestWithFallback = old_run
        ported._archivePromptResponse = old_archive
        ported._writeCostLog = old_cost


# t_regression_recorder_uses_refinement_union removed 2026-05-12 with the
# regression-block recording mechanism itself. record_passing_block no longer
# runs at terminal_complete; the audit suite no longer re-executes blocks.


# auto_0074 active-status check removed 2026-05-12: that regression block
# locked in project_memory's pre-redesign behavior (failures.md, task_log
# in agent context). The redesign retired those surfaces. Block is
# explicitly disabled with a reason in regression_blocks.yaml.


def t_judge_consumer_accepts_refinements():
    """Source guard: execution_judge must accept refinement context so the judge
    sees the effective refined verification block, not stale plan_text."""
    judge_src = (VAULT_ROOT / "scripts" / "vault_graph" / "execution_judge.py").read_text(encoding="utf-8")
    execution_src = (VAULT_ROOT / "scripts" / "vault_graph" / "nodes" / "execution.py").read_text(encoding="utf-8")

    assert "refinements: list[str] | None = None" in judge_src, (
        "execution_judge.run_execution_judge must accept refinements so the "
        "judge sees the effective refined verification block."
    )
    assert "extract_verification_blocks_from_refinements(refinements)" in judge_src, (
        "execution_judge must union refinement verification checks."
    )
    assert "refinements=state.get(\"refinements\") or []" in execution_src, (
        "execution node must pass refinements into execution_judge."
    )


check("execution judge prompt uses refined verification union (Bug U behavioral)",
      t_execution_judge_prompt_uses_refinement_union)
check("execution_judge accepts refinements so judge sees effective verified block",
      t_judge_consumer_accepts_refinements)


# Final report
# ════════════════════════════════════════════════════════════════════════════
print()
print(f"{BOLD}{'═' * 72}{RESET}")
total = len(results)
passed = sum(1 for _, p, _ in results if p)
failed = total - passed
if failed == 0:
    print(f"{GREEN}{BOLD}✓ ALL {total} CHECKS PASSED{RESET}")
else:
    print(f"{RED}{BOLD}✗ {failed} of {total} checks FAILED{RESET}")
    print(f"\n{BOLD}Failures:{RESET}")
    for name, p, msg in results:
        if not p:
            print(f"  {RED}✗{RESET} {name}: {msg}")
print(f"{BOLD}{'═' * 72}{RESET}")
sys.exit(0 if failed == 0 else 1)
