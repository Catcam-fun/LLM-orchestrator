#!/usr/bin/env python3
"""
probe_models.py - Validate every model in model_pool.json against its CLI.

Sends a trivial "say ok" prompt to each (provider, model) pair and reports:
- Which models work (returned a sensible output)
- Which models are unavailable (CLI returned 404 / ModelNotFoundError)
- Which models are auth-broken (401 / unauthorized / API key missing)
- Which models are slow (>30s for trivial response)
- Latency per model

Run after changing model_pool.json, when CLIs update, or when a previously
working model starts failing in production. Output is human-readable; results
also feed cost_log.jsonl with phase=probe so routing has fresh evidence.

Usage:
    python scripts/probe_models.py              # probe all configured models
    python scripts/probe_models.py --provider gemini   # only one provider
    python scripts/probe_models.py --quiet      # just the summary table
"""
import json
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# Force UTF-8 on Windows for arrow / check-mark output
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SCRIPT_VERSION = "1.5"  # 2026-05-03 - Loads vault/.env for API keys so all shells see them


def _loadDotenv():
    """Read vault/.env and populate os.environ for keys not already set.

    Lets the user keep API keys in one vault-local file (gitignored) instead
    of relying on shell-inherited env vars. Process env always wins — .env is
    only used to fill gaps. No external dependency.
    """
    import os
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        return
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            # Don't override values already set in the actual process environment
            if key and value and not os.environ.get(key):
                os.environ[key] = value
    except Exception:
        pass


# Run at import — so any function calling os.environ.get sees the .env values
_loadDotenv()


# Candidate names per provider for --discover mode. Strategy depends on what's
# available per provider:
#
# - codex (openai): we use the /v1/models REST API directly when OPENAI_API_KEY
#   is set — that returns the authoritative live list. The static list below
#   is only a fallback if the API call fails. Generated with _discoverOpenAI().
#
# - claude / gemini: no model-listing API is available without an API key those
#   providers don't always set in env. We brute-force common version-increment
#   patterns. Wide enough to catch likely successors without being absurd.
#
# Edit these lists when new naming patterns emerge.
DISCOVERY_CANDIDATES = {
    "claude": (
        # Tier names used as aliases by the Claude CLI (always work if anything does)
        ["haiku", "sonnet", "opus"]
        # Brute-force version pattern: claude-{tier}-{major}-{minor}
        + [
            f"claude-{tier}-{major}-{minor}"
            for tier in ("haiku", "sonnet", "opus")
            for major in (4, 5)
            for minor in range(1, 10)  # 4-1 through 4-9, 5-1 through 5-9
        ]
        # Bare tier-version aliases (some CLI versions accept these)
        + [f"{tier}-{v}" for tier in ("haiku", "sonnet", "opus") for v in ("5",)]
    ),
    "gemini": (
        # Brute-force gemini-{major}.{minor}-{variant}
        [
            f"gemini-{major}.{minor}-{variant}"
            for major in (2, 3)
            for minor in (0, 5, 6)
            for variant in ("flash-lite", "flash", "pro")
        ]
    ),
    "codex": [
        # Static fallback list (used when OPENAI_API_KEY not set or API call fails).
        # When the API call succeeds, this list is ignored — the live API list wins.
        "gpt-5.5", "gpt-5.5-mini", "gpt-5.5-pro",
        "gpt-5.4", "gpt-5.4-mini", "gpt-5.4-nano", "gpt-5.4-pro",
        "gpt-5-codex", "gpt-5.1-codex", "gpt-5.1-codex-mini",
        "gpt-5.2-codex", "gpt-5.3-codex",
        "o4-mini", "o4",
    ],
}


