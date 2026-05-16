# The vault — vision

The vault is a personal capability multiplier for one user. It coordinates LLM coding agents on the user's projects: routing each phase to the right model, injecting only the narrow context relevant to the task, verifying work with mechanical checks plus a second-opinion LLM advisor whose concerns surface at human gates, and recording diagnostic data so the system keeps getting better. It's a thin layer over capable LLMs — not a chatbot wrapper, not a memory database, not a chain-of-prompts shell. Engine and structure live on GitHub; the user's skills, project charters, vision, and notes are personal.

---

## What problem this solves

Coding LLMs are capable but inconsistent: they forget preferences between sessions, default to whatever pattern is trendiest in training data, pick the wrong tool when they don't know the project, and produce plausible-looking work that doesn't quite hold together. Existing harnesses (Claude Projects, Cursor, Aider, OpenHands) help, but each is tied to one provider, ships a fixed memory model, and treats every task the same.

The vault gives one user a coordination layer that:
- **Routes phases across LLMs** — Claude, Codex, Gemini, whichever performs best on *this* phase by recorded data
- **Loads narrow context per task** — skills, per-project charter, capabilities registry — only what's relevant, not everything always
- **Verifies before declaring done** — mechanical checks + LLM advisor whose concerns surface to the user at human gates
- **Learns about itself** — diagnostic data on what failed, what cost, which model worked, so future tasks route smarter

---

## What it isn't

- **Not a chatbot wrapper.** Interactions go through a structured pipeline (lazy prompt → sharpen → plan → human gate → execute → verify → judge → human gate), not freeform chat with tool calls.
- **Not an auto-learning memory.** We tried agent-readable cross-task memory (auto-curated skills, failure injection into planning). It didn't help; removed. Skills, charters, capabilities, and ai_main are hand-authored. Auto-write is reserved for diagnostic data that *humans* read.
- **Not vault-building-vault.** Subprocess agents only work on user projects. Vault infrastructure changes go through the orchestrator session directly.
- **Not defensive-architecture-as-engineering.** When in doubt, fewer moving parts. The vault has been dialed back from over-engineered states more than once; output quality didn't suffer.
- **Not LLM-vendor-locked.** Claude, Codex, Gemini all participate. Routing picks the right one per phase.
- **Not productized.** Built for one user. Engine on GitHub for transparency and structural portability; content (skills, charters, vision, notes) is personal.

---

## Principles (immovable)

1. **Quality of product > efficiency of vault > token efficiency.** Token cost is a tiebreaker among equivalent-quality options, never an excuse to skip a quality gate.
2. **Thin layer over capable LLMs.** Models are smart; the vault doesn't try to be smarter. It coordinates, routes, injects the right context, verifies — and stays out of the way otherwise.
3. **Curate, don't auto-learn.** Anything an agent reads at runtime (ai_main, skills, capabilities, charter) is hand-authored by user + orchestrator. Auto-write is reserved for diagnostic data humans inspect.
4. **Hybrid Norm verification.** Mechanical checks (tests, builds, file assertions) answer "did the code work?" — LLM advisor concerns answer "is the work good?" Both, paired, surfaced to a human gate. Neither replaces the other.
5. **Universal first, specialized via match.** ai_main and skills generalize across projects. Specialization happens by description-matching skills to tasks and via per-project charter, not by forking ai_main per project.
6. **Three-audience memory.** Subprocess agents read prescriptive guidance (ai_main + skills + capabilities + charter). Vault code reads routing/cost data. Humans read diagnostics. No muddle zone where one file serves all three.

---

## Open territory

If the vault matures into what it aspires to be: it coordinates agents across many of the user's projects without re-explaining preferences each time; routing picks the right model often enough that the user stops thinking about it; the skill library covers the domains the user actually works in; the judge's concerns at human gates genuinely help the user catch issues, not just generate plausible-looking noise; the diagnostic data tells the user — not just the system — what's actually working. None of this is on a timeline — it's the shape of "things going well."
