"""verification.py — closes the plan->execute->measure->learn loop.

The vault has historically had a broken step 3 of the learning loop: tasks
got marked `completed` based on "didn't crash", not "feature actually works
as the plan promised". This module makes step 3 real:

1. The planning phase is required to emit a `verification:` YAML block
   alongside the plan, with one or more concrete checks (CLI command,
   file assertion, test suite, screenshot diff).
2. After execution, `run_verification_block()` is called by self-test.
   It parses the block, runs each check, captures pass/fail.
3. Results are recorded with `failure-loud, success-quiet` discipline:
   passes print one line, failures print full output in red.
4. Completion is gated on every check passing. Failed verification writes
   a structured failure_modes entry that REQUIRES a constraint_added or
   no_constraint_possible_because field (per the iteration discipline
   from harness research 2026-05-07).

Schema (parsed from the planning output's verification: block):

    verification:
      - id: <string identifier>
        type: command | assertion | test_suite | screenshot_compare
        # type-specific fields:
        #   command:     run, expect_returncode (default 0),
        #                expect_stdout_contains (list of strings)
        #   assertion:   file (path), contains (string) | not_contains (string)
        #   test_suite:  run, expect_returncode (default 0)
        #   screenshot_compare: reference (path), output (path),
        #                       max_diff_ratio (float, default 0.10)
    verification_policy:
      max_iterations: 2                 # default 2
      on_first_failure: retry_with_higher_tier_model | escalate
      on_repeated_failure: escalate_to_human   # default
"""
from __future__ import annotations
import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any


# ── ANSI colors (failure-loud, success-quiet) ─────────────────────────────
GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"


# ── Result types ──────────────────────────────────────────────────────────

@dataclass
class CheckResult:
    """Outcome of a single verification check."""
    id: str
    type: str
    passed: bool
    duration_sec: float
    detail: str = ""
    output: str = ""
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class VerificationOutcome:
    """Aggregate outcome of a full verification block run."""
    all_passed: bool
    n_checks: int
    n_passed: int
    n_failed: int
    results: list[CheckResult] = field(default_factory=list)
    parse_error: str | None = None

    @property
    def failed(self) -> list[CheckResult]:
        return [r for r in self.results if not r.passed]

    def to_dict(self) -> dict:
        return {
            "all_passed": self.all_passed,
            "n_checks": self.n_checks,
            "n_passed": self.n_passed,
            "n_failed": self.n_failed,
            "results": [r.to_dict() for r in self.results],
            "parse_error": self.parse_error,
        }


# ── YAML extraction (no external dep — small hand-parser) ─────────────────

_YAML_BLOCK_RE = re.compile(
    r"```(?:yaml|yml)?\s*\n([\s\S]*?)```",
    re.MULTILINE,
)


# ── Behavioral-vs-structural classifier (2026-05-09) ─────────────────────
# Heuristic: flags verification blocks that LOOK structural-only. Used by
# the audit + supervisor analytics to detect tasks that completed via
# structural-pass-while-behavior-broken — the canonical Bug E pattern.
# Conservative — false positives are fine (we just won't flag the task);
# false negatives are the cost (some structural-only blocks slip through).

_BEHAVIORAL_SIGNAL_TOKENS = (
    # In-process behavioral isolation
    "tempfile", "mkdtemp", "TemporaryDirectory", "shutil.rmtree",
    # Test runners (behavioral by definition — they invoke real code paths)
    "pytest", "unittest", "nose2", "ward",
    # State mutation patterns
    ".write_text(", ".write(", "open(", "os.makedirs", "Path(",
    # Common assertion patterns that read back actual results
    "assert ", "assertEqual", "assertTrue", "assertIsNotNone",
)

_BEHAVIORAL_TYPES = ("test_suite", "screenshot_compare")


