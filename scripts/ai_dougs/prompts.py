"""Prompt constants for ai_dougs."""

PLANNING_PROMPT = """\
{initial_prompt}

CONTEXT:
{context}

Provide:
1. A detailed step-by-step plan
2. Clarifying questions only if something is genuinely unclear â€” default to none
3. Any assumptions you are making

TECHNOLOGY CHOICE â€” choose the right tool for the job, not the easiest one to generate. \
If the task involves a web frontend, evaluate whether the existing stack is the best choice. \
A plain CSS approach may be correct for a small project; Tailwind + a component library may be \
correct for a complex UI. Make the call that a professional team would make â€” and justify it. \
Do not default to raw CSS just because it is familiar.

PACKAGES â€” if your plan requires any npm or pip packages not already installed in the project, \
list every one of them in a "## Packages Requested" section at the end of your plan, with a \
one-line justification for each. The human must approve these before execution runs. \
Do NOT include packages that are already in the project's package.json / requirements.txt.

RESEARCH â€” if your plan would benefit from fetching external URLs (screenshots, documentation, \
design references, API specs), list every URL under a "## Research Requested" section with a \
one-line explanation of exactly what you expect to get from it and how it will be used. \
Do not fetch anything during planning â€” only list what you intend to fetch. \
The human must approve specific URLs before execution may fetch them.

Your plan MUST end with a "## Verification" section containing a fenced \
```yaml block with one or more machine-runnable checks. Format:

```yaml
verification:
  - id: <short_identifier>
    type: command | assertion | test_suite
    # type=command:    run (shell), expect_returncode (default 0),
    #                  expect_stdout_contains [list of strings], optional expect_stdout_not_contains
    # type=assertion:  file (path relative to vault root), contains <string> | not_contains <string>
    # type=test_suite: run (shell, longer timeout), expect_returncode (default 0)
verification_policy:
  max_iterations: 2                   # how many execution attempts total
  on_first_failure: retry_with_higher_tier_model
  on_repeated_failure: escalate_to_human
```

Every promise the plan makes MUST have at least one corresponding verification \
check. Examples:
- Plan says "add `vault.py models` command" â†’ check: `python vault.py models` returncode 0
- Plan says "add function `cmd_models` to cli.py" â†’ assertion: file `scripts/vault_graph/cli.py` contains `def cmd_models`
- Plan says "tests pass" â†’ test_suite: `pytest tests/test_models.py`

**The verification block MUST include at least one BEHAVIORAL check** that \
exercises the actual code path end-to-end (calls the new function, mutates state, \
reads back, validates the result). File-contains assertions and "function \
defined" structural checks are belt-and-suspenders, NOT the primary signal. \
Why this is required: structural checks pass while behavior is broken. The \
canonical case (Bug E DESC PRESERVE, 2026-05-08): `assert "def writeSkillFile" \
in source` returned True the entire time the function was actively regressing \
SKILL.md descriptions from 110 words to 14. Only a behavioral check (call the \
function with a known input, read back the file, assert the schema is intact) \
catches that class of failure. Use `type: command` with `python -c "..."` for \
inline behavioral tests; use `tempfile.mkdtemp()` + `shutil.rmtree()` for \
isolation so checks don't pollute real files.

**Verification commands MUST be cross-shell portable.** They run via \
`subprocess.run(shell=True)` â€” `cmd.exe` on Windows, `sh` on POSIX. Do NOT \
use PowerShell-only syntax (`Get-ChildItem`, `@'...'@` heredocs, \
`Where-Object`, `ForEach-Object`, `| python -` heredoc piping). Prefer \
inline `python -c "..."` for any non-trivial check â€” Python is reliably \
available on every host the vault runs on. For file existence / content \
checks, prefer the `assertion` type (which doesn't shell out at all) over \
shelling out to `Get-ChildItem` / `Test-Path` / `ls`.

**Verification YAML must be PyYAML-safe.** The parser is `yaml.safe_load`. \
Common pitfalls that make the WHOLE verification block fail to parse \
(Bug Y, 2026-05-11 — auto_0075 marked failed despite correct code fix):

- **Do NOT embed Python triple-quote strings inside double-quoted YAML \
scalars.** Patterns like `command: "python -c \"exec(\\\"\\\"\\\"...\\\"\\\"\\\")\""` \
confuse PyYAML and the entire verification block is rejected with \
`parse_error: verification: block found but did not parse as a non-empty list`. \
The whole task gets marked `failed`.
- **For multi-line Python, use a YAML literal block scalar (`|`)** instead \
of jamming everything onto one line with `python -c "..."`. Example:
  ```yaml
  - id: foo_behavioral
    type: command
    run: |
      python -c "import json, tempfile; \
                 d = tempfile.mkdtemp(); \
                 # actual test logic on separate lines, no triple quotes
                 print('ok')"
  ```
- **For long / complex Python, write a sidecar script** (e.g. \
`task_files/.<task_name>_verify.py`) and reference it: \
`run: python task_files/.foo_verify.py`. Cleaner, no escape-hell.
- **Single-line `python -c "..."` is fine** when the test is one line. \
Escape inner double quotes with `\"` or use single quotes inside: \
`python -c "import x; assert x.f('y') == 'z'"`. No triple quotes.

When in doubt, run your verification YAML through `python -c "import yaml; \
print(yaml.safe_load(open('plan.yaml')))"` mentally â€” if you'd need to \
escape triple quotes, restructure to a block scalar or sidecar script.

**Test-writing tasks STILL need a verification block** â€” and it is NOT optional. \
If your task is "write a test that verifies X", the verification block must \
include a `test_suite` check that actually invokes pytest (or unittest, or \
whatever the project uses) on the new test file and asserts returncode 0. \
The test itself is the SUBJECT of the task; the verification block proves \
the test was actually written, runs, and passes. Do NOT fall back to prose \
"## Completion Criteria" with grep commands â€” that format is deprecated and \
the verification gate cannot parse it. The yaml block is the only accepted \
exit gate format.

The task CANNOT be marked completed unless every check returns passed=true. \
Vague prose like "redesign the page" without a runnable check means the task \
will never be allowed to complete â€” be specific.

For visual/design tasks include either a screenshot_compare check (if the plan \
provides reference + output image paths) or an assertion that the relevant \
files contain the specific design tokens (e.g. assert App.css contains `var(--gold)`).

Format the rest of the plan as markdown; the verification block is the exit gate."""