def _discoverOpenAI():
    """Query OpenAI /v1/models for the authoritative model list.

    Returns a sorted list of model IDs that look usable for chat/completion
    (filters out images, audio, transcription, embeddings, etc). Returns empty
    list on any failure — caller should fall back to DISCOVERY_CANDIDATES.
    """
    import os, urllib.request, urllib.error
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        return []
    try:
        req = urllib.request.Request(
            "https://api.openai.com/v1/models",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError):
        return []
    except Exception:
        return []

    # Filter to text-completion models — exclude image, audio, embeddings, etc.
    EXCLUDE_SUBSTRINGS = (
        "image", "audio", "tts", "transcribe", "realtime",
        "embedding", "whisper", "moderation", "search-api", "search-preview",
        "instruct", "diarize",
    )
    # Only modern model generations. Older ones (gpt-3.5, gpt-4, gpt-4o, o1)
    # exist in /v1/models but aren't typically what we want for new work.
    # Edit this list as new generations launch (gpt-6, o5, etc.).
    MODERN_PREFIXES = ("gpt-5", "o3", "o4")
    result = []
    for m in data.get("data", []):
        mid = m.get("id", "")
        if not mid:
            continue
        if not any(mid.startswith(p) for p in MODERN_PREFIXES):
            continue
        if any(s in mid for s in EXCLUDE_SUBSTRINGS):
            continue
        # Skip dated snapshots — the alias names are what users actually want
        if mid.endswith(("01", "02", "03", "04", "05", "06", "07", "08", "09", "10", "11", "12", "13")) and mid.count("-") >= 3:
            try:
                last = mid.rsplit("-", 1)[1]
                if last.isdigit() and len(last) == 2 and 1 <= int(last) <= 31:
                    continue
            except (IndexError, ValueError):
                pass
        result.append(mid)
    return sorted(set(result))


def _discoverAnthropic():
    """Query Anthropic /v1/models for the authoritative claude model list.

    Requires ANTHROPIC_API_KEY env var. Returns sorted list of model IDs.
    Empty list on any failure — caller falls back to candidate-name probing.
    """
    import os, urllib.request, urllib.error
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return []
    try:
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/models",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError):
        return []
    except Exception:
        return []

    result = []
    for m in data.get("data", []):
        mid = m.get("id", "")
        if not mid or not mid.startswith("claude-"):
            continue
        # Skip dated snapshots — they end in YYYYMMDD (8 digits). We prefer the
        # canonical names (e.g. claude-opus-4-7) which auto-update to the latest
        # snapshot anyway. Keeps the pool clean.
        if re.search(r"-\d{8}$", mid):
            continue
        result.append(mid)
    return sorted(set(result))


def _discoverGemini():
    """Query Google Generative Language /v1beta/models for the authoritative gemini list.

    Requires GEMINI_API_KEY (or GOOGLE_API_KEY) env var. Returns sorted list
    of model IDs (without the 'models/' prefix the API returns them with).
    Empty list on any failure — caller falls back to candidate-name probing.
    """
    import os, urllib.request, urllib.error
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY", "")
    if not api_key:
        return []
    try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError):
        return []
    except Exception:
        return []

    # Filter to text-generation gemini models, strip the "models/" prefix
    EXCLUDE_SUBSTRINGS = (
        "embedding", "aqa", "tts", "image", "vision-only",
        "robotics",  # specialised robotics models, not for chat/coding
        "live",      # streaming / realtime variants
        "native-audio", "audio-",
    )
    result = []
    for m in data.get("models", []):
        full = m.get("name", "")
        if not full.startswith("models/"):
            continue
        mid = full[len("models/"):]
        if not mid.startswith("gemini-"):
            continue
        if any(s in mid for s in EXCLUDE_SUBSTRINGS):
            continue
        # Filter to models that actually support generateContent
        methods = m.get("supportedGenerationMethods", [])
        if methods and "generateContent" not in methods:
            continue
        result.append(mid)
    return sorted(set(result))


