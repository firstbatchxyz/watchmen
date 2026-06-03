"""Project/path/skill helpers shared across cli.py and watchmen.commands.*

Extracted from cli.py during the Phase 3 split so command modules can
import these without circular dependencies on cli.py. Most callers in
cli.py keep the `_name` alias convention for source-stability across the
mechanical move.

Design rules:
  - Side-effect-free where possible; pure path math returns Paths.
  - DB reads accept a CORPUS_DB path so tests can swap the location.
  - No imports from cli.py, ui.py, or commands.*  — strictly leaf utilities.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from watchmen import state
from watchmen.paths import ANALYSES_DIR, BUNDLES_DIR, CORPUS_DB, repo_dir_sql_predicate


# ─── Test-aware path roots ──────────────────────────────────────────────────
# These mirror the cli.py `ROOT != SOURCE_ROOT` gates. Tests monkey-patch
# `cli.ROOT` to a temp dir; this module reads cli.ROOT/cli.SOURCE_ROOT
# lazily through a getter so the test patch flows through here too.


def _cli_root_pair() -> tuple[Path, Path]:
    """Return (ROOT, SOURCE_ROOT) from cli.py at call time, NOT import time.

    The lazy import is deliberate — importing watchmen.cli at module load
    would create a cycle (cli.py imports from this module). Doing it at
    call time means the lookup happens after both modules are constructed
    AND lets tests overwrite cli.ROOT after import.
    """
    from watchmen import cli
    return cli.ROOT, cli.SOURCE_ROOT


def bundle_dir(project_key: str) -> Path:
    return bundle_base() / project_key


def bundle_base() -> Path:
    """Tests override cli.ROOT to a temp dir; fall through to the canonical
    WATCHMEN_HOME/bundles/ via paths.BUNDLES_DIR when not overridden."""
    root, source_root = _cli_root_pair()
    return root / "bundles" if root != source_root else BUNDLES_DIR


def analyses_base() -> Path:
    root, source_root = _cli_root_pair()
    return root / "analyses" if root != source_root else ANALYSES_DIR


def corpus_db_path() -> Path:
    root, source_root = _cli_root_pair()
    return root / "corpus.db" if root != source_root else CORPUS_DB


# ─── Project metadata ──────────────────────────────────────────────────────


def tracked_source_repo(project_key: str) -> str | None:
    proj = state.get_project(project_key)
    return proj.get("source_repo") if proj else None


def project_dir_predicate(project_key: str, alias: str = "s") -> tuple[str, tuple[str | int, ...]] | None:
    """SQL predicate that selects sessions inside a tracked project's repo.

    Returns (where_clause, params) for substituting into a SELECT, or None
    if the project isn't tracked. Covers both an exact project_dir match and
    child-dir matches (sessions opened from a subfolder), cross-platform.
    """
    source_repo = tracked_source_repo(project_key)
    if not source_repo:
        return None
    return repo_dir_sql_predicate(source_repo, alias)


def tracked_project_keys() -> list[str]:
    """Project keys that have at least a `bundles/<key>/` dir on disk —
    used as the universe for `show` and `recent` without a project arg.
    Falls back to state.list_projects() when nothing is on disk yet."""
    base = bundle_base()
    if base.exists():
        keys = sorted(d.name for d in base.iterdir() if d.is_dir() and (d / "skills").exists())
        if keys:
            return keys
    return [p["project_key"] for p in state.list_projects()]


# ─── Adapter mix + friction signals (corpus.db reads) ──────────────────────


ADAPTER_SHORT = {"claude_code": "cc", "codex": "cd", "pi": "pi"}


def adapter_breakdown(project_key: str) -> dict[str, int]:
    """Session counts per adapter from corpus.db, filtered to substantive
    non-subagent sessions matching the project path."""
    db = corpus_db_path()
    if not db.exists():
        return {}
    pred = project_dir_predicate(project_key)
    if not pred:
        return {}
    where, params = pred
    cc = sqlite3.connect(db)
    rows = cc.execute(
        f"""SELECT agent, COUNT(*) FROM sessions s
            WHERE {where} AND is_subagent = 0
            GROUP BY agent""",
        params,
    ).fetchall()
    cc.close()
    return {agent: n for agent, n in rows}


def format_adapter_count(breakdown: dict[str, int]) -> str:
    """Compact `2053 cc · 417 cd · 0 pi` style line. Always shows all 3 adapters
    so the row width is stable, even when projects don't have sessions in
    every adapter yet."""
    parts = []
    for agent in ("claude_code", "codex", "pi"):
        n = breakdown.get(agent, 0)
        parts.append(f"{n:>4} {ADAPTER_SHORT[agent]}")
    return " · ".join(parts)


# Frustration-marker regex over the prompts table. Coarser than Anthropic's
# LLM-inferred satisfaction but cheap and traceable to actual user prompts.
# Used by both watchmen insights and the per-repo summary in the viewer.
FRUSTRATION_MARKERS_SQL = (
    # Case-sensitive markers that would create false positives if lowercased.
    "p.text LIKE '%:(%' "
    # Case-insensitive — phrase fragments common in genuine frustration.
    "OR LOWER(p.text) LIKE '%no wait%' "
    "OR LOWER(p.text) LIKE '%bruh%' "
    "OR LOWER(p.text) LIKE '%nope%' "
    "OR LOWER(p.text) LIKE '%dammit%' "
    "OR LOWER(p.text) LIKE '%wtf%' "
    "OR LOWER(p.text) LIKE '%just stop%' "
    "OR LOWER(p.text) LIKE '%still?%' "
    "OR LOWER(p.text) LIKE '%ugh,%' "
    "OR LOWER(p.text) LIKE '%fuck,%' "
    "OR LOWER(p.text) LIKE '%fucking%'"
)


def repo_friction_signals(project_key: str) -> tuple[int, list[tuple[str, int]], int, list[str]]:
    """Pull tool-error totals + frustration-marker matches per repo from
    corpus.db. Returns (total_tool_errors, [(tool, n), …] top-3 erroring
    tools, frustration_prompt_count, [sample_text, …] first 2 samples).

    Two signals that `/insights` surfaces as charts: tool-error counts
    and inferred-satisfaction histograms. We approximate satisfaction
    via a regex over the prompts table (frustration markers ≈ negative
    satisfaction) — coarser than Anthropic's LLM-inferred satisfaction
    but cheap and traceable to actual user prompts."""
    db = corpus_db_path()
    if not db.exists():
        return 0, [], 0, []
    pred = project_dir_predicate(project_key)
    if not pred:
        return 0, [], 0, []
    where, params = pred
    cc = sqlite3.connect(db)
    err_total = cc.execute(
        f"""SELECT COALESCE(SUM(tool_error_count), 0) FROM sessions s
            WHERE {where} AND is_subagent = 0""",
        params,
    ).fetchone()[0] or 0
    top_tools = cc.execute(
        f"""SELECT tc.tool_name, COUNT(*) AS n
            FROM tool_calls tc JOIN sessions s ON s.session_id = tc.session_id
            WHERE {where} AND s.is_subagent = 0 AND tc.is_error = 1
            GROUP BY tc.tool_name ORDER BY n DESC LIMIT 3""",
        params,
    ).fetchall()
    frust_count = cc.execute(
        f"""SELECT COUNT(*) FROM prompts p
            JOIN sessions s ON s.session_id = p.session_id
            WHERE {where} AND s.is_subagent = 0
              AND ({FRUSTRATION_MARKERS_SQL})""",
        params,
    ).fetchone()[0] or 0
    samples = [
        row[0][:120].replace("\n", " ").strip()
        for row in cc.execute(
            f"""SELECT substr(p.text, 1, 160) FROM prompts p
                JOIN sessions s ON s.session_id = p.session_id
                WHERE {where} AND s.is_subagent = 0
                  AND ({FRUSTRATION_MARKERS_SQL})
                  AND p.text NOT LIKE '%Image%'
                ORDER BY p.timestamp DESC LIMIT 2""",
            params,
        ).fetchall()
    ]
    cc.close()
    return err_total, list(top_tools), frust_count, samples


# ─── Skill list I/O (pin/blocklist files) ──────────────────────────────────


PINNED_FILE = "_pinned.json"
BLOCKLIST_FILE = "_blocklist.json"


def read_skill_list(project: str, filename: str) -> set[str]:
    """Load a JSON list of skill slugs from bundles/<project>/<filename>.
    Empty/missing/invalid → empty set."""
    p = bundle_dir(project) / filename
    if not p.exists():
        return set()
    try:
        return set(json.loads(p.read_text()))
    except Exception:
        return set()


def write_skill_list(project: str, filename: str, values: set[str]) -> Path:
    """Persist a sorted JSON list of slugs back to the project dir. When the
    list becomes empty (last unpin or restore), delete the file instead of
    leaving an empty `[]` behind — keeps the bundle dir tidy."""
    proj_dir = bundle_dir(project)
    proj_dir.mkdir(parents=True, exist_ok=True)
    p = proj_dir / filename
    if not values:
        if p.exists():
            p.unlink()
        return p
    p.write_text(json.dumps(sorted(values), indent=2) + "\n")
    return p


def resolve_skill_slug(project: str, target: str) -> str | None:
    """Find the canonical slug for a user-supplied skill identifier. Accepts:
      - exact slug matching bundles/<project>/skills/<slug>/
      - display name from _candidates.json (case-insensitive)
    Returns None if neither matches — the caller is expected to suggest
    available slugs in that case."""
    proj_dir = bundle_dir(project)
    skills_dir = proj_dir / "skills"
    if skills_dir.exists() and (skills_dir / target).is_dir():
        return target
    cands_path = proj_dir / "_candidates.json"
    if cands_path.exists():
        try:
            cands = json.loads(cands_path.read_text())
        except Exception:
            cands = []
        for c in cands:
            if c.get("slug") == target or c.get("name", "").lower() == target.lower():
                return c.get("slug")
    return None


def available_skills(project: str) -> list[str]:
    """List slugs present on disk — used to suggest valid options on miss."""
    skills_dir = bundle_dir(project) / "skills"
    if not skills_dir.exists():
        return []
    return sorted(d.name for d in skills_dir.iterdir() if d.is_dir())


# ─── CHANGELOG.md discovery ────────────────────────────────────────────────


def find_changelog() -> Path | None:
    """Return the CHANGELOG.md path, or None if it isn't bundled.

    Installed wheel layout: force-included at watchmen/CHANGELOG.md → sits
    next to cli.py. Source-checkout layout: lives at the repo root, two
    parents above src/watchmen/. Both `watchmen changelog` (interactive
    render) and the version-bump notification on first run after a pull
    use this lookup.
    """
    # Import lazily to dodge a cli → util cycle; this matches bundle_base
    # — see _cli_root_pair above for why.
    from watchmen import cli
    for candidate in (cli.ROOT / "CHANGELOG.md", cli.ROOT.parents[1] / "CHANGELOG.md"):
        if candidate.exists():
            return candidate
    return None


# ─── Run-failure classification ─────────────────────────────────────────────
# A failed curator/analyst subprocess only hands the daemon an exit code. The
# *reason* (provider rate-limit, OAuth expiry) lives in the captured output and
# the bundle _run.log. classify_failure() distills that into a concise
# runs.notes string so a wall of red rows reads as "blocked on quota" instead
# of an identical "exit 1". Pure + text-only so it's trivially testable.

# Substrings that map a provider's endpoint/label to a short name. Ordered:
# first match wins, so put more-specific hosts before generic vendor names.
_PROVIDER_HINTS: tuple[tuple[str, str], ...] = (
    ("chatgpt.com", "chatgpt"),
    ("openrouter", "openrouter"),
    ("api.anthropic.com", "claude-pro"),
    ("claude pro", "claude-pro"),
    ("claude-pro", "claude-pro"),
    ("anthropic", "claude-pro"),
)

_RATE_LIMIT_MARKERS = (
    "http 429",
    "429 too many requests",
    "too many requests",
    "rate limit",
    "rate_limit",
)

_AUTH_MARKERS = (
    "oauth token expired",
    "http 401",
    "401 unauthorized",
    "invalid_api_key",
    "invalid api key",
)


def _detect_provider(output: str) -> str:
    """Best-effort provider name from captured output: prefer the explicit
    retry-log label (``chatgpt: HTTP 429``), fall back to endpoint host hints,
    else a generic ``provider``."""
    m = re.search(r"([A-Za-z][\w.-]*): HTTP \d{3}", output)
    if m:
        return m.group(1)
    low = output.lower()
    for needle, name in _PROVIDER_HINTS:
        if needle in low:
            return name
    return "provider"


def classify_failure(output: str, returncode: int) -> str:
    """Map a failed run's captured output + exit code to a concise notes string.

    Returns ``rate_limit: <provider>`` when the output carries a 429 / rate-limit
    signature, ``auth: <provider>`` for OAuth expiry / 401, else ``exit <code>``.
    The provider families (chatgpt subscription, claude-pro OAuth) share this
    shape, so one classifier covers both.
    """
    low = output.lower()
    if any(m in low for m in _RATE_LIMIT_MARKERS):
        return f"rate_limit: {_detect_provider(output)}"
    if any(m in low for m in _AUTH_MARKERS):
        return f"auth: {_detect_provider(output)}"
    return f"exit {returncode}"


def classify_run_failure(project_key: str, returncode: int, extra_output: str = "") -> str:
    """`classify_failure` plus the tail of the project's bundle ``_run.log``.

    The curator writes per-attempt ``<provider>: HTTP 429`` breadcrumbs to that
    log, so folding it in lets callers that don't capture subprocess output
    (the interactive CLI) still classify the failure. ``extra_output`` carries
    captured stderr/stdout when the caller has it (the daemon)."""
    parts = [extra_output]
    run_log = BUNDLES_DIR / project_key / "_run.log"
    if run_log.exists():
        try:
            parts.append(run_log.read_text(encoding="utf-8", errors="replace")[-4000:])
        except OSError:
            pass
    return classify_failure("\n".join(p for p in parts if p), returncode)