PLAN_REVIEW_PROMPT = """\
You are reviewing a plan written by another AI agent for adequacy before execution begins.
Your job is NOT to rewrite the plan. Your job is to flag concerns so the human can decide.

Score the plan on these axes (1-5 each, 5 = excellent):
1. Scope clarity â€” are the boundaries of the change clear?
2. Completeness â€” does the plan address everything the prompt asked for?
3. Specificity â€” does it specify files, functions, concrete steps (not vague intent)?
4. Risk awareness â€” does it identify edge cases or things that could go wrong?
5. Verifiability â€” are completion criteria measurable?

Respond in this exact format (no other text):

SCORES:
- scope_clarity: <1-5>
- completeness: <1-5>
- specificity: <1-5>
- risk_awareness: <1-5>
- verifiability: <1-5>

CONCERNS:
- <one concern per line, or "None" if no concerns>

VERDICT: <APPROVE | NEEDS_REFINEMENT | REJECT>

ORIGINAL PROMPT:
{initial_prompt}

PLAN UNDER REVIEW:
{plan}
"""

REFINEMENT_PROMPT = """\
The human has reviewed the plan and provided feedback. The full task file below \
contains the initial prompt, the plan, and the human's response â€” read all of it.

Keep your response proportional to the feedback â€” a one-line note warrants a brief \
acknowledgement and a targeted change, not a full rewrite. A major correction warrants \
a complete revised plan. Either way, confirm you are ready to execute at the end.

CRITICAL â€” verification block edits MUST be machine-readable YAML, not prose:

If the human's feedback requests adding, modifying, or replacing any verification \
check, your response MUST include a literal fenced ```yaml block containing the \
new/modified `verification:` entries. Prose like "I will add a check for X" is \
NOT actionable â€” the executor parses YAML from refinements via \
`extract_verification_blocks_from_refinements` and unions with the original plan's \
verification block. Dedupe is by check `id` (refinement-supplied checks REPLACE \
plan-supplied ones with the same id; new ids are appended). Example:

```yaml
verification:
  - id: project_memory_end_to_end_behavior
    type: command
    command: python -c "<inline behavioral test>"
    expect_returncode: 0
```

If the feedback is NOT about verification (just clarifies scope, fixes a typo, \
adjusts approach), normal prose is fine. The YAML requirement is ONLY for \
verification additions/modifications. (Bug G fix, 2026-05-08: refinements that \
described verification edits in prose were silently dropped.)

**Renaming a check** (Bug Z.2 fix, 2026-05-11): if you're replacing a plan-text \
check with one that has a DIFFERENT id (e.g. splitting `ui_button_exists` into \
`ui_button_ghost_variant`, `ui_button_solid_variant`, `ui_button_destructive_variant`), \
add a top-level `verification_delete:` list naming the old ids. Without this, \
the old check stays in the union and fails forever. Example:

```yaml
verification_delete:
  - ui_button_exists       # superseded by the three variant-specific checks below
verification:
  - id: ui_button_ghost_variant
    type: assertion
    file: src/components/ui/Button.js
    contains: "ghost"
  - id: ui_button_solid_variant
    type: assertion
    file: src/components/ui/Button.js
    contains: "solid"
  - id: ui_button_destructive_variant
    type: assertion
    file: src/components/ui/Button.js
    contains: "destructive"
```

If you keep the SAME id and update the body, normal dedup-by-id semantics apply \
(your new version replaces the old). `verification_delete:` is ONLY needed when \
ids change.

FULL TASK FILE:
{full_task}

Your response will be stored as a new section titled exactly: **Plan refinement {refinement_n}**.
Format as markdown."""

