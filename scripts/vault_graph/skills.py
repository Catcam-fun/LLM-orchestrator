"""Skills auto-loading + effectiveness tracking.

The vault accumulates skills (markdown SKILL.md files in ai_skills/) over time
via the learning phase. Until now they were write-only — no code read them.
This module fixes that:

1. **Auto-load** — when planning a new task, scan all SKILL.md files, score
   each one by keyword overlap with the task text, inject the top-N as
   context for the planner.

2. **Effectiveness tracking** — after each task's learning phase, parse which
   skills the agent reports having used. Append to logs/skill_usage.jsonl
   so we can rank skills by helpfulness over time.

Skill matching uses two signals:
- `triggers:` field in the SKILL.md frontmatter (comma-separated keywords)
- Keyword overlap with task text (4+ char words, case-insensitive)

Effectiveness is computed from skill_usage.jsonl entries with shape:
    {"date","task","skill","loaded","used","helpful"}
"""
from __future__ import annotations
import json
import re
from datetime import datetime
from pathlib import Path

from . import ported as P


def _vault_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


SKILL_USAGE_LOG = _vault_root() / "logs" / "skill_usage.jsonl"


def _parse_skill_frontmatter(skill_path: Path) -> dict:
    """Extract triggers + description from a SKILL.md frontmatter block."""
    try:
        text = skill_path.read_text(encoding="utf-8")
    except Exception:
        return {}
    fm = {}
    if not text.startswith("---"):
        return fm
    end = text.find("\n---", 3)
    if end < 0:
        return fm
    for line in text[3:end].splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            fm[k.strip()] = v.strip()
    return fm


def _list_skills(vault_root: Path) -> list[dict]:
    """Enumerate all skills in ai_skills/, returning frontmatter + path.

    Schema (Anthropic Agent Skills standard):
      - `name` (required) — kebab-case skill identifier; matches folder name
      - `description` (required) — the trigger signal. Phrased "Use this
        skill when an agent must X..." so keyword matching can score it
        against task content.

    Relevance scoring (in `_score_skill`) extracts keywords from the
    description; the folder name contributes as a fallback signal. The
    legacy `triggers:` field is no longer authored on active skills as
    of 2026-05-12 (description is sufficient) but is still parsed if
    present for backward compat with any third-party skills imported
    in the future.

    Loader skips `_*`-prefixed directories (archive / private zones).
    """
    skills_dir = vault_root / "ai_skills"
    if not skills_dir.is_dir():
        return []
    out = []
    for skill_dir in skills_dir.iterdir():
        if not skill_dir.is_dir():
            continue
        if skill_dir.name.startswith("_"):
            continue  # archive / private zones
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            continue
        fm = _parse_skill_frontmatter(skill_md)

        # Legacy triggers field — parsed for back-compat with third-party
        # skills. Active skills (2026-05-12+) use description-only.
        explicit_triggers = [t.strip().lower() for t in fm.get("triggers", "").split(",") if t.strip()]
        # Folder-name words contribute as a low-weight fallback signal
        name_words = re.findall(r"[a-z]+", skill_dir.name.lower())
        triggers = list(set(explicit_triggers) | set(w for w in name_words if len(w) >= 4))

        out.append({
            "name": skill_dir.name,
            "path": skill_md,
            "triggers": triggers,
            "description": fm.get("description", ""),
        })
    return out


