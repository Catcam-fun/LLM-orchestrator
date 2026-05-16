#!/usr/bin/env python3
"""
vault_smoke_test.py - Self-test smoke check for vault scripts.

Run this after modifying any vault script (ai_dougs.py, ai_sharpener.py,
run_vault.py, vault.py, vault_graph/*) to catch breaking changes before they
cause runtime failures.

Checks:
1. Every .py in scripts/ parses (syntax)
2. Every script imports without error (catches missing imports, name errors)
3. logs/routing_params.json is valid JSON if present
4. logs/cost_log.jsonl every line is valid JSON if present
5. ai_main.md frontmatter is parseable
6. Required vault folders exist (ai_context/, ai_skills/, task_files/)

Exit code 0 = all checks passed. Non-zero = at least one failure.

Usage:
    python scripts/vault_smoke_test.py
    python scripts/vault_smoke_test.py --quiet     # only show failures
"""
import ast
import contextlib
import io
import json
import sys
from types import SimpleNamespace
from pathlib import Path

# Force UTF-8 on Windows so check marks render correctly
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

SCRIPT_VERSION = "1.4"  # 2026-05-10 - make cost log smoke check rotation-aware

# ANSI colors (works on most terminals; ignored if redirected)
RESET  = "\033[0m"
GREEN  = "\033[32m"
RED    = "\033[31m"
YELLOW = "\033[33m"
DIM    = "\033[2m"
BOLD   = "\033[1m"


def _vault_root():
    """Resolve the vault root from this script's location."""
    return Path(__file__).resolve().parent.parent


def check_python_syntax(scripts_dir, quiet=False):
    """Check every .py file in scripts/ parses without syntax errors."""
    failures = []
    py_files = list(scripts_dir.rglob("*.py"))
    for py in py_files:
        try:
            source = py.read_text(encoding="utf-8")
            ast.parse(source)
        except SyntaxError as e:
            failures.append(f"{py.relative_to(scripts_dir.parent)}: {e}")
        except Exception as e:
            failures.append(f"{py.relative_to(scripts_dir.parent)}: {type(e).__name__}: {e}")

    if not quiet:
        status = f"{GREEN}✓{RESET}" if not failures else f"{RED}✗{RESET}"
        print(f"  {status} Python syntax  {DIM}({len(py_files)} files){RESET}")
    return failures


def check_routing_params(vault_root, quiet=False):
    """Validate logs/routing_params.json is parseable JSON if present."""
    fp = vault_root / "logs" / "routing_params.json"
    if not fp.exists():
        if not quiet:
            print(f"  {DIM}- routing_params.json (not present, skipped){RESET}")
        return []
    try:
        json.loads(fp.read_text(encoding="utf-8"))
        if not quiet:
            print(f"  {GREEN}✓{RESET} routing_params.json")
        return []
    except json.JSONDecodeError as e:
        msg = f"routing_params.json: invalid JSON at line {e.lineno}: {e.msg}"
        if not quiet:
            print(f"  {RED}✗{RESET} {msg}")
        return [msg]


def check_cost_log(vault_root, quiet=False):
    """Validate every line in cost_log.jsonl is valid JSON."""
    fp = vault_root / "logs" / "cost_log.jsonl"
    if not fp.exists():
        if not quiet:
            print(f"  {DIM}- cost_log.jsonl (not present, skipped){RESET}")
        return []
    failures = []
    line_count = 0
    with open(fp, "r", encoding="utf-8") as f:
        for i, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            line_count += 1
            try:
                json.loads(line)
            except json.JSONDecodeError as e:
                failures.append(f"cost_log.jsonl line {i}: {e.msg}")
                if len(failures) >= 5:
                    failures.append(f"... and more (truncated)")
                    break
    max_mb = 5
    params_file = vault_root / "logs" / "routing_params.json"
    if params_file.exists():
        try:
            params = json.loads(params_file.read_text(encoding="utf-8"))
            max_mb = params.get("cost_log_max_mb", max_mb)
        except json.JSONDecodeError:
            pass
    max_bytes = int(float(max_mb) * 1024 * 1024)
    if fp.stat().st_size > max_bytes:
        failures.append(f"cost_log.jsonl exceeds configured rotation threshold ({max_mb} MB)")
    if not quiet:
        status = f"{GREEN}✓{RESET}" if not failures else f"{RED}✗{RESET}"
        print(f"  {status} cost_log.jsonl  {DIM}({line_count} entries){RESET}")
    return failures