def is_check_likely_behavioral(check: dict) -> bool:
    """Heuristic: does this check exercise a real code path, or is it structural?

    Behavioral signals (any one is enough):
      - type is test_suite or screenshot_compare (behavioral by category)
      - command/run contains test-runner invocations or in-process behavioral
        tokens (tempfile, write_text, assert, etc.)

    Structural-only checks (returns False):
      - type=assertion with `contains:` (file-content grep)
      - type=command running `--help` / `--version` / a single `print('ok')`

    See the canonical case (Bug E DESC PRESERVE, 2026-05-08): assert
    "def writeSkillFile" in source returned True the entire time the
    function was actively regressing SKILL.md descriptions. The contains
    assertion is structural; only a real call + read-back catches that.
    """
    if not isinstance(check, dict):
        return False
    if check.get("type") in _BEHAVIORAL_TYPES:
        return True
    # Look for behavioral tokens in the runnable text
    text_pool = " ".join(
        str(check.get(k, "")) for k in ("command", "run", "script")
    )
    if any(tok in text_pool for tok in _BEHAVIORAL_SIGNAL_TOKENS):
        return True
    return False


def behavioral_check_count(checks: list[dict] | None) -> int:
    """Return the number of checks classified as behavioral by the heuristic."""
    if not checks:
        return 0
    return sum(1 for c in checks if is_check_likely_behavioral(c))


def has_any_behavioral_check(checks: list[dict] | None) -> bool:
    """True iff at least one check in the list is heuristic-behavioral.

    Used by the audit + analytics to flag tasks whose verification block is
    entirely structural (= pretends to verify behavior but doesn't). This
    is the auto-detect companion to the PLANNING_PROMPT requirement that
    "every verification block MUST include at least one behavioral check"
    (added 2026-05-08). The prompt asks the model to do it; this function
    detects whether the model actually did.
    """
    return behavioral_check_count(checks) >= 1


def _validate_check_runnable(check: dict) -> str | None:
    """Return an error message if the check is structurally broken; None if OK.

    Bug J fix (2026-05-09): refinement-supplied verification YAML can
    contain `command` strings with literal `\\n` characters from the
    model trying to write multi-line Python in a single `python -c "..."`.
    The executor's verification runner passes the command to subprocess
    which sees the `\\n` as a real newline mid-string-literal, producing
    `SyntaxError: unexpected character after line continuation character`.
    Catch this BEFORE unioning so the bad check is dropped (with a clear
    log) instead of silently failing at run-time.

    Heuristics flagged:
      - command/run contains a raw newline embedded inside what's clearly
        a single python -c "..." string (mid-string newlines)
      - id is missing or non-string
      - type is missing
    """
    if not isinstance(check, dict):
        return "not a dict"
    if not check.get("id") or not isinstance(check.get("id"), str):
        return "missing or non-string id"
    if not check.get("type"):
        return "missing type"
    cmd = str(check.get("command", "") or check.get("run", "") or "")
    if "python -c " in cmd and "\n" in cmd:
        # Suspicious: multi-line Python passed to python -c as one arg.
        # Allow ONLY if the script uses `;` separators (single line) or if
        # the `\n` is inside a literal escaped sequence the shell will
        # tolerate. The common breakage case: `python -c "import x\ntry:\n   ..."`
        # — a raw newline ends the shell argument and the quoted string is
        # broken. Be conservative: reject any newlines in python -c commands.
        return ("command contains a raw newline inside `python -c \"...\"` — "
                "shell will break the string at the newline. Use ';' separators "
                "to keep the whole script on one logical line, or write the "
                "test to a tempfile and invoke with `python /tmp/test.py`.")
    return None


def extract_verification_blocks_from_refinements(refinements: list[str] | None) -> list[dict]:
    """Extract verification: check entries from any refinement strings.

    Backward-compatible wrapper around extract_refinement_verification_edits
    that returns just the additive/replacing checks (drops the delete-list
    side channel). Most call sites only need the additive set; the new
    delete-list is consumed by verify_plan via the richer function.
    """
    edits = extract_refinement_verification_edits(refinements)
    return edits.get("checks", [])


