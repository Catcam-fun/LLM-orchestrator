"""docs_refresh.py — skill-quality detector.

2026-05-12: STATUS.md generation removed. STATUS.md was a gitignored
auto-generated dashboard nobody read; `vault.py diagnostics` replaces it.
All that remains here is `_detect_skill_quality_issues`, used by
`vault.py prune-skills` and the audit suite to catch damaged SKILL.md files
(the CLI-banner / truncation false-success pattern).
"""
from __future__ import annotations
from pathlib import Path


def _detect_skill_quality_issues(skills_dir: Path, names: list[str]) -> list[tuple[str, str]]:
    """Return [(skill_name, reason), ...] for damaged-looking SKILL.md files.

    Catches the failure pattern that produced the original frontend-ui damage:
    files that contain only a CLI banner string, are too small to hold real
    procedural content, or are missing the required YAML front-matter.
    """
    issues: list[tuple[str, str]] = []
    BANNER_MARKERS = (
        "OpenAI Codex",
        "research preview",
        "claude-code",
        "(c) 20", "(C) 20",
    )
    MIN_BYTES = 200       # under this is too small to hold any real skill
    MIN_LINES = 8         # canonical Anthropic skill shape needs at least
                          # frontmatter + Purpose/When/Steps blocks
    for name in names:
        p = skills_dir / name / "SKILL.md"
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            issues.append((name, "unreadable"))
            continue
        size = len(text)
        lines = text.count("\n")
        if size < MIN_BYTES:
            issues.append((name, f"only {size} bytes (likely truncated/banner)"))
            continue
        if lines < MIN_LINES:
            issues.append((name, f"only {lines} lines"))
            continue
        if not text.startswith("---"):
            issues.append((name, "missing YAML front-matter"))
            continue
        # Banner-only check: short file dominated by CLI marker text
        if size < 1000 and any(m in text for m in BANNER_MARKERS):
            issues.append((name, "matches CLI-banner false-success pattern"))
            continue
    return issues