def _filterToLatestPerFamily(models, provider):
    """Reduce a list of discovered models to only the latest per family.

    Family is the model's "lineage" — e.g. claude-opus-4-5 and claude-opus-4-7
    are the same family (opus), and we keep only 4-7. Different families coexist
    (haiku, sonnet, opus all stay).

    Aliases like 'haiku', 'sonnet', 'opus' are kept as-is because they always
    auto-resolve to whatever the latest version is.
    """
    import re

    if provider == "claude":
        # Pattern: claude-{tier}-{major}-{minor}  (e.g. claude-opus-4-7)
        # Family = tier (haiku/sonnet/opus). Aliases pass through.
        ALIASES = {"haiku", "sonnet", "opus"}
        latest = {}  # tier -> (major, minor, full_name)
        passthrough = []
        for m in models:
            if m in ALIASES:
                passthrough.append(m)
                continue
            mt = re.match(r"^claude-(haiku|sonnet|opus)-(\d+)-(\d+)$", m)
            if not mt:
                passthrough.append(m)  # unknown pattern — keep
                continue
            tier = mt.group(1)
            ver = (int(mt.group(2)), int(mt.group(3)))
            if tier not in latest or ver > latest[tier][:2]:
                latest[tier] = (ver[0], ver[1], m)
        result = passthrough + [v[2] for v in latest.values()]
        return sorted(set(result))

    if provider == "gemini":
        # Pattern: gemini-{major}.{minor}-{variant}  (variant = flash-lite/flash/pro/etc.)
        # Family = variant. So gemini-2.5-flash beats gemini-2.0-flash but
        # gemini-2.5-pro coexists with gemini-2.5-flash.
        # Skip dated/numbered snapshots (e.g. gemini-2.5-flash-002, -preview-04-09).
        latest = {}  # variant -> (major, minor, full_name)
        passthrough = []
        for m in models:
            mt = re.match(r"^gemini-(\d+)\.(\d+)-(.+)$", m)
            if not mt:
                passthrough.append(m)
                continue
            major, minor = int(mt.group(1)), int(mt.group(2))
            variant = mt.group(3)
            # Skip dated snapshots and previews — prefer canonical aliases
            if re.search(r"-\d{2,}$", variant) or "preview" in variant or "exp" in variant:
                continue
            if variant not in latest or (major, minor) > latest[variant][:2]:
                latest[variant] = (major, minor, m)
        result = passthrough + [v[2] for v in latest.values()]
        return sorted(set(result))

    if provider == "codex":
        # OpenAI naming has many shapes: gpt-5, gpt-5-mini, gpt-5.4-codex,
        # gpt-5.1-codex-mini, o3, o4-mini. Family = canonical shape with the
        # version number stripped. e.g. "gpt-?-codex-mini" family.
        # Skip dated snapshots (gpt-5-2025-08-07).
        family_pattern = re.compile(r"^(gpt|o)(?:-?)(\d+(?:\.\d+)?)(.*)$")
        latest = {}  # family_key -> (version_tuple, full_name)
        passthrough = []
        for m in models:
            # Strip trailing date snapshots
            if re.search(r"-\d{4}-\d{2}-\d{2}$", m):
                continue
            mt = family_pattern.match(m)
            if not mt:
                passthrough.append(m)
                continue
            prefix = mt.group(1)
            ver_str = mt.group(2)
            suffix = mt.group(3)
            family_key = f"{prefix}{suffix}"  # e.g. "gpt-codex-mini"
            try:
                ver_tuple = tuple(int(x) for x in ver_str.split("."))
                # Pad to length 2 for consistent comparison
                if len(ver_tuple) == 1:
                    ver_tuple = (ver_tuple[0], 0)
            except ValueError:
                passthrough.append(m)
                continue
            if family_key not in latest or ver_tuple > latest[family_key][0]:
                latest[family_key] = (ver_tuple, m)
        result = passthrough + [v[1] for v in latest.values()]
        return sorted(set(result))

    return sorted(set(models))

RESET, BOLD, DIM = "\033[0m", "\033[1m", "\033[2m"
GREEN, RED, YELLOW, ORANGE, CYAN = "\033[32m", "\033[31m", "\033[33m", "\033[38;5;208m", "\033[36m"

PROBE_PROMPT = "respond with the single word: ok"
PROBE_TIMEOUT_SEC = 60

# Outcome categories for the report
OUTCOME_OK            = "OK"
OUTCOME_SLOW          = "SLOW"
OUTCOME_REFUSAL       = "REFUSAL"
OUTCOME_UNAVAILABLE   = "UNAVAILABLE"
OUTCOME_UNAUTHORIZED  = "UNAUTHORIZED"
OUTCOME_RATE_LIMITED  = "RATE_LIMITED"
OUTCOME_TIMEOUT       = "TIMEOUT"
OUTCOME_ERROR         = "ERROR"