def extract_refinement_verification_edits(refinements: list[str] | None) -> dict:
    """Extract refinement-supplied verification edits in their full shape.

    Returns:
        {
            "checks": [<check_dict>, ...],   # to union with plan-text checks
            "delete":  [<id>, ...],          # Bug Z.2 fix: explicit drops
        }

    Bug G fix (2026-05-08): refinements carry verification additions as
    parseable YAML. Bug Z.2 fix (2026-05-11): refinements may also carry
    a top-level `verification_delete:` list (just check ids) to explicitly
    drop plan-text checks that the refinement is RENAMING or REPLACING with
    new checks of different ids. Without this, renames left the stale check
    in the union and it failed forever with "file not found" or similar.

    Bug J fix (2026-05-09): each parsed check is validated by
    `_validate_check_runnable` before being returned. Broken checks are
    DROPPED with a print warning so they can't crash the runner.
    """
    out_checks: list[dict] = []
    out_delete: list[str] = []
    if not refinements:
        return {"checks": out_checks, "delete": out_delete}
    try:
        import yaml
    except ImportError:
        return {"checks": out_checks, "delete": out_delete}
    for ref in refinements:
        if not isinstance(ref, str) or "verification" not in ref:
            continue
        # Try fenced ```yaml block first, then bare verification: region
        candidates: list[str] = []
        for m in _YAML_BLOCK_RE.finditer(ref):
            body = m.group(1)
            if "verification" in body:
                candidates.append(body)
        if not candidates:
            m = re.search(r"^verification\s*:\s*\n((?:[ \t-].*\n?)+)",
                          ref, re.M)
            if m:
                candidates.append("verification:\n" + m.group(1))
        for body in candidates:
            try:
                data = yaml.safe_load(body)
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            checks = data.get("verification")
            if isinstance(checks, list):
                for c in checks:
                    if not isinstance(c, dict):
                        continue
                    err = _validate_check_runnable(c)
                    if err:
                        try:
                            print(f"      {YELLOW}[verification.refinement] dropping "
                                  f"malformed check {c.get('id', '?')!r}: {err}{RESET}")
                        except Exception:
                            pass
                        continue
                    out_checks.append(c)
            # Bug Z.2: pick up verification_delete: top-level list
            delete_list = data.get("verification_delete")
            if isinstance(delete_list, list):
                for entry in delete_list:
                    if isinstance(entry, str) and entry:
                        out_delete.append(entry)
    return {"checks": out_checks, "delete": out_delete}


def extract_verification_block(plan_text: str) -> tuple[list[dict] | None, dict, str | None]:
    """Extract the verification: list and verification_policy: dict from plan_text.

    The planning prompt is asked to emit YAML inside a fenced ```yaml block
    OR as a top-level YAML region. We look for either.

    Returns (checks, policy, error). On success, error is None and checks is
    a list of dicts. On parse failure, checks is None and error is a string.
    """
    try:
        import yaml  # PyYAML is a transitive dep via langgraph; safe to import
    except ImportError:
        return None, {}, "PyYAML not installed (pip install pyyaml)"

    # Strategy 1: fenced ```yaml block containing verification:
    candidates: list[str] = []
    for m in _YAML_BLOCK_RE.finditer(plan_text):
        body = m.group(1)
        if "verification" in body:
            candidates.append(body)

    # Strategy 2: bare verification: ... block at root of plan
    if not candidates:
        # Find a line starting with "verification:" and capture until the next
        # top-level non-indented section or end of document
        m = re.search(r"^verification\s*:\s*\n((?:[ \t-].*\n?)+)",
                      plan_text, re.M)
        if m:
            # Reconstruct as parseable YAML
            candidates.append("verification:\n" + m.group(1))

    if not candidates:
        return None, {}, "no `verification:` block found in plan"

    # Track the most informative YAMLError so we can surface it to the agent
    # in the iteration retry. Without this the agent sees only the generic
    # "did not parse" message and can't fix the underlying syntax (Bug Y,
    # 2026-05-11 — auto_0075 marked failed despite a correct code fix
    # because Python-in-YAML broke parsing and the agent never saw why).
    last_yaml_error: str = ""
    for body in candidates:
        try:
            data = yaml.safe_load(body)
        except yaml.YAMLError as e:
            # Compose a short, actionable error pointing at the failure site
            err_str = str(e).strip().splitlines()[0] if str(e) else type(e).__name__
            # Detect the canonical Python-in-YAML pitfall so the agent gets a
            # specific fix recommendation, not just the raw PyYAML message
            hint = ""
            if '"""' in body or "\\\"\\\"\\\"" in body:
                hint = (" Hint: triple-quoted Python strings inside "
                        "double-quoted YAML scalars are a common cause "
                        "(Bug Y). Restructure to a `|` literal block scalar "
                        "or move the test to a sidecar script.")
            last_yaml_error = f"PyYAML parse error: {err_str}.{hint}"
            continue
        if not isinstance(data, dict):
            continue
        checks = data.get("verification")
        if not isinstance(checks, list) or not checks:
            continue
        policy = data.get("verification_policy", {}) or {}
        if not isinstance(policy, dict):
            policy = {}
        return checks, policy, None

    if last_yaml_error:
        return None, {}, last_yaml_error
    return None, {}, "verification: block found but did not parse as a non-empty list"