EXECUTION_PROMPT = """\
{scope_block}
Execute the approved work using the COMPLETE task file below as context (all sections: prompt, plans, refinements, answers, questions, etc.).

PLAN PRECEDENCE â€” when instructions conflict, follow the higher item first (descending importance):
1. **Plan refinement N** sections: use them in **descending numeric order** (largest N first, then N-1, â€¦).
2. Then the initial **Plan** section (first planning output).
3. Treat **Initial Prompt**, **Context**, **Human Answers**, and other sections as supporting context.

FULL TASK FILE:
{full_task}

PACKAGE GATE â€” before running any install command:
- Find the "## Packages Approved" section in the Human Answers of the task file above.
- Only install packages explicitly listed there. If a package you need is NOT in that list, \
  do not install it â€” note the gap in your summary instead.
- If no "## Packages Approved" section exists, treat all package installs as blocked.

RESEARCH GATE â€” before fetching any external URL:
- Find the "## Research Approved" section in the Human Answers of the task file above.
- Only fetch URLs explicitly listed there. Do not fetch any other URL, even if it seems useful.
- If no "## Research Approved" section exists, treat all external fetching as blocked.

COMPLETION GATE â€” before writing your final summary you MUST:
1. Find the "## Completion Criteria" section in the plan above.
2. Run every check listed there (grep for patterns, count occurrences, verify file changes, etc.).
3. For any criterion that fails, continue working and fix it â€” do not declare done until all pass.
4. In your summary, report the result of each criterion check explicitly (pass/fail + evidence).

Do not skip this step. A build that compiles is not the same as a task that is done. \
The criteria are the exit gate â€” declaration of completion is only valid when all criteria show pass.

Please execute each step and provide:
1. Step-by-step execution output
2. Any errors encountered and how you resolved them
3. Completion criteria check results (one line per criterion)
4. Final result summary

Format as markdown."""

LEARNING_PROMPT = """\
Review this completed task and produce structured outputs for the vault's
diagnostics + routing. Be concise — this output is machine-parsed.

2026-05-12: trimmed. The vault no longer auto-curates skills, auto-writes
project context, or tracks cost/context-effectiveness from this output
(those systems were removed). Only model-performance, skill-usage, and
routing-param signals are still consumed.

FULL TASK RECORD:
{full_task}

---

Reply using EXACTLY these delimiters (keep every ##...## marker on its own line):

##MODEL_START##
task_type: [2-4 word label e.g. react-frontend-redesign or python-api-refactor]
provider: [claude/gemini/codex]
planning_model: [exact model used for planning — from task frontmatter "planning_model"]
execution_model: [exact model used for execution — from task frontmatter "execution_model"]
execution_attempts: [number — from task frontmatter "execution_attempts", default 1]
outcome: [good / acceptable / poor]
notes: [one line — was the planning model appropriate? the execution model? what would be better?]
##MODEL_END##

##SKILLS_USED_START##
# Skills auto-injected into the planning prompt are listed in the task record above
# under "## Relevant Skills (auto-loaded)". Report which ones were actually applied:
used: [comma-separated skill names you actually applied during this task, or "none"]
helpful: [subset of `used` that genuinely improved the outcome, or "none"]
##SKILLS_USED_END##

##PARAMS_START##
# Only include keys you have evidence to adjust. Omit keys you have no opinion on.
# Valid keys (all numeric):
#   Routing:     min_history_runs, success_threshold, escalate_threshold,
#                cost_drift_std_devs, provider_min_success_rate
#   Timeouts:    build_timeout_sec, resolver_timeout_sec,
#                lock_timeout_execution_min, lock_timeout_other_min
#   Polling:     poll_active_sec, poll_idle_sec
#   Execution:   max_autofix_passes
#   Vision:      vision_max_tokens
# Format: key: value  (one per line). Write nothing between the markers if no change.
##PARAMS_END##
"""

VISUAL_COMPARISON_PROMPT = (
    "You are a design QA reviewer. "
    "Image 1 is the REFERENCE design that was approved. Image 2 is the BUILT output that must match it. "
    "Compare them for design fidelity. Check each of the following and note any discrepancy:\n"
    "1. Layout structure â€” same sections in the same order (nav, hero, product card, bento grid, CTA, footer)?\n"
    "2. Color palette â€” background, card, accent colors match?\n"
    "3. Typography â€” font weight, size hierarchy, heading style match?\n"
    "4. Hero section â€” heading text style, gradient text line, CTA buttons, aurora blobs visible?\n"
    "5. Navigation/header â€” brand position, button styles, blur backdrop?\n"
    "6. Hero product card â€” topbar with dots, context pills, data table visible?\n"
    "7. Below-the-fold â€” bento grid or card sections present?\n"
    "Be concrete and specific about each discrepancy (e.g. 'hero gradient text is missing', "
    "'bento grid is absent', 'background is wrong shade'). "
    "Conclude with PASS if the implementation closely matches the reference design, "
    "or FAIL if there are significant visual discrepancies."
)


# SKILL_CURATION_PROMPT + SKILL_CURATION_JUDGE_PROMPT removed 2026-05-12.
# The auto-curator that used them was disabled then removed; skills are
# now user + orchestrator co-authored. See git history (commits before
# 2026-05-12) for the legacy prompts.
