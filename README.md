# vault

**A one-user, multi-LLM coding harness.** It coordinates Claude, Codex, and Gemini agents through a structured pipeline (sharpen → plan → plan-review → refine → execute → judge → learn) and lets a single human stay at decision gates. Per-phase routing picks the model with the best recorded performance; cross-model verification pairs mechanical checks with an LLM advisor; agents read from a hand-curated skills library and a pre-approved capabilities registry rather than always-loaded mega-memory.

Built on LangGraph with SQLite checkpointing. Designed to be a thin layer over capable LLMs, not a thick self-managing system. Engine + structure live here on GitHub; the content a user runs on top (their skills, charters, prompts) is personal.

---

## How it works

**The pipeline.** A task flows through a LangGraph state machine:

```
task_entry → planning → plan_review → ⏸ wait_human_answers
  → refinement → ⏸ wait_human_approval
  → execution → (verify + judge) → ⏸ wait_human_review
  → learning → terminal_complete
```

The `⏸` points are interrupts: the daemon halts and waits for the human to resume via `vault.py advance`. Every state transition is checkpointed to SQLite, so a task can be inspected or resumed at any point.

**Per-phase model routing.** Each phase independently routes to a provider/model from `state/model_pool.json`. Selection is data-driven: `logs/cost_log.jsonl` records cost + outcome per call, and a Wilson lower-bound exploration keeps trying alternatives so the system never locks onto a stale leader. Planning might run Sonnet, execution Haiku, judging Gemini — whatever the data supports for that phase.

**Hybrid-Norm verification.** Each plan ships a `verification:` block — mechanical checks (commands, file assertions, builds) answering "did the code work?" Separately, a cross-model **judge** (different LLM than the executor) reviews the diff and produces structured concerns + axis scores answering "is it good?" Both surface to the human at `wait_human_review`. The judge is an advisor — it never auto-routes or auto-blocks; the human decides.

**Narrow context.** Agents read `ai_main.md` (universal rules), description-matched skills from `ai_skills/`, the `state/capabilities.json` library registry, and a per-project `charter.md`. Nothing is auto-learned; everything an agent reads is hand-curated.

**Watchdog.** A background thread tree-kills any CLI subprocess wedged past a threshold. That's the only autonomous intervention; everything else routes to the human.

**Diagnostics.** `vault.py diagnostics` reads the logs/checkpoints read-only — summary, per-task drill-down, search, live snapshot.

---

## Repo layout

```
ai_main.md              Universal rules every subprocess agent reads
VISION.md               Why this exists (the manifesto)
README.md               This file
CLAUDE.md / AGENTS.md / GEMINI.md
                        Vendor stubs that point at ai_main.md

scripts/
  ai_dougs/             Core engine: routing, cost tracking, CLI invocation
  ai_sharpener/         Sharpener daemon (lazy prompt → structured task)
  vault_graph/          LangGraph pipeline: nodes/, judge, watchdog,
                        diagnostics, checkpointer
  hooks/                pre_write / pre_read / pre_bash safety backstops
  vault_graph_audit.py  Self-checks for the harness
  vault_smoke_test.py   Fast import/syntax sanity

ai_skills/              Hand-curated skills (Anthropic Skills SDK schema)
state/
  model_pool.json       Provider/model registry
  capabilities.json     Pre-approved library registry
  routing_params.json   Routing tunables
ai_context/             Auto-written diagnostics (failure_modes,
                        model_performance, daily_summaries) — read by us,
                        not injected into agents
ai_instructions/        Lazy-prompt inbox + sharpened-task staging
task_files/             Per-task spec files (auto_NNNN.md)
logs/                   cost_log, events, skill_usage, session logs
.agent_memory/<slug>/   Per-project charter + task_log (gitignored)
_human_notes/           Orchestrator-only zone (gitignored)
vault.py                CLI entrypoint
```

---

## Setup

**Prereqs:** Python 3.12+, the three vendor CLIs (Claude Code, Codex, Gemini), and their subscription logins.

```bash
pip install -r requirements.txt        # langgraph, pyyaml, etc.
```

**CLI auth** (LLM work goes through subscriptions, not per-token API billing):

| CLI | Login |
|-----|-------|
| Claude Code | `claude /login` (Claude Max/Pro) |
| Gemini CLI | `gemini` (interactive; Google account) |
| Codex CLI | `codex login --device-auth` (ChatGPT sub) |

**API keys** are optional and only used for free `/v1/models` discovery endpoints — never for paid LLM calls. Put them in `.env` (gitignored): `OPENAI_API_KEY`, `GEMINI_API_KEY`, `ANTHROPIC_API_KEY`.

**Run it:**

```bash
python vault.py daemon          # poll task_files/, run the pipeline
python vault.py --help          # everything else
```

---

## CLI

`python vault.py <command>`:

| Command | Does |
|---|---|
| `daemon` | Poll `task_files/`, run tasks through the pipeline |
| `list` / `show` / `inspect` | Task state + checkpoint history |
| `new` / `advance` / `rerun` / `replay` | Create, resume, retry, replay tasks |
| `delete` / `snooze` | Remove checkpoints / pause daemon pickup |
| `diagnostics` | Read-only inspector: `summary`, `task <name>`, `search <q>`, `live` |
| `budget` / `status` / `models` | Spend analytics, daemon health, provider status |
| `skill-stats` / `prune-skills` | Skill load/use stats, curation fitness |
| `health` | Honest report on what's working / broken |

Run any command with `--help` for flags.

---

## Notes

This is one person's personal harness, developed in the open. The engine and structure are the shareable part; the content a user runs on top (their skills, project charters, prompts, notes) is personal and gitignored. No formal contribution process — it's built for one user's workflow, not as a product.

The `_human_notes/` directory and `.agent_memory/` are orchestrator/personal zones and never committed.