def check_ai_main(vault_root, quiet=False):
    """ai_main.md must exist and have parseable frontmatter."""
    fp = vault_root / "ai_main.md"
    if not fp.exists():
        msg = "ai_main.md missing — agents have no constitution"
        if not quiet:
            print(f"  {RED}✗{RESET} {msg}")
        return [msg]
    try:
        content = fp.read_text(encoding="utf-8")
        # Just check it has at least one section
        if "##" not in content:
            msg = "ai_main.md has no sections"
            if not quiet:
                print(f"  {YELLOW}⚠{RESET} {msg}")
            return [msg]
    except Exception as e:
        msg = f"ai_main.md unreadable: {e}"
        if not quiet:
            print(f"  {RED}✗{RESET} {msg}")
        return [msg]
    if not quiet:
        print(f"  {GREEN}✓{RESET} ai_main.md")
    return []


def check_required_folders(vault_root, quiet=False):
    """Required vault folders must exist."""
    required = ["ai_context", "ai_skills", "task_files", "scripts", "logs"]
    failures = []
    for name in required:
        path = vault_root / name
        if not path.exists():
            failures.append(f"missing folder: {name}/")
    if not quiet:
        status = f"{GREEN}✓{RESET}" if not failures else f"{RED}✗{RESET}"
        print(f"  {status} Required folders  {DIM}({len(required)}){RESET}")
    return failures


def check_imports(scripts_dir, quiet=False):
    """Try to compile (not execute) each top-level script.

    Compiling catches import-style errors and indentation errors that ast.parse
    misses. We don't actually exec because that would run the scripts.
    """
    failures = []
    top_level = ["ai_dougs/ai_dougs.py", "ai_sharpener/ai_sharpener.py"]
    for rel in top_level:
        path = scripts_dir / rel
        if not path.exists():
            continue
        try:
            source = path.read_text(encoding="utf-8")
            compile(source, str(path), "exec")
        except SyntaxError as e:
            failures.append(f"{rel}: {e}")
        except Exception as e:
            failures.append(f"{rel}: {type(e).__name__}: {e}")
    if not quiet:
        status = f"{GREEN}✓{RESET}" if not failures else f"{RED}✗{RESET}"
        print(f"  {status} Compile check  {DIM}(top-level scripts){RESET}")
    return failures


def check_vault_backup_helper(vault_root, quiet=False):
    """Verify the execution backup helper exists and exposes its entry point."""
    backup_path = vault_root / "scripts" / "vault_graph" / "vault_backup.py"
    failures = []
    if not backup_path.exists():
        failures.append("scripts/vault_graph/vault_backup.py missing")
    else:
        try:
            sys.path.insert(0, str(vault_root / "scripts"))
            from vault_graph import vault_backup
            if not callable(getattr(vault_backup, "create_backup", None)):
                failures.append("vault_backup.create_backup is not callable")
        except Exception as e:
            failures.append(f"vault_backup import failed: {type(e).__name__}: {e}")
        finally:
            try:
                sys.path.remove(str(vault_root / "scripts"))
            except ValueError:
                pass

    if not quiet:
        status = f"{GREEN}✓{RESET}" if not failures else f"{RED}✗{RESET}"
        print(f"  {status} Vault backup helper")
    return failures