# ── Check runners (one per type) ───────────────────────────────────────────

def _run_command_check(check: dict, cwd: Path) -> CheckResult:
    cid = check.get("id", "command")
    cmd = check.get("run") or check.get("command")
    if not cmd:
        return CheckResult(cid, "command", False, 0.0,
                           detail="missing 'run' field")
    expected_rc = int(check.get("expect_returncode", 0))
    expected_substrings = check.get("expect_stdout_contains") or []
    if isinstance(expected_substrings, str):
        expected_substrings = [expected_substrings]
    not_contains = check.get("expect_stdout_not_contains") or []
    if isinstance(not_contains, str):
        not_contains = [not_contains]

    t0 = time.time()
    try:
        proc = subprocess.run(
            cmd, shell=True, capture_output=True, text=True,
            encoding="utf-8", errors="replace", cwd=str(cwd), timeout=120,
        )
        dur = time.time() - t0
        combined = (proc.stdout or "") + (proc.stderr or "")
        rc_ok = proc.returncode == expected_rc
        contains_ok = all(s in combined for s in expected_substrings)
        not_contains_ok = all(s not in combined for s in not_contains)
        passed = rc_ok and contains_ok and not_contains_ok
        detail = (f"rc={proc.returncode} (expected {expected_rc})"
                  + (f" / missing substrings {[s for s in expected_substrings if s not in combined]}"
                     if not contains_ok else "")
                  + (f" / forbidden substrings present {[s for s in not_contains if s in combined]}"
                     if not not_contains_ok else ""))
        # Cap output to 4KB so we don't dump giant logs into context
        output = combined[:4000] if not passed else ""
        return CheckResult(cid, "command", passed, dur, detail=detail, output=output)
    except subprocess.TimeoutExpired:
        return CheckResult(cid, "command", False, time.time() - t0,
                           detail="timeout after 120s", output="")
    except Exception as e:
        return CheckResult(cid, "command", False, time.time() - t0,
                           detail=f"{type(e).__name__}: {e}")


def _run_assertion_check(check: dict, cwd: Path) -> CheckResult:
    cid = check.get("id", "assertion")
    file_rel = check.get("file") or check.get("file_exists")
    if not file_rel:
        return CheckResult(cid, "assertion", False, 0.0,
                           detail="missing 'file' field")
    file_path = (cwd / file_rel).resolve()
    if not file_path.exists():
        return CheckResult(cid, "assertion", False, 0.0,
                           detail=f"file not found: {file_rel}")
    contains = check.get("contains") or check.get("file_contains")
    not_contains = check.get("not_contains") or check.get("file_not_contains")
    try:
        text = file_path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return CheckResult(cid, "assertion", False, 0.0,
                           detail=f"could not read: {e}")
    failures = []
    if contains:
        substrs = [contains] if isinstance(contains, str) else list(contains)
        for s in substrs:
            if s not in text:
                failures.append(f"missing substring {s!r}")
    if not_contains:
        substrs = [not_contains] if isinstance(not_contains, str) else list(not_contains)
        for s in substrs:
            if s in text:
                failures.append(f"forbidden substring {s!r} present")
    passed = not failures
    detail = "ok" if passed else " / ".join(failures)
    return CheckResult(cid, "assertion", passed, 0.0, detail=detail)


def _run_test_suite_check(check: dict, cwd: Path) -> CheckResult:
    """test_suite is just command with longer timeout + always captures output."""
    cid = check.get("id", "test_suite")
    cmd = check.get("run")
    if not cmd:
        return CheckResult(cid, "test_suite", False, 0.0,
                           detail="missing 'run' field")
    expected_rc = int(check.get("expect_returncode", 0))
    t0 = time.time()
    try:
        proc = subprocess.run(
            cmd, shell=True, capture_output=True, text=True,
            encoding="utf-8", errors="replace", cwd=str(cwd), timeout=600,
        )
        dur = time.time() - t0
        passed = proc.returncode == expected_rc
        combined = (proc.stdout or "") + (proc.stderr or "")
        return CheckResult(
            cid, "test_suite", passed, dur,
            detail=f"rc={proc.returncode} (expected {expected_rc})",
            output=combined[:4000] if not passed else "",
        )
    except subprocess.TimeoutExpired:
        return CheckResult(cid, "test_suite", False, time.time() - t0,
                           detail="timeout after 600s")