OUTCOME_COLORS = {
    OUTCOME_OK:           GREEN,
    OUTCOME_SLOW:         YELLOW,
    OUTCOME_REFUSAL:      ORANGE,
    OUTCOME_UNAVAILABLE:  RED,
    OUTCOME_UNAUTHORIZED: RED,
    OUTCOME_RATE_LIMITED: ORANGE,
    OUTCOME_TIMEOUT:      RED,
    OUTCOME_ERROR:        RED,
}

UNAVAILABLE_PHRASES = [
    "modelnotfounderror", "model not found", "requested entity was not found",
    '"code": 404', "code: 404", "404 not found", "is not a valid model",
    "unknown model", "no such model", "model does not exist",
    "it may not exist or you may not have access",       # claude CLI phrasing
    "issue with the selected model",                     # claude CLI phrasing
]
UNAUTHORIZED_PHRASES = [
    "401 unauthorized", "unauthorized", "api key", "not authenticated",
    "authentication failed", "missing api key",
]
RATE_LIMIT_PHRASES = [
    "rate limit", "429", "529", "quota exceeded", "too many requests",
    "exhausted your capacity", "resource_exhausted",
]
REFUSAL_PHRASES = [
    "no prompt was included", "please paste", "didn't come through",
    "i don't see", "your message ends", "i need more context",
]


def _vault_root():
    return Path(__file__).resolve().parent.parent


def _resolveCmd(name):
    if sys.platform == "win32":
        found = shutil.which(name + ".cmd")
        if found:
            return found
    return shutil.which(name) or name


def _classifyOutput(output, duration_seconds, returncode):
    """Categorise the probe outcome from the CLI's output."""
    if not output:
        return OUTCOME_ERROR, "no output"
    lo = output.lower()
    if any(p in lo for p in UNAVAILABLE_PHRASES):
        return OUTCOME_UNAVAILABLE, "model not found / 404"
    if any(p in lo for p in UNAUTHORIZED_PHRASES):
        return OUTCOME_UNAUTHORIZED, "401 / auth missing"
    if any(p in lo for p in RATE_LIMIT_PHRASES):
        return OUTCOME_RATE_LIMITED, "rate limited or quota exhausted"
    if any(p in lo[:200] for p in REFUSAL_PHRASES):
        return OUTCOME_REFUSAL, "refused / claimed no prompt"
    # If the model said "ok" anywhere in its first 200 chars, accept it
    if "ok" in lo[:200]:
        if duration_seconds > 30:
            return OUTCOME_SLOW, f"replied ok but took {duration_seconds:.1f}s"
        return OUTCOME_OK, f"replied ok in {duration_seconds:.1f}s"
    if returncode != 0:
        return OUTCOME_ERROR, f"exit code {returncode}"
    # Got something back, not obviously broken — probably worked
    if duration_seconds > 30:
        return OUTCOME_SLOW, f"slow ({duration_seconds:.1f}s) but responded"
    return OUTCOME_OK, f"responded in {duration_seconds:.1f}s"


def _buildCmd(provider, model):
    """Build the CLI command for a (provider, model) probe."""
    if provider == "claude":
        cmd = [_resolveCmd("claude"), "-p", PROBE_PROMPT]
        if model:
            cmd += ["--model", model]
        return cmd
    if provider == "gemini":
        cmd = [_resolveCmd("gemini"), "--yolo", "-p", PROBE_PROMPT]
        if model:
            cmd += ["--model", model]
        return cmd
    if provider == "codex":
        cmd = [_resolveCmd("codex"), "exec", "--dangerously-bypass-approvals-and-sandbox"]
        if model:
            cmd += ["--model", model]
        cmd.append(PROBE_PROMPT)
        return cmd
    return None


def _probeOne(provider, model, quiet):
    """Probe a single (provider, model). Returns dict with outcome details."""
    cmd = _buildCmd(provider, model)
    if cmd is None:
        return {"provider": provider, "model": model, "outcome": OUTCOME_ERROR,
                "detail": f"unknown provider: {provider}", "duration": 0}
    if not quiet:
        print(f"  {DIM}probing {provider}/{model}...{RESET}", flush=True)
    start = time.monotonic()
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=PROBE_TIMEOUT_SEC,
            encoding="utf-8", errors="replace",
        )
        duration = time.monotonic() - start
        output = (result.stdout or "") + (result.stderr or "")
        outcome, detail = _classifyOutput(output, duration, result.returncode)
    except subprocess.TimeoutExpired:
        duration = PROBE_TIMEOUT_SEC
        outcome, detail = OUTCOME_TIMEOUT, f"exceeded {PROBE_TIMEOUT_SEC}s timeout"
    except FileNotFoundError:
        duration = 0
        outcome, detail = OUTCOME_ERROR, f"CLI not found: {cmd[0]}"
    except Exception as e:
        duration = time.monotonic() - start
        outcome, detail = OUTCOME_ERROR, f"{type(e).__name__}: {e}"

    return {
        "provider": provider, "model": model,
        "outcome": outcome, "detail": detail,
        "duration": duration,
    }