def check_models_command_handler(vault_root, quiet=False):
    """Import and invoke the models CLI handler without spawning a subprocess."""
    failures = []
    try:
        sys.path.insert(0, str(vault_root / "scripts"))
        from vault_graph import cli
        if not callable(getattr(cli, "cmd_models", None)):
            failures.append("vault_graph.cli.cmd_models is not callable")
        else:
            providers = cli._load_model_pool_providers()
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = cli.cmd_models(SimpleNamespace())
            text = output.getvalue()
            if result != 0:
                failures.append(f"cmd_models returned {result}")
            if not text.strip():
                failures.append("cmd_models printed no output")
            if providers and not any(provider in text for provider in providers):
                failures.append("cmd_models output mentions no provider from model_pool.json")
    except Exception as e:
        failures.append(f"cmd_models smoke failed: {type(e).__name__}: {e}")
    finally:
        try:
            sys.path.remove(str(vault_root / "scripts"))
        except ValueError:
            pass

    if not quiet:
        status = f"{GREEN}âœ“{RESET}" if not failures else f"{RED}âœ—{RESET}"
        print(f"  {status} Models command handler")
    return failures


def check_prune_skills_command(vault_root, quiet=False):
    """Import and invoke prune-skills dry-run without archiving anything."""
    failures = []
    try:
        sys.path.insert(0, str(vault_root / "scripts"))
        from vault_graph import cli
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = cli.main(["prune-skills", "--dry-run"])
        text = output.getvalue()
        if result != 0:
            failures.append(f"prune-skills --dry-run returned {result}")
        # 2026-05-11: previously asserted "cli-patterns" specifically, but that
        # skill was archived in the scrub. Now assert the command surfaces
        # at least one active skill from ai_skills/ (ignoring _* archive dirs).
        import os as _os
        skills_dir = vault_root / "ai_skills"
        active = [d for d in _os.listdir(skills_dir)
                  if not d.startswith("_") and (skills_dir / d).is_dir()]
        if active and not any(s in text for s in active):
            failures.append(
                f"prune-skills output mentioned none of the active skills "
                f"{active}; got: {text[:200]!r}")
    except Exception as e:
        failures.append(f"prune-skills smoke failed: {type(e).__name__}: {e}")
    finally:
        try:
            sys.path.remove(str(vault_root / "scripts"))
        except ValueError:
            pass

    if not quiet:
        status = f"{GREEN}OK{RESET}" if not failures else f"{RED}FAIL{RESET}"
        print(f"  {status} prune-skills dry-run")
    return failures


def main(argv):
    quiet = "--quiet" in argv or "-q" in argv

    vault_root = _vault_root()
    scripts_dir = vault_root / "scripts"

    if not scripts_dir.exists():
        print(f"{RED}Cannot find scripts/ folder. Are you running from the vault?{RESET}")
        return 2

    print(f"{BOLD}vault_smoke_test {SCRIPT_VERSION}{RESET}  {DIM}{vault_root}{RESET}")
    print()

    all_failures = []
    all_failures += check_python_syntax(scripts_dir, quiet)
    all_failures += check_imports(scripts_dir, quiet)
    all_failures += check_routing_params(vault_root, quiet)
    all_failures += check_cost_log(vault_root, quiet)
    all_failures += check_ai_main(vault_root, quiet)
    all_failures += check_required_folders(vault_root, quiet)
    all_failures += check_vault_backup_helper(vault_root, quiet)
    all_failures += check_models_command_handler(vault_root, quiet)
    all_failures += check_prune_skills_command(vault_root, quiet)

    print()
    if not all_failures:
        print(f"{GREEN}{BOLD}✓ All checks passed.{RESET}")
        return 0
    else:
        print(f"{RED}{BOLD}✗ {len(all_failures)} failure(s):{RESET}")
        for f in all_failures:
            print(f"  {RED}•{RESET} {f}")
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