def _run_screenshot_compare_check(check: dict, cwd: Path) -> CheckResult:
    """Visual diff between reference and output screenshot.

    Schema:
        type: screenshot_compare
        reference: <path to expected image>
        output: <path to actual image>
        max_diff_ratio: <float 0.0-1.0, default 0.10>

    Optional rendering helpers (the planner can ALSO ask the runner to render
    one or both images via Playwright before comparing — useful when the
    actual UI is at a localhost URL that needs to be screenshotted):
        render_reference: <url-or-file://...>     # render to `reference` first
        render_output: <url-or-file://...>        # render to `output` first
        viewport_width: <int, default 1440>
        viewport_height: <int, default 900>

    Comparison is pixel-wise via Pillow. Returns:
        passed=True if Pillow available, both images load, diff_ratio <= threshold
        passed=False otherwise (with detail explaining why)
    """
    cid = check.get("id", "screenshot_compare")
    ref_path = check.get("reference")
    out_path = check.get("output")
    if not ref_path or not out_path:
        return CheckResult(
            cid, "screenshot_compare", False, 0.0,
            detail="screenshot_compare needs both `reference` and `output` paths",
        )
    max_diff = float(check.get("max_diff_ratio", 0.10))
    vp_w = int(check.get("viewport_width", 1440))
    vp_h = int(check.get("viewport_height", 900))

    cwd = Path(cwd).resolve()
    ref = (cwd / ref_path).resolve()
    out = (cwd / out_path).resolve()

    t0 = time.time()

    # Optional Playwright rendering before comparing
    render_ref = check.get("render_reference")
    render_out = check.get("render_output")
    if render_ref or render_out:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return CheckResult(
                cid, "screenshot_compare", False, time.time() - t0,
                detail="render_* requested but playwright not installed",
            )
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                if render_ref:
                    page = browser.new_page(viewport={"width": vp_w, "height": vp_h})
                    page.goto(render_ref, timeout=15000)
                    page.wait_for_load_state("networkidle", timeout=15000)
                    ref.parent.mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=str(ref), full_page=False)
                    page.close()
                if render_out:
                    page = browser.new_page(viewport={"width": vp_w, "height": vp_h})
                    page.goto(render_out, timeout=15000)
                    page.wait_for_load_state("networkidle", timeout=15000)
                    out.parent.mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=str(out), full_page=False)
                    page.close()
                browser.close()
        except Exception as e:
            return CheckResult(
                cid, "screenshot_compare", False, time.time() - t0,
                detail=f"playwright render failed: {type(e).__name__}: {e}",
            )

    if not ref.exists():
        return CheckResult(cid, "screenshot_compare", False, time.time() - t0,
                           detail=f"reference image not found: {ref_path}")
    if not out.exists():
        return CheckResult(cid, "screenshot_compare", False, time.time() - t0,
                           detail=f"output image not found: {out_path}")

    try:
        from PIL import Image, ImageChops
    except ImportError:
        return CheckResult(cid, "screenshot_compare", False, time.time() - t0,
                           detail="Pillow not installed (pip install pillow)")

    try:
        ref_img = Image.open(ref).convert("RGB")
        out_img = Image.open(out).convert("RGB")
    except Exception as e:
        return CheckResult(cid, "screenshot_compare", False, time.time() - t0,
                           detail=f"image load failed: {type(e).__name__}: {e}")

    # Resize output to reference dimensions for fair comparison
    if out_img.size != ref_img.size:
        out_img = out_img.resize(ref_img.size)

    # Pixel-wise difference: count pixels with any channel difference > 30
    diff = ImageChops.difference(ref_img, out_img)
    bbox = diff.getbbox()
    if bbox is None:
        ratio = 0.0
    else:
        # Per-pixel threshold: count pixels where max(R-diff, G-diff, B-diff) > 30
        # (ignores tiny anti-aliasing noise)
        differing = 0
        total = ref_img.width * ref_img.height
        for px in diff.getdata():
            if max(px) > 30:
                differing += 1
        ratio = differing / total if total else 0.0

    dur = time.time() - t0
    passed = ratio <= max_diff
    detail = f"diff_ratio={ratio:.4f} (threshold {max_diff})"
    if not passed:
        detail += f" — {differing}/{total} pixels differ"
    return CheckResult(cid, "screenshot_compare", passed, dur,
                       detail=detail)