def _logProbeToCostLog(vault_root, results):
    """Append probe results to logs/cost_log.jsonl with phase=probe."""
    log_dir = vault_root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "cost_log.jsonl"
    timestamp = datetime.now().isoformat(timespec="seconds")
    date_str  = datetime.now().strftime("%Y-%m-%d")

    # Map probe outcomes to cost_log status values that _routeModel understands
    status_map = {
        OUTCOME_OK:           "success",
        OUTCOME_SLOW:         "success",  # works but flag in detail
        OUTCOME_REFUSAL:      "refusal",
        OUTCOME_UNAVAILABLE:  "model_unavailable",
        OUTCOME_UNAUTHORIZED: "failed",
        OUTCOME_RATE_LIMITED: "rate_limited",
        OUTCOME_TIMEOUT:      "failed",
        OUTCOME_ERROR:        "failed",
    }

    with open(log_file, "a", encoding="utf-8") as f:
        for r in results:
            entry = {
                "date": date_str,
                "timestamp": timestamp,
                "task": "probe-models",
                "phase": "probe",
                "status": status_map.get(r["outcome"], "failed"),
                "provider": r["provider"],
                "model": r["model"],
                "passes": 1,
                "input_tokens": 0,
                "output_tokens": 0,
                "cost_usd": 0.0,
                "duration_seconds": round(r["duration"], 2),
                "probe_outcome": r["outcome"],
                "probe_detail": r["detail"],
            }
            f.write(json.dumps(entry) + "\n")


def _printReport(results, quiet=False):
    """Pretty-print the probe results table."""
    if not quiet:
        print()
    print(f"{BOLD}{'PROVIDER':<10} {'MODEL':<32} {'OUTCOME':<14} {'TIME':<8} DETAIL{RESET}")
    print(f"{DIM}{'-' * 90}{RESET}")
    for r in sorted(results, key=lambda x: (x["provider"], x["model"])):
        color = OUTCOME_COLORS.get(r["outcome"], RESET)
        time_s = f"{r['duration']:.1f}s" if r["duration"] else "-"
        print(f"{r['provider']:<10} {r['model']:<32} {color}{r['outcome']:<14}{RESET} {time_s:<8} {DIM}{r['detail']}{RESET}")

    # Summary
    counts = {}
    for r in results:
        counts[r["outcome"]] = counts.get(r["outcome"], 0) + 1
    print()
    summary = "  ".join(f"{OUTCOME_COLORS.get(o, RESET)}{c} {o}{RESET}" for o, c in sorted(counts.items()))
    print(f"{BOLD}Summary:{RESET}  {summary}")