def _load_effectiveness() -> dict:
    """Aggregate skill_usage.jsonl into per-skill stats.

    Returns: {skill_name: {loaded, used, helpful, helpful_rate}}
    """
    if not SKILL_USAGE_LOG.exists():
        return {}
    stats: dict = {}
    try:
        with open(SKILL_USAGE_LOG, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                name = e.get("skill")
                if not name:
                    continue
                s = stats.setdefault(name, {"loaded": 0, "used": 0, "helpful": 0})
                if e.get("loaded"):
                    s["loaded"] += 1
                if e.get("used"):
                    s["used"] += 1
                if e.get("helpful"):
                    s["helpful"] += 1
    except Exception:
        return {}

    # Compute helpfulness rate: helpful / used (when used at all)
    for name, s in stats.items():
        s["helpful_rate"] = s["helpful"] / s["used"] if s["used"] > 0 else 0.0
    return stats


# Common English stop words + skill-description boilerplate that should never
# count as relevance signal. Without this, every skill matches every prompt
# via filler words like "that", "this", "skill", "when", "user", "asks".
_STOPWORDS = {
    # English stop words (4+ chars)
    "that", "this", "with", "from", "have", "been", "were", "they", "them",
    "their", "what", "when", "where", "which", "would", "could", "should",
    "your", "yours", "ours", "into", "onto", "upon", "than", "then", "more",
    "most", "some", "such", "only", "also", "very", "much", "many", "some",
    "other", "another", "each", "every", "both", "either", "neither", "make",
    "made", "good", "well", "best", "way", "ways", "thing", "things", "any",
    "all", "just", "still", "even", "yet", "while", "during", "before",
    "after", "above", "below", "between", "without", "within",
    # Skill description boilerplate (recurs across most SKILL.md files)
    "skill", "skills", "use", "uses", "user", "users", "asks", "ask",
    "task", "tasks", "create", "creates", "creating", "build", "builds",
    "code", "file", "files", "guide", "guidance", "instructions", "process",
    "work", "works", "working", "example", "examples", "include", "includes",
    "generate", "generates", "generated", "follow", "following",
    # Common verbs that don't disambiguate intent
    "write", "writes", "writing", "read", "reads", "reading",
    "edit", "edits", "editing", "update", "updates", "updating",
}


def _score_skill(skill: dict, task_keywords: set, effectiveness: dict) -> float:
    """Score a skill for relevance to the current task.

    Combines:
    - Trigger keyword overlap (primary signal — heavily weighted)
    - Description text overlap (secondary, after stop-word filtering)
    - Historical effectiveness (boost good, mild penalty for bad)

    Returns 0 if no real relevance — caller filters those out.
    """
    # Filter stop words from both sides before scoring
    task_meaningful = task_keywords - _STOPWORDS

    # Primary: trigger match (triggers are author-curated, weight 3x)
    trigger_set = set(skill["triggers"]) - _STOPWORDS
    trigger_overlap = len(task_meaningful & trigger_set)

    # Secondary: description word overlap (lower weight since less curated)
    desc_words = set(re.findall(r"[a-z]{4,}", skill["description"].lower())) - _STOPWORDS
    desc_overlap = len(task_meaningful & desc_words)

    base = trigger_overlap * 3 + desc_overlap

    # Require at least 1 trigger overlap OR 2 description matches to load.
    # A single accidental description word match isn't enough.
    if trigger_overlap == 0 and desc_overlap < 2:
        return 0.0

    # Effectiveness boost: scale by historical helpful_rate (default 1.0 if never used)
    eff = effectiveness.get(skill["name"], {})
    used = eff.get("used", 0)
    if used >= 3:
        helpful_rate = eff.get("helpful_rate", 0.5)
        # 0.0 helpful_rate → 0.5x score, 1.0 helpful_rate → 1.5x
        base *= 0.5 + helpful_rate
    return float(base)


def select_relevant_skills(task_text: str, max_skills: int = 3,
                           vault_root: Path = None) -> list[dict]:
    """Score every skill against the task; return top-N relevant matches."""
    vault_root = vault_root or _vault_root()
    skills = _list_skills(vault_root)
    if not skills:
        return []

    task_lower = task_text.lower()
    task_keywords = set(re.findall(r"[a-z]{4,}", task_lower))
    effectiveness = _load_effectiveness()

    scored = [(_score_skill(s, task_keywords, effectiveness), s) for s in skills]
    scored = [(score, s) for score, s in scored if score > 0]
    scored.sort(key=lambda x: x[0], reverse=True)
    return [s for _, s in scored[:max_skills]]


def build_skills_context_block(skills: list[dict], max_chars_per_skill: int = 1500) -> str:
    """Render selected skills as a markdown context block for prompt injection."""
    if not skills:
        return ""
    parts = ["## Relevant Skills (auto-loaded)",
             "",
             "These skills match the current task. Read and apply when relevant — they encode "
             "patterns the vault has learned from prior tasks.",
             ""]
    for skill in skills:
        try:
            content = skill["path"].read_text(encoding="utf-8")
        except Exception:
            continue
        # Strip frontmatter for the inline context
        if content.startswith("---"):
            end = content.find("\n---", 3)
            if end > 0:
                content = content[end + 4:].lstrip()
        if len(content) > max_chars_per_skill:
            content = content[:max_chars_per_skill] + "\n\n... (skill truncated — full text at ai_skills/" + skill["name"] + "/SKILL.md)"
        parts.append(f"### Skill: `{skill['name']}`")
        parts.append("")
        parts.append(content)
        parts.append("")
    return "\n".join(parts)


def log_skill_usage(task_name: str, loaded_skills: list[str],
                    used_skills: list[str] = None,
                    helpful_skills: list[str] = None) -> None:
    """Append per-skill effectiveness records to logs/skill_usage.jsonl.

    `loaded_skills` is required (which skills got injected into the prompt).
    `used_skills` and `helpful_skills` come from the learning agent's report.
    Each skill name in `loaded` gets one log entry with the per-skill flags set.
    """
    used_set = set(used_skills or [])
    helpful_set = set(helpful_skills or [])

    SKILL_USAGE_LOG.parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().isoformat(timespec="seconds")
    date_str = datetime.now().strftime("%Y-%m-%d")

    with P._fileLock(SKILL_USAGE_LOG, timeout=5.0):
        with open(SKILL_USAGE_LOG, "a", encoding="utf-8") as f:
            for skill_name in loaded_skills:
                entry = {
                    "date": date_str,
                    "timestamp": timestamp,
                    "task": task_name,
                    "skill": skill_name,
                    "loaded": True,
                    "used": skill_name in used_set,
                    "helpful": skill_name in helpful_set,
                }
                f.write(json.dumps(entry) + "\n")


def parse_skills_used_from_learning(learning_text: str) -> tuple[list[str], list[str]]:
    """Extract `used` and `helpful` skill lists from the learning agent's response.

    Looks for blocks like:
        ##SKILLS_USED_START##
        used: skill_a, skill_b
        helpful: skill_a
        ##SKILLS_USED_END##

    Or (more lenient) any line `Skills used: a, b` and `Helpful: c, d`.
    Returns (used, helpful) lists.
    """
    used: list[str] = []
    helpful: list[str] = []

    if not learning_text:
        return used, helpful

    # Structured block (preferred — added to LEARNING_PROMPT)
    block_match = re.search(
        r"##SKILLS_USED_START##(.*?)##SKILLS_USED_END##",
        learning_text, re.DOTALL,
    )
    text = block_match.group(1) if block_match else learning_text

    # Look for `used:` and `helpful:` lines
    used_match = re.search(r"^used\s*:\s*(.+)$", text, re.MULTILINE | re.IGNORECASE)
    helpful_match = re.search(r"^helpful\s*:\s*(.+)$", text, re.MULTILINE | re.IGNORECASE)

    def _split(s: str) -> list[str]:
        return [item.strip().strip("`*-") for item in re.split(r"[,;]", s) if item.strip()]

    if used_match:
        used = _split(used_match.group(1))
    if helpful_match:
        helpful = _split(helpful_match.group(1))

    # Filter out non-skill placeholder values
    used = [s for s in used if s and s.lower() not in ("none", "n/a", "(none)", "[none]")]
    helpful = [s for s in helpful if s and s.lower() not in ("none", "n/a", "(none)", "[none]")]
    return used, helpful