_RUNNERS = {
    "command": _run_command_check,
    "assertion": _run_assertion_check,
    "test_suite": _run_test_suite_check,
    "screenshot_compare": _run_screenshot_compare_check,
}


CHECK_TAGS_SIDECAR = Path("logs") / "verification_check_tags.jsonl"
OBSERVATIONAL_CHECK_TAGS = ("correctness", "visual", "contract", "regression", "smoke")


def classify_passing_check(check: dict, result: CheckResult) -> list[str]:
    """Heuristically tag what a passing verification check actually tested."""
    if not result.passed:
        return []
    searchable = " ".join(
        str(part)
        for part in (
            check.get("id", ""),
            check.get("type", ""),
            check.get("run", ""),
            check.get("command", ""),
            check.get("file", ""),
            check.get("contains", ""),
            check.get("not_contains", ""),
            check.get("reference", ""),
            check.get("output", ""),
        )
    ).lower()
    check_type = str(check.get("type", "command")).lower()
    tags: set[str] = set()

    if check_type == "screenshot_compare" or any(token in searchable for token in (
        "screenshot", "visual", "pixel", "image", "playwright", "viewport",
    )):
        tags.add("visual")

    if check_type in ("test_suite", "assertion") or any(token in searchable for token in (
        "pytest", "unittest", "test_", "npm test", "vitest", "jest", "assert",
        "expect_", "contains",
    )):
        tags.add("correctness")

    if any(token in searchable for token in (
        "contract", "schema", "api", "interface", "cli", "json shape",
        "required field", "yaml", "frontmatter", "response shape",
    )):
        tags.add("contract")

    if any(token in searchable for token in (
        "regression", "rerun", "audit", "bug", "preserve", "previously",
    )):
        tags.add("regression")

    if any(token in searchable for token in (
        "smoke", "import", "--help", "version", "startup", "starts", "loads",
        "module_exists", "file_exists",
    )):
        tags.add("smoke")

    if not tags:
        tags.add("smoke" if check_type == "command" else "correctness")
    return [tag for tag in OBSERVATIONAL_CHECK_TAGS if tag in tags]


def append_check_tag_observation(check: dict, result: CheckResult, cwd: Path,
                                 task_id: str = "") -> None:
    """Append sidecar JSONL observation for a passing check."""
    if not result.passed:
        return
    sidecar = cwd / CHECK_TAGS_SIDECAR
    try:
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        observation = {
            "task_id": task_id,
            "check_id": result.id,
            "check_type": result.type,
            "passed": True,
            "tags": result.tags,
            "source": "post_hoc_heuristic",
        }
        with open(sidecar, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(observation, sort_keys=True) + "\n")
    except OSError:
        pass


# ── Top-level runner ──────────────────────────────────────────────────────

def run_verification_block(checks: list[dict], cwd: Path | str,
                           task_id: str = "") -> VerificationOutcome:
    """Run every check, return aggregate outcome.

    Output discipline (failure-loud, success-quiet):
    - PASS: one green line per check
    - FAIL: red header + first 4KB of output + detail field
    """
    cwd = Path(cwd).resolve()
    results: list[CheckResult] = []
    for check in checks:
        ctype = check.get("type", "command")
        runner = _RUNNERS.get(ctype)
        if runner is None:
            results.append(CheckResult(
                check.get("id", "?"), ctype, False, 0.0,
                detail=f"unknown check type: {ctype!r}",
            ))
            continue
        result = runner(check, cwd)
        if result.passed:
            result.tags = classify_passing_check(check, result)
            append_check_tag_observation(check, result, cwd, task_id=task_id)
        results.append(result)

    n_passed = sum(1 for r in results if r.passed)
    n_failed = len(results) - n_passed
    return VerificationOutcome(
        all_passed=(n_failed == 0 and len(results) > 0),
        n_checks=len(results),
        n_passed=n_passed,
        n_failed=n_failed,
        results=results,
    )