def _checkNewModels(vault_root, providers):
    """Cheap metadata-only check for new models via provider APIs.

    No probing, no quota cost. Hits each provider's /v1/models endpoint and
    reports any models in the API list that aren't in model_pool.json (after
    applying the latest-only filter so you don't get spammed by every dated
    snapshot or experimental variant).

    Returns: list of (provider, new_model_name) tuples for human review.
    """
    api_discoverers = {
        "codex":  ("OpenAI",    _discoverOpenAI),
        "claude": ("Anthropic", _discoverAnthropic),
        "gemini": ("Google",    _discoverGemini),
    }
    findings = []
    for provider, (label, fn) in api_discoverers.items():
        live = fn()
        if not live:
            current = providers.get(provider, [])
            print(f"  {DIM}{label}: API unavailable (no key set or call failed) — skipping. Current pool: {current}{RESET}")
            continue
        live_set = set(live)
        # Filter the live list so the "new" suggestions are only the latest per
        # family (no snapshot spam, no superseded variants).
        filtered = _filterToLatestPerFamily(live, provider)
        current = set(providers.get(provider, []))
        new = sorted(set(filtered) - current)
        # Deprecation = pool model that's no longer in the RAW api list. A pool
        # entry that's been superseded by a newer family member is NOT deprecated
        # — the API still lists it, the filter just preferred the newer one.
        ALIASES = {"haiku", "sonnet", "opus"}  # aliases auto-resolve, never "go"
        truly_gone = sorted((current - live_set) - ALIASES)
        if new:
            print(f"  {GREEN}{label}: {len(new)} new model(s) since last check{RESET}")
            for m in new:
                print(f"    {GREEN}+{RESET} {provider}/{m}")
                findings.append((provider, m))
        else:
            print(f"  {DIM}{label}: no new models (pool has latest of each family){RESET}")
        if truly_gone:
            print(f"  {YELLOW}{label}: {len(truly_gone)} pool model(s) no longer in API listing{RESET}")
            for m in truly_gone:
                print(f"    {YELLOW}?{RESET} {provider}/{m}  {DIM}(deprecated by provider){RESET}")
    return findings


