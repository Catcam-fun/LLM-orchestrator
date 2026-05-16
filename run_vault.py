#!/usr/bin/env python3
"""
run_vault.py - Vault agent launcher.

Auto-discovers all ai_*.py scripts under scripts/ and runs them together
in one terminal with labeled, colored output per agent.

Usage: python run_vault.py
"""

import json
import os
import subprocess
import sys
import threading
import time
from datetime import date
from pathlib import Path

# Force UTF-8 output on Windows (fixes cp1252 UnicodeEncodeError)
if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

SCRIPT_VERSION = "1.6"  # 2026-05-04 - Removed ai_runner (unused); launches SHAR + DOUG only

RESET  = "\033[0m"
BOLD   = "\033[1m"
DIM    = "\033[2m"
WHITE  = "\033[97m"

# Colors cycled across discovered agents
_COLORS = [
    "\033[36m",   # cyan
    "\033[32m",   # green
    "\033[33m",   # yellow
    "\033[35m",   # magenta
    "\033[34m",   # blue
    "\033[31m",   # red
    "\033[96m",   # bright cyan
    "\033[92m",   # bright green
]

W = 60
_print_lock = threading.Lock()


def findVaultRoot():
    for p in [Path.cwd(), *Path.cwd().parents]:
        if (p / "ai_main.md").exists():
            return p
    raise FileNotFoundError("Vault root not found — ai_main.md missing.")


def discoverAgents(vault_root):
    """
    Returns the agents to launch in parallel:
      - SHAR: ai_sharpener.py  (refines brainstorm prompts → staging → routes to dougs)
      - DOUG: vault.py daemon   (LangGraph backend; replaces old ai_dougs.py)

    The old ai_runner is intentionally not present — every approved prompt now
    routes through dougs so it gets the full pipeline (cost ceiling, scope
    hooks, build gate, human approval). The runner's "single-shot for simple
    prompts" use case wasn't being used in practice.

    Old ai_dougs/ai_dougs.py stays on disk during the migration as a source of
    helpers imported by vault_graph/ported.py; it'll be removed after the new
    system is proven on real tasks.
    """
    scripts_dir = vault_root / "scripts"
    agents = []

    # Sharpener — standalone polling daemon (passive markdown watcher)
    sharpener = scripts_dir / "ai_sharpener" / "ai_sharpener.py"
    if sharpener.exists():
        agents.append({
            "label": "SHAR",
            "color": _COLORS[len(agents) % len(_COLORS)],
            "cmd": [sys.executable, "-u", str(sharpener)],
            "path": sharpener,
        })

    # Dougs replacement — vault.py daemon (LangGraph state machine)
    vault_cli = vault_root / "vault.py"
    if vault_cli.exists():
        agents.append({
            "label": "DOUG",
            "color": _COLORS[len(agents) % len(_COLORS)],
            "cmd": [sys.executable, "-u", str(vault_cli), "daemon"],
            "path": vault_cli,
        })

    return agents


def streamOutput(proc, label, color):
    prefix = f"{color}{BOLD}[{label}]{RESET} "
    try:
        for raw in proc.stdout:
            with _print_lock:
                print(f"{prefix}{raw}", end="", flush=True)
    except Exception:
        pass


def launch(agent, vault_root):
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"  # flush every print immediately through the pipe
    # Each agent supplies its own command list (cmd) so DOUG can use subcommands
    # like `vault.py daemon` while SHAR/RUNN are direct script invocations
    cmd = agent.get("cmd") or [sys.executable, "-u", str(agent["path"])]
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        cwd=str(vault_root),
        env=env,
    )
    threading.Thread(
        target=streamOutput,
        args=(proc, agent["label"], agent["color"]),
        daemon=True,
    ).start()
    return proc


def _lastProbeDate(vault_root):
    """Return the most recent date a model probe ran (YYYY-MM-DD), or None."""
    log_file = vault_root / "logs" / "cost_log.jsonl"
    if not log_file.exists():
        return None
    last = None
    try:
        with open(log_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                    if e.get("phase") == "probe":
                        d = e.get("date", "")
                        if d and (last is None or d > last):
                            last = d
                except json.JSONDecodeError:
                    continue
    except Exception:
        return None
    return last


def maybeRunDailyProbe(vault_root):
    """Run probe_models.py --check-new if no check has happened today.

    --check-new is metadata-only: queries each provider's /v1/models endpoint
    to see if new models have been released. Zero LLM calls, no quota cost,
    completes in 2-5 seconds. Just reports new models for the human to add.

    For full validation (actually testing each model with a prompt), the human
    runs `python scripts/probe_models.py --discover` manually when they suspect
    something is broken.
    """
    probe_script = vault_root / "scripts" / "probe_models.py"
    if not probe_script.exists():
        return

    last = _lastProbeDate(vault_root)
    today = date.today().isoformat()
    if last == today:
        print(f"  {DIM}Daily check-new already ran today ({last}) — skipping.{RESET}")
        return

    label = "never run" if not last else f"last run {last}"
    print(f"  {DIM}Checking provider APIs for new models ({label})...{RESET}")
    try:
        subprocess.run(
            [sys.executable, "-u", str(probe_script), "--check-new"],
            cwd=str(vault_root),
            timeout=60,
            env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"},
        )
    except subprocess.TimeoutExpired:
        print(f"  {DIM}(check-new exceeded 60s timeout — continuing){RESET}")
    except Exception as e:
        print(f"  {DIM}(check-new skipped: {e}){RESET}")


def main():
    vault_root = findVaultRoot()
    agents     = discoverAgents(vault_root)

    if not agents:
        print(f"No agent scripts found under {vault_root / 'scripts'}")
        sys.exit(1)

    print(f"\n{BOLD}{WHITE}{'═' * W}{RESET}")
    print(f"{BOLD}{WHITE}  VAULT  v{SCRIPT_VERSION}{RESET}  {DIM}{vault_root}{RESET}")
    print(f"{BOLD}{WHITE}{'═' * W}{RESET}")

    # Daily-once: validate model pool + discover new models before agents launch
    maybeRunDailyProbe(vault_root)

    procs = []
    for agent in agents:
        proc = launch(agent, vault_root)
        procs.append((agent, proc))
        rel = agent["path"].relative_to(vault_root)
        print(f"  {agent['color']}{BOLD}[{agent['label']}]{RESET}  {DIM}{rel}{RESET}")

    print(f"\n{DIM}  Ctrl+C to stop all agents.{RESET}")
    print(f"{DIM}{'─' * W}{RESET}\n")

    try:
        while True:
            for i, (agent, proc) in enumerate(procs):
                if proc.poll() is not None:
                    with _print_lock:
                        print(f"\n{agent['color']}[{agent['label']}]{RESET} exited — restarting...")
                    new_proc = launch(agent, vault_root)
                    procs[i] = (agent, new_proc)
            time.sleep(5)
    except KeyboardInterrupt:
        print(f"\n{DIM}Stopping agents...{RESET}")
        for agent, proc in procs:
            proc.terminate()
        for agent, proc in procs:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

        # Auto-generate today's session digest on clean exit — institutional memory
        digest_script = vault_root / "scripts" / "session_digest.py"
        if digest_script.exists():
            print(f"{DIM}Compiling session digest...{RESET}")
            try:
                subprocess.run(
                    [sys.executable, str(digest_script)],
                    cwd=str(vault_root),
                    timeout=15,
                    env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                )
            except Exception as e:
                print(f"{DIM}(digest skipped: {e}){RESET}")

        print(f"{DIM}Done.{RESET}\n")
        sys.exit(0)


if __name__ == "__main__":
    main()
