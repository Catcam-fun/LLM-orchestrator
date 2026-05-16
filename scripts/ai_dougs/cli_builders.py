"""Provider CLI command builders for ai_dougs."""

import shutil
import sys
from pathlib import Path


def resolveCmd(name):
    if sys.platform == "win32":
        found = shutil.which(name + ".cmd")
        if found:
            return found
    found = shutil.which(name)
    return found or name


def build_claude_cmd(model, agent_mode, cwd):
    cmd = [resolveCmd("claude"), "-p"]
    if agent_mode:
        cmd.append("--dangerously-skip-permissions")
    if model:
        cmd += ["--model", model]
    return cmd


def build_codex_cmd(model, agent_mode, cwd):
    return [resolveCmd("codex"), "exec", "--dangerously-bypass-approvals-and-sandbox", "-"]


def build_gemini_cmd(model, agent_mode, cwd):
    cmd = [resolveCmd("gemini"), "--yolo", "-p"]
    if model:
        cmd += ["--model", model]
    return cmd


def build_agent_cmd(model, agent_mode, cwd):
    cmd = [resolveCmd("agent"), "-p", "--force", "--trust"]
    if agent_mode:
        cmd += ["--workspace", str(cwd)]
    if model:
        cmd += ["--model", model]
    return cmd


def build_cli_command(provider, model, agent_mode, cwd):
    if provider == "claude":
        return build_claude_cmd(model, agent_mode, cwd)
    if provider == "codex":
        return build_codex_cmd(model, agent_mode, cwd)
    if provider == "gemini":
        return build_gemini_cmd(model, agent_mode, cwd)
    if provider in ("agent", "cursor"):
        return build_agent_cmd(model, agent_mode, cwd)
    return []


def add_claude_directories(cmd, addDirs):
    for directory in addDirs:
        if directory and Path(directory).exists():
            cmd += ["--add-dir", str(directory)]
    return cmd


def add_gemini_prompt_argument(cmd, prompt):
    prompt_index = cmd.index("-p") + 1
    cmd.insert(prompt_index, prompt)
    return cmd


def build_cli_command_for_prompt(provider, model, agent_mode, cwd, prompt=None, addDirs=None):
    command_agent_mode = agent_mode or provider in ("agent", "cursor")
    cmd = build_cli_command(provider, model, command_agent_mode, cwd)
    if not cmd:
        return [], True
    if provider == "claude" and agent_mode:
        cmd = add_claude_directories(cmd, addDirs or [])
    if provider == "gemini" and prompt is not None:
        cmd = add_gemini_prompt_argument(cmd, prompt)
        return cmd, False
    return cmd, True