def main(argv):
    quiet = "--quiet" in argv or "-q" in argv
    discover = "--discover" in argv
    update_pool = "--update-pool" in argv
    check_new = "--check-new" in argv
    if update_pool:
        discover = True  # update implies discover
    only_provider = None
    if "--provider" in argv:
        idx = argv.index("--provider")
        if idx + 1 < len(argv):
            only_provider = argv[idx + 1]

    vault_root = _vault_root()
    pool_path  = vault_root / "model_pool.json"
    if not pool_path.exists():
        print(f"{RED}model_pool.json not found at {pool_path}{RESET}")
        return 2
    try:
        pool = json.loads(pool_path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"{RED}Failed to parse model_pool.json: {e}{RESET}")
        return 2

    providers = pool.get("providers", {})

    # --check-new: cheap metadata-only check for new models. No probing, no
    # quota cost. Use this in daily startup. Reports new models found via API
    # listings; does NOT modify model_pool.json (you decide what to add).
    if check_new:
        print(f"{BOLD}probe_models {SCRIPT_VERSION}{RESET}  {DIM}metadata-only check  •  {pool_path}{RESET}\n")
        findings = _checkNewModels(vault_root, providers)
        if findings:
            print()
            print(f"{GREEN}{BOLD}Add these to model_pool.json if you want routing to consider them:{RESET}")
            for provider, model in findings:
                print(f'  "{provider}": [..., "{model}"]')
        # Write a marker entry so the daily-once gate in run_vault.py knows
        # check-new ran today. Marker uses phase=probe so _lastProbeDate sees it.
        log_dir = vault_root / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        with open(log_dir / "cost_log.jsonl", "a", encoding="utf-8") as f:
            entry = {
                "date": datetime.now().strftime("%Y-%m-%d"),
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "task": "check-new-models",
                "phase": "probe",
                "status": "success",
                "provider": "meta",
                "model": "metadata-only",
                "passes": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cost_usd": 0.0,
                "duration_seconds": 0.0,
                "check_mode": "metadata-only",
                "new_models_found": len(findings),
            }
            f.write(json.dumps(entry) + "\n")
        return 0

    targets = []
    for provider, models in providers.items():
        if only_provider and provider != only_provider:
            continue
        if isinstance(models, list):
            for m in models:
                targets.append((provider, m))

    # Discovery mode: also probe candidate next-gen names not currently in the pool
    discovery_targets = []
    if discover:
        existing = set(targets)
        # Provider-specific authoritative API discovery (when keys are set).
        # Falls back to brute-force candidate list if API call returns nothing.
        api_discoverers = {
            "codex":  ("OpenAI",    _discoverOpenAI),
            "claude": ("Anthropic", _discoverAnthropic),
            "gemini": ("Google",    _discoverGemini),
        }
        for provider, candidates in DISCOVERY_CANDIDATES.items():
            if only_provider and provider != only_provider:
                continue
            if provider in api_discoverers:
                api_label, api_fn = api_discoverers[provider]
                live = api_fn()
                if live:
                    print(f"  {DIM}{api_label} API returned {len(live)} candidate {provider} models (authoritative){RESET}")
                    candidates = live
                else:
                    print(f"  {DIM}{api_label} API unavailable — using brute-force candidate list ({len(candidates)} names){RESET}")
            for m in candidates:
                if (provider, m) not in existing:
                    discovery_targets.append((provider, m))

    if not targets and not discovery_targets:
        print(f"{YELLOW}No models to probe (filter matched nothing).{RESET}")
        return 1

    label = f"{len(targets)} pool"
    if discovery_targets:
        label += f" + {len(discovery_targets)} candidates"
    print(f"{BOLD}probe_models {SCRIPT_VERSION}{RESET}  {DIM}{label}  •  {pool_path}{RESET}\n")

    results = [_probeOne(p, m, quiet) for p, m in targets]
    discovery_results = [_probeOne(p, m, quiet) for p, m in discovery_targets]

    # Tag discovery results so we can split them in the report and the cost log
    for r in discovery_results:
        r["is_discovery"] = True

    all_results = results + discovery_results
    _logProbeToCostLog(vault_root, all_results)
    _printReport(all_results, quiet)

    # Newly discovered working models — surface for human approval
    new_working = [r for r in discovery_results if r["outcome"] in (OUTCOME_OK, OUTCOME_SLOW)]
    if new_working:
        print()
        print(f"{GREEN}{BOLD}New models discovered (consider adding to model_pool.json):{RESET}")
        for r in new_working:
            print(f"  {GREEN}+{RESET} {r['provider']}/{r['model']}  {DIM}({r['detail']}){RESET}")

    # Suggest removals for stale unavailable models
    unavailable = [r for r in results if r["outcome"] == OUTCOME_UNAVAILABLE]
    if unavailable:
        print()
        print(f"{YELLOW}Suggested model_pool.json removals:{RESET}")
        for r in unavailable:
            print(f"  {RED}-{RESET} remove {r['provider']}/{r['model']}  {DIM}({r['detail']}){RESET}")

    # Auto-update mode: rewrite model_pool.json with the latest working models
    # per family. Combines current pool + new discoveries, drops unavailable, then
    # filters each provider to only the latest version per family.
    if update_pool:
        print()
        print(f"{CYAN}{BOLD}--update-pool: rewriting {pool_path}{RESET}")
        # For each provider, gather all working models (pool + discovery)
        provider_working = {}  # provider -> set of working model names
        for r in results + discovery_results:
            if r["outcome"] not in (OUTCOME_OK, OUTCOME_SLOW):
                continue
            provider_working.setdefault(r["provider"], set()).add(r["model"])

        # Apply latest-only filter per provider
        new_providers = {}
        for provider in sorted(set(list(providers.keys()) + list(provider_working.keys()))):
            working = sorted(provider_working.get(provider, set()))
            if working:
                filtered = _filterToLatestPerFamily(working, provider)
                new_providers[provider] = filtered
            elif provider in providers:
                # Provider had nothing working today — keep the existing config
                # so transient quota issues don't wipe the pool. Routing will
                # mark them rate_limited and deprioritise naturally.
                new_providers[provider] = providers[provider]

        from datetime import date
        updated_pool = {
            "_comment": pool.get("_comment", "Model pool per provider. Auto-updated by probe_models.py --update-pool. Position is initial test order ONLY — real quality ranking emerges from logs/cost_log.jsonl via _routeModel."),
            "_verified": date.today().isoformat(),
            "_run_to_verify": "python scripts/probe_models.py --discover",
            "_auto_update_command": "python scripts/probe_models.py --update-pool",
            "providers": new_providers,
        }
        pool_path.write_text(json.dumps(updated_pool, indent=2) + "\n", encoding="utf-8")
        print(f"  {GREEN}✓{RESET} Pool updated:")
        for prov, models in new_providers.items():
            print(f"    {prov}: {models}")

    # Exit non-zero if any pool model is unavailable or unauthorized (don't count
    # discovery candidates — they're SUPPOSED to fail when the model doesn't exist)
    bad = [r for r in results if r["outcome"] in (OUTCOME_UNAVAILABLE, OUTCOME_UNAUTHORIZED, OUTCOME_TIMEOUT, OUTCOME_ERROR)]
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