def print_outcome(outcome: VerificationOutcome) -> None:
    """Print outcome with failure-loud, success-quiet discipline."""
    if outcome.parse_error:
        print(f"  {RED}{BOLD}[VERIFICATION] could not parse plan's verification "
              f"block: {outcome.parse_error}{RESET}")
        return
    if outcome.n_checks == 0:
        print(f"  {RED}{BOLD}[VERIFICATION] no checks defined — task cannot "
              f"complete without at least one check{RESET}")
        return
    if outcome.all_passed:
        # SUCCESS-QUIET: one green summary line, no per-check noise
        print(f"  {GREEN}[VERIFICATION] all {outcome.n_checks} check(s) passed{RESET}")
        return
    # FAILURE-LOUD: full per-failure dump
    print(f"  {RED}{BOLD}[VERIFICATION] {outcome.n_failed}/{outcome.n_checks} "
          f"check(s) FAILED{RESET}")
    for r in outcome.failed:
        print(f"    {RED}✗ [{r.type}] {r.id} ({r.duration_sec:.1f}s) — "
              f"{r.detail}{RESET}")
        if r.output:
            for line in r.output.rstrip().splitlines()[:30]:
                print(f"        {DIM}{line}{RESET}")


# ── Convenience wrapper for self-test integration ────────────────────────

def verify_plan(plan_text: str, cwd: Path | str, task_id: str = "",
                refinements: list[str] | None = None) -> VerificationOutcome:
    """One-shot: extract block from plan_text + refinements, run it, return outcome.

    Bug G fix (2026-05-08): refinements are parsed for additional verification:
    YAML blocks that get UNIONED with plan_text's checks. Dedupe is by check
    `id` — refinement-supplied checks with an id that already exists in
    plan_text REPLACE the original (so humans can amend, not just append).
    Refinement-supplied checks with new ids are appended.

    Bug Z.2 fix (2026-05-11): refinements may also carry a top-level
    `verification_delete:` list of ids to DROP from the plan-text checks.
    Used when the refinement is RENAMING a check (giving it a new id) and
    needs to clear the old id from the union. Without this, the stale
    check failed forever with "file not found" because the original plan's
    paths were authored before app_location was set, and the refinement
    couldn't communicate "this id is obsolete."
    """
    checks, policy, err = extract_verification_block(plan_text)
    edits = extract_refinement_verification_edits(refinements)
    extra_checks = edits["checks"]
    delete_ids = set(edits["delete"])

    # Bug R fix (2026-05-10): if plan_text has no parseable verification
    # block but a refinement DOES, use the refinement-supplied checks as
    # the primary plan rather than failing. Surfaced via auto_0070 — the
    # planner (claude:haiku) produced clarifying questions only with no
    # verification block, but the refinement (codex:gpt-5.5) supplied a
    # full 4-check verification YAML. Without this fallback, verify_plan
    # returned parse_error and the refinement's checks were ignored, even
    # though they were perfectly valid.
    if err is not None:
        if extra_checks:
            print(f"      {DIM}verification: plan_text had no block "
                  f"({err!r}); using {len(extra_checks)} refinement-"
                  f"supplied check(s) as primary plan{RESET}")
            return run_verification_block(extra_checks, cwd, task_id=task_id)
        return VerificationOutcome(
            all_passed=False, n_checks=0, n_passed=0, n_failed=0,
            parse_error=err,
        )

    # ── Bug Z.2: drop checks that refinement explicitly marks deleted ──
    if delete_ids:
        before = len(checks)
        checks = [c for c in checks
                  if not (isinstance(c, dict) and c.get("id") in delete_ids)]
        dropped = before - len(checks)
        if dropped > 0:
            print(f"      {DIM}verification: refinement_delete dropped "
                  f"{dropped} stale plan-text check(s){RESET}")

    # ── Bug G fix: union refinement-supplied verification additions ──
    if extra_checks:
        # Build id index of existing checks for dedupe/replace semantics
        id_to_idx = {c.get("id"): i for i, c in enumerate(checks) if isinstance(c, dict) and c.get("id")}
        for extra in extra_checks:
            extra_id = extra.get("id")
            if extra_id and extra_id in id_to_idx:
                checks[id_to_idx[extra_id]] = extra  # replace by id
            else:
                checks.append(extra)  # append new
        print(f"      {DIM}verification: unioned {len(extra_checks)} refinement-supplied check(s) "
              f"(total {len(checks)}){RESET}")
    return run_verification_block(checks, cwd, task_id=task_id)
