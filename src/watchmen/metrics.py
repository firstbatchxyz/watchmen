"""Daily efficiency metrics for a tracked project.

Joins three data sources to build per-day rollups for the metrics viewer + CLI:

  - corpus.db.sessions     → sessions, prompts, tool errors, token usage by date
  - ~/.watchmen/suggestions.jsonl → suggestions fired, with session_id
  - prompts table          → uptake detection (did /<skill> appear in a later
                              prompt within the same session within 1 hour?)

Cost is computed from model→price table. Prices are fetched from OpenRouter
API (https://openrouter.ai/api/v1/models) and cached locally.
Falls back to hardcoded defaults if API is unavailable.
"""

import json
import re
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable

from watchmen.paths import CORPUS_DB, STATE_DB
from watchmen.model_prices import price_for_model as price_for_model_from_api, turn_cost_usd as turn_cost_usd_from_api

ROOT = Path(__file__).parent
SUGGESTIONS_LOG = Path.home() / ".watchmen" / "suggestions.jsonl"

# Friendly labels for the `agent` column in corpus.db. The DB stores adapter
# slugs (claude_code, codex, pi); UI surfaces want display names. Fall through
# to the raw slug for unknown adapters so future ones still render.
ADAPTER_LABELS = {
    "claude_code": "Claude Code",
    "codex": "Codex",
    "pi": "pi.dev",
}


def adapter_label(slug: str) -> str:
    return ADAPTER_LABELS.get(slug, slug)


def _repo_label(project_dir: str) -> str:
    """Human label for a repo row — the trailing path segment (the repo's
    own folder name), which is what the user actually thinks of it as. Falls
    back to the raw value if there's no separator."""
    if not project_dir:
        return "(unknown)"
    return project_dir.rstrip("/").rsplit("/", 1)[-1] or project_dir


# Per-agent colours, matching agent_donut_svg's blue/cyan/teal trio so the
# swimlane lanes read consistently with the rest of the page.
AGENT_COLORS = {"claude_code": "#3b82f6", "codex": "#06b6d4", "pi": "#14b8a6"}
_AGENT_FALLBACK = ["#10b981", "#f59e0b", "#ef4444", "#8b5cf6", "#ec4899"]


def _agent_color(slug: str, idx: int = 0) -> str:
    return AGENT_COLORS.get(slug) or _AGENT_FALLBACK[idx % len(_AGENT_FALLBACK)]

# Default price used when model is unknown (matches sonnet-4.6 hardcoded fallback)
DEFAULT_PRICE = (3.00, 3.75, 6.00, 0.30, 15.00)

# Backward-compatible alias for tests (hardcoded fallback prices)
MODEL_PRICES = {
    "opus-4.7": (5.00, 6.25, 10.00, 0.50, 25.00),
    "opus-4.6": (5.00, 6.25, 10.00, 0.50, 25.00),
    "opus-4.5": (5.00, 6.25, 10.00, 0.50, 25.00),
    "opus-4.1": (15.00, 18.75, 30.00, 1.50, 75.00),
    "opus-4": (15.00, 18.75, 30.00, 1.50, 75.00),
    "sonnet-4.6": (3.00, 3.75, 6.00, 0.30, 15.00),
    "sonnet-4.5": (3.00, 3.75, 6.00, 0.30, 15.00),
    "sonnet-4": (3.00, 3.75, 6.00, 0.30, 15.00),
    "haiku-4.5": (1.00, 1.25, 2.00, 0.10, 5.00),
    "haiku-3.5": (0.80, 1.00, 1.60, 0.08, 4.00),
    "gpt-5.5": (1.25, 1.25, 1.25, 0.125, 10.00),
    "gpt-5.4": (1.25, 1.25, 1.25, 0.125, 10.00),
    "gpt-5-mini": (0.25, 0.25, 0.25, 0.025, 2.00),
    "gpt-5": (1.25, 1.25, 1.25, 0.125, 10.00),
    "gpt-4.1": (2.00, 2.00, 2.00, 0.500, 8.00),
    "gpt-4o": (2.50, 2.50, 2.50, 1.250, 10.00),
    "o3": (2.00, 2.00, 2.00, 0.500, 8.00),
    "o4-mini": (1.10, 1.10, 1.10, 0.275, 4.40),
}


_VERSION_DASH = re.compile(r"(opus|sonnet|haiku)-(\d+)-(\d+)\b")
# GPT model names use dots too (gpt-5.5, gpt-4.1) but API also returns dash forms
# in some cases (gpt-5-5-mini). Same normalizer pattern:
_GPT_DASH = re.compile(r"gpt-(\d+)-(\d+)\b")


def price_for_model(model: str | None) -> tuple[float, float, float, float, float]:
    """Get price for a model. Uses model_prices module which fetches from OpenRouter
    API and caches locally. Falls back to family-based matching if not found.
    Same normalization as before for compatibility."""
    return price_for_model_from_api(model)


def turn_cost_usd(
    model: str | None,
    input_tokens: int,
    cache_creation_5m: int,
    cache_creation_1h: int,
    cache_read: int,
    output_tokens: int,
) -> float:
    """Cost for one assistant turn, in USD. Per-turn attribution means we use
    THIS turn's model — not the session's dominant model — which matters when
    a session spans multiple models (e.g. Opus for planning, Sonnet for grunt
    work)."""
    return turn_cost_usd_from_api(
        model,
        input_tokens,
        cache_creation_5m,
        cache_creation_1h,
        cache_read,
        output_tokens,
    )


def _project_dir_for_key(project_key: str) -> str | None:
    """Map a project_key to its corpus.db project_dir.

    Post-normalization, all adapters store the real cwd (matching state.db's
    source_repo verbatim), so this is just a lookup. Returns None if the
    project isn't tracked or the state DB doesn't exist yet."""
    if not CORPUS_DB.exists():
        return None
    if not STATE_DB.exists():
        return None
    try:
        with sqlite3.connect(str(STATE_DB)) as conn:
            row = conn.execute(
                "SELECT source_repo FROM projects WHERE project_key = ?", (project_key,)
            ).fetchone()
    except sqlite3.Error:
        return None
    if not row or not row[0]:
        return None
    return row[0]


def _local_date(iso_ts: str | None) -> str | None:
    if not iso_ts:
        return None
    try:
        # corpus stores ISO 8601 in UTC (with Z suffix sometimes)
        dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        return dt.astimezone().date().isoformat()
    except (ValueError, TypeError):
        return None


def daily_metrics(project_key: str, days: int = 30) -> list[dict]:
    """Return list of dicts, one per day in the last `days` days (newest first).
    Days with no activity still appear with zeroed counters."""
    project_dir = _project_dir_for_key(project_key)
    if not project_dir or not CORPUS_DB.exists():
        return []

    # 1. Pull sessions for this project from corpus, group by local date.
    with sqlite3.connect(str(CORPUS_DB)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT session_id, started_at, ended_at, user_prompt_count,
                      tool_error_count, input_tokens, cache_creation_tokens,
                      cache_read_tokens, output_tokens, model_dominant, cost_usd
               FROM sessions
               WHERE project_dir = ? AND is_subagent = 0""",
            (project_dir,),
        ).fetchall()

    by_day: dict[str, dict] = {}
    for r in rows:
        d_str = _local_date(r["started_at"])
        if not d_str:
            continue
        bucket = by_day.setdefault(d_str, _empty_bucket(d_str))
        bucket["sessions"] += 1
        bucket["prompts"] += r["user_prompt_count"] or 0
        bucket["tool_errors"] += r["tool_error_count"] or 0
        bucket["input_tokens"] += r["input_tokens"] or 0
        bucket["cache_creation_tokens"] += r["cache_creation_tokens"] or 0
        bucket["cache_read_tokens"] += r["cache_read_tokens"] or 0
        bucket["output_tokens"] += r["output_tokens"] or 0
        # cost is per-turn-summed at scan time; use the stored value if present.
        bucket["cost_usd"] += (r["cost_usd"] if r["cost_usd"] is not None else 0.0)

    # 2. Pull suggestions for this project from the log, group by local date.
    suggestions = _load_suggestions(project_key)
    for s in suggestions:
        d_str = _local_date(s["ts"])
        if not d_str:
            continue
        bucket = by_day.setdefault(d_str, _empty_bucket(d_str))
        bucket["suggestions_fired"] += 1
        bucket["_suggestion_records"].append(s)

    # 3. Compute uptake per bucket.
    for bucket in by_day.values():
        recs = bucket.pop("_suggestion_records", [])
        if recs:
            bucket["uptake"] = _count_uptake(recs)
            bucket["uptake_rate"] = (
                bucket["uptake"] / bucket["suggestions_fired"]
                if bucket["suggestions_fired"] > 0
                else 0.0
            )

    # 4. Backfill any missing days in the window with zeros so charts look right.
    today = date.today()
    cutoff = today - timedelta(days=days - 1)
    out: list[dict] = []
    for d in (cutoff + timedelta(days=i) for i in range(days)):
        d_str = d.isoformat()
        out.append(by_day.get(d_str, _empty_bucket(d_str)))
    out.sort(key=lambda b: b["date"], reverse=True)
    return out


def _empty_bucket(d_str: str) -> dict:
    return {
        "date": d_str,
        "sessions": 0,
        "prompts": 0,
        "tool_errors": 0,
        "input_tokens": 0,
        "cache_creation_tokens": 0,
        "cache_read_tokens": 0,
        "output_tokens": 0,
        "cost_usd": 0.0,
        "suggestions_fired": 0,
        "uptake": 0,
        "uptake_rate": 0.0,
        "_suggestion_records": [],
    }


def _load_suggestions(project_key: str) -> list[dict]:
    if not SUGGESTIONS_LOG.exists():
        return []
    out = []
    with SUGGESTIONS_LOG.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("project_key") == project_key:
                out.append(rec)
    return out


def _skill_name_matches(skill_name: str, slug: str) -> bool:
    """A `tool_calls.skill_name` matches a suggestion slug if it equals the slug
    or equals it after stripping a namespace prefix (e.g. `watchmen:brief` and
    `figma:figma-use` match `brief` and `figma-use`)."""
    return skill_name == slug or skill_name.split(":")[-1] == slug


def _count_uptake(suggestions: list[dict]) -> int:
    """For each suggestion, count it as 'taken' if, in the same session and
    within 1 hour after the suggestion, the suggested skill was either:

      - invoked as a slash command in a later prompt (e.g. ``/<slug>``), or
      - auto-fired by the agent — captured as a ``tool_calls`` row with
        ``tool_name = 'Skill'`` and ``skill_name`` matching the slug.

    Skills activate by context match far more often than by explicit slash
    command, so counting only the prompt channel undercounts real uptake."""
    if not suggestions or not CORPUS_DB.exists():
        return 0

    session_ids = {s["session_id"] for s in suggestions if s.get("session_id")}
    if not session_ids:
        return 0

    placeholders = ",".join("?" for _ in session_ids)
    by_session_prompts: dict[str, list[tuple[str, str]]] = {}
    by_session_skills: dict[str, list[tuple[str, str]]] = {}
    with sqlite3.connect(str(CORPUS_DB)) as conn:
        conn.row_factory = sqlite3.Row
        for r in conn.execute(
            f"SELECT session_id, timestamp, text FROM prompts "
            f"WHERE session_id IN ({placeholders}) ORDER BY timestamp ASC",
            tuple(session_ids),
        ):
            by_session_prompts.setdefault(r["session_id"], []).append(
                (r["timestamp"], r["text"] or "")
            )
        for r in conn.execute(
            f"SELECT session_id, timestamp, skill_name FROM tool_calls "
            f"WHERE session_id IN ({placeholders}) "
            f"AND skill_name IS NOT NULL AND skill_name != '' "
            f"ORDER BY timestamp ASC",
            tuple(session_ids),
        ):
            by_session_skills.setdefault(r["session_id"], []).append(
                (r["timestamp"], r["skill_name"])
            )

    taken = 0
    for s in suggestions:
        sid = s.get("session_id")
        slug = s.get("skill_slug")
        ts_str = s.get("ts")
        if not (sid and slug and ts_str):
            continue
        try:
            ts = datetime.fromisoformat(ts_str)
        except ValueError:
            continue
        if _suggestion_taken(sid, slug, ts, by_session_prompts, by_session_skills):
            taken += 1
    return taken


def _suggestion_taken(
    sid: str,
    slug: str,
    ts: datetime,
    by_session_prompts: dict[str, list[tuple[str, str]]],
    by_session_skills: dict[str, list[tuple[str, str]]],
) -> bool:
    """True if `slug` was invoked as a slash command or auto-fired as a Skill
    tool call in session `sid` within 1 hour after `ts`."""
    pattern = re.compile(rf"/(?:watchmen:)?{re.escape(slug)}\b")
    for p_ts_str, p_text in by_session_prompts.get(sid, []):
        p_ts = _parse_any_ts(p_ts_str)
        if not p_ts or p_ts <= ts:
            continue
        if (p_ts - ts).total_seconds() > 3600:
            continue
        if pattern.search(p_text):
            return True
    for c_ts_str, skill_name in by_session_skills.get(sid, []):
        c_ts = _parse_any_ts(c_ts_str)
        if not c_ts or c_ts <= ts:
            continue
        if (c_ts - ts).total_seconds() > 3600:
            continue
        if _skill_name_matches(skill_name, slug):
            return True
    return False


def _parse_any_ts(s: str | None):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
    except (ValueError, TypeError):
        return None


def summarize_window(rows: list[dict], days: int) -> dict:
    """Roll up a slice of the daily rows into an N-day total."""
    slice_ = rows[:days]
    return {
        "days": days,
        "sessions": sum(r["sessions"] for r in slice_),
        "prompts": sum(r["prompts"] for r in slice_),
        "tool_errors": sum(r["tool_errors"] for r in slice_),
        "input_tokens": sum(r["input_tokens"] for r in slice_),
        "cache_creation_tokens": sum(r["cache_creation_tokens"] for r in slice_),
        "cache_read_tokens": sum(r["cache_read_tokens"] for r in slice_),
        "output_tokens": sum(r["output_tokens"] for r in slice_),
        "cost_usd": sum(r["cost_usd"] for r in slice_),
        "suggestions_fired": sum(r["suggestions_fired"] for r in slice_),
        "uptake": sum(r["uptake"] for r in slice_),
    }


def _tracked_project_dirs() -> list[str]:
    """Real-path project_dirs for every tracked project. Used to filter the
    aggregated views down to 'tracked only' when the toggle is on. Matches
    the post-normalization adapter convention (real cwd, not encoded)."""
    if not STATE_DB.exists():
        return []
    try:
        with sqlite3.connect(str(STATE_DB)) as conn:
            rows = conn.execute(
                "SELECT source_repo FROM projects WHERE source_repo IS NOT NULL"
            ).fetchall()
    except sqlite3.Error:
        return []
    return [r[0] for r in rows if r[0]]


def daily_metrics_all(days: int = 30, tracked_only: bool = False) -> list[dict]:
    """Aggregated daily metrics across every is_subagent=0 session in the corpus.
    Set tracked_only=True to restrict to projects in state.db. Suggestions come
    from the log (which only has tracked projects, so they're unchanged either way)."""
    today = date.today()
    cutoff = today - timedelta(days=days - 1)
    by_day: dict[str, dict] = {}
    for i in range(days):
        d_str = (cutoff + timedelta(days=i)).isoformat()
        by_day[d_str] = _empty_bucket(d_str)

    if CORPUS_DB.exists():
        with sqlite3.connect(str(CORPUS_DB)) as conn:
            conn.row_factory = sqlite3.Row
            base_sql = """SELECT session_id, started_at, user_prompt_count, tool_error_count,
                                  input_tokens, cache_creation_tokens, cache_read_tokens,
                                  output_tokens, model_dominant, cost_usd
                           FROM sessions
                           WHERE is_subagent = 0
                             AND date(started_at, 'localtime') >= ?"""
            params: list = [cutoff.isoformat()]
            if tracked_only:
                tracked_dirs = _tracked_project_dirs()
                if not tracked_dirs:
                    rows = []
                else:
                    placeholders = ",".join("?" for _ in tracked_dirs)
                    base_sql += f" AND project_dir IN ({placeholders})"
                    params.extend(tracked_dirs)
                    rows = conn.execute(base_sql, params).fetchall()
            else:
                rows = conn.execute(base_sql, params).fetchall()
        for r in rows:
            d_str = _local_date(r["started_at"])
            if not d_str or d_str not in by_day:
                continue
            b = by_day[d_str]
            b["sessions"] += 1
            b["prompts"] += r["user_prompt_count"] or 0
            b["tool_errors"] += r["tool_error_count"] or 0
            b["input_tokens"] += r["input_tokens"] or 0
            b["cache_creation_tokens"] += r["cache_creation_tokens"] or 0
            b["cache_read_tokens"] += r["cache_read_tokens"] or 0
            b["output_tokens"] += r["output_tokens"] or 0
            b["cost_usd"] += (r["cost_usd"] if r["cost_usd"] is not None else 0.0)

    # Suggestions log — already partitioned by project_key, but we sum all.
    if SUGGESTIONS_LOG.exists():
        with SUGGESTIONS_LOG.open() as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                d_str = _local_date(rec.get("ts"))
                if not d_str or d_str not in by_day:
                    continue
                by_day[d_str]["suggestions_fired"] += 1
                by_day[d_str]["_suggestion_records"].append(rec)
        for b in by_day.values():
            recs = b.pop("_suggestion_records", [])
            if recs:
                b["uptake"] = _count_uptake(recs)
                b["uptake_rate"] = (
                    b["uptake"] / b["suggestions_fired"] if b["suggestions_fired"] > 0 else 0.0
                )

    for b in by_day.values():
        b.pop("_suggestion_records", None)

    out = list(by_day.values())
    out.sort(key=lambda b: b["date"], reverse=True)
    return out


def adapter_breakdown_all(days: int = 30, tracked_only: bool = False) -> list[dict]:
    """Per-coding-agent rollup over the last `days` calendar days.

    Returns one row per `agent` value in corpus.db.sessions, ordered by
    session count desc. Surfaces are the viewer's /metrics page and the
    CLI's `By coding agent` table.

    Note: distinct from `util.adapter_breakdown(project_key)`, which does a
    per-project quick lookup. This is the global cross-project rollup with
    cost / errors / projects-touched alongside session counts.

    Empty list when corpus.db doesn't exist yet or no sessions in window.
    """
    if not CORPUS_DB.exists():
        return []
    cutoff = date.today() - timedelta(days=days - 1)
    base_sql = """
        SELECT agent,
               COUNT(*)                       AS sessions,
               COUNT(DISTINCT project_dir)    AS projects,
               COALESCE(SUM(user_prompt_count), 0)     AS prompts,
               COALESCE(SUM(tool_error_count), 0)      AS tool_errors,
               COALESCE(SUM(input_tokens), 0)          AS input_tokens,
               COALESCE(SUM(cache_creation_tokens), 0) AS cache_creation_tokens,
               COALESCE(SUM(cache_read_tokens), 0)     AS cache_read_tokens,
               COALESCE(SUM(output_tokens), 0)         AS output_tokens,
               COALESCE(SUM(cost_usd), 0.0)            AS cost_usd
          FROM sessions
         WHERE is_subagent = 0
           AND date(started_at, 'localtime') >= ?
    """
    params: list = [cutoff.isoformat()]
    if tracked_only:
        tracked_dirs = _tracked_project_dirs()
        if not tracked_dirs:
            return []
        placeholders = ",".join("?" for _ in tracked_dirs)
        base_sql += f" AND project_dir IN ({placeholders})"
        params.extend(tracked_dirs)
    base_sql += " GROUP BY agent ORDER BY sessions DESC, agent ASC"
    out: list[dict] = []
    with sqlite3.connect(str(CORPUS_DB)) as conn:
        conn.row_factory = sqlite3.Row
        for r in conn.execute(base_sql, params).fetchall():
            out.append({
                "agent": r["agent"],
                "label": adapter_label(r["agent"]),
                "sessions": r["sessions"],
                "projects": r["projects"],
                "prompts": r["prompts"],
                "tool_errors": r["tool_errors"],
                "input_tokens": r["input_tokens"],
                "cache_creation_tokens": r["cache_creation_tokens"],
                "cache_read_tokens": r["cache_read_tokens"],
                "output_tokens": r["output_tokens"],
                "cost_usd": r["cost_usd"],
            })
    return out


# Metrics the work matrix can shade/sort by. `cell_key` is the per-cell field
# the colour ramp normalizes; `row_sort` is the per-repo total used to rank
# rows (errors sorts by absolute count, not rate, so a 1-session 100%-error
# repo doesn't float to the top on noise).
MATRIX_METRICS = ("sessions", "cost", "tokens", "errors")
_MATRIX_META = {
    "sessions": {"label": "sessions", "cell_key": "sessions",   "row_sort": "sessions"},
    "cost":     {"label": "cost",     "cell_key": "cost_usd",   "row_sort": "cost_usd"},
    "tokens":   {"label": "tokens",   "cell_key": "tokens",     "row_sort": "tokens"},
    "errors":   {"label": "error rate", "cell_key": "error_rate", "row_sort": "tool_errors"},
}


def _matrix_primary_label(metric: str, cell: dict) -> str:
    if metric == "cost":
        return f"${cell['cost_usd']:,.2f}"
    if metric == "tokens":
        return _format_kn(cell["tokens"])
    if metric == "errors":
        return f"{cell['error_rate']:.0%}"
    return f"{cell['sessions']:,}"


def work_matrix(
    days: int = 90, tracked_only: bool = False, top_repos: int = 20, metric: str = "sessions"
) -> dict:
    """Repo × agent grid: where the user's work actually happens and where
    each agent struggles. Rows are repos, columns are the agents that have
    sessions in the window. Each cell carries sessions / cost / tokens /
    error-rate; `metric` picks which one drives the colour ramp, the headline
    number, and the row ranking. Columns stay ordered by session volume.

    `metric` ∈ MATRIX_METRICS ("sessions", "cost", "tokens", "errors");
    anything else falls back to "sessions".

    Shape (JSON-able, ready for a server-rendered table):
        {
          "window_days": 90, "since": "2026-03-04",
          "metric": "cost", "metric_label": "cost",
          "agents": ["claude_code", ...], "agent_labels": {...},  # columns
          "rows": [
            {"repo": "/abs/path", "label": "kai", "total_label": "$5.40",
             "cells": {"claude_code": {sessions, cost_usd, tokens, tool_errors,
                       tool_calls, error_rate, intensity, primary_label,
                       secondary_label, secondary_hot}, "codex": None, ...}}],
          "scale": {"sessions_max": 142, "value_max": <max of active metric>},
          "repos_total": 27, "repos_shown": 20,        # truncation, surfaced
        }

    `intensity` is the active metric's cell value / its max, in [0,1] — the
    template multiplies it into a cell background. `secondary_label` is the
    error rate (or session count, when the metric *is* errors); `secondary_hot`
    flags an error rate ≥10%. Never raises; missing corpus.db → empty grid."""
    if metric not in _MATRIX_META:
        metric = "sessions"
    meta = _MATRIX_META[metric]
    cell_key, row_sort = meta["cell_key"], meta["row_sort"]

    out: dict = {
        "window_days": days, "since": None, "metric": metric, "metric_label": meta["label"],
        "agents": [], "agent_labels": {}, "rows": [],
        "scale": {"sessions_max": 0, "value_max": 0}, "repos_total": 0, "repos_shown": 0,
    }
    if not CORPUS_DB.exists():
        return out

    cutoff = date.today() - timedelta(days=days - 1)
    out["since"] = cutoff.isoformat()
    params: list = [cutoff.isoformat()]
    tracked_filter = ""
    if tracked_only:
        tracked_dirs = _tracked_project_dirs()
        if not tracked_dirs:
            return out
        ph = ",".join("?" for _ in tracked_dirs)
        tracked_filter = f" AND project_dir IN ({ph})"
        params.extend(tracked_dirs)

    sql = f"""
        SELECT project_dir AS repo, agent AS agent,
               COUNT(*) AS sessions,
               COALESCE(SUM(cost_usd), 0.0) AS cost_usd,
               COALESCE(SUM(tool_error_count), 0) AS tool_errors,
               COALESCE(SUM(tool_use_count), 0) AS tool_calls,
               COALESCE(SUM(input_tokens + cache_creation_tokens
                            + cache_read_tokens + output_tokens), 0) AS tokens
          FROM sessions
         WHERE is_subagent = 0
           AND project_dir IS NOT NULL AND project_dir <> ''
           AND date(started_at, 'localtime') >= ?
           {tracked_filter}
         GROUP BY project_dir, agent
    """
    with sqlite3.connect(str(CORPUS_DB)) as conn:
        conn.row_factory = sqlite3.Row
        raw = [dict(r) for r in conn.execute(sql, params).fetchall()]
    if not raw:
        return out

    # Fold (repo, agent) rows into per-repo buckets; column order is by total
    # sessions (volume) regardless of the active metric.
    repos: dict[str, dict] = {}
    agent_totals: dict[str, int] = {}
    sessions_max = 0
    value_max = 0.0
    for r in raw:
        repo, agent = r["repo"], r["agent"]
        tools = r["tool_calls"] or 0
        cell = {
            "sessions": r["sessions"],
            "cost_usd": round(float(r["cost_usd"] or 0.0), 4),
            "tokens": r["tokens"] or 0,
            "tool_errors": r["tool_errors"] or 0,
            "tool_calls": tools,
            "error_rate": round((r["tool_errors"] / tools), 4) if tools > 0 else 0.0,
        }
        bucket = repos.setdefault(
            repo, {"repo": repo, "label": _repo_label(repo),
                   "totals": {"sessions": 0, "cost_usd": 0.0, "tokens": 0, "tool_errors": 0},
                   "cells": {}}
        )
        bucket["cells"][agent] = cell
        t = bucket["totals"]
        t["sessions"] += cell["sessions"]
        t["cost_usd"] = round(t["cost_usd"] + cell["cost_usd"], 4)
        t["tokens"] += cell["tokens"]
        t["tool_errors"] += cell["tool_errors"]
        agent_totals[agent] = agent_totals.get(agent, 0) + cell["sessions"]
        sessions_max = max(sessions_max, cell["sessions"])
        value_max = max(value_max, float(cell[cell_key]))

    agents = [a for a, _ in sorted(agent_totals.items(), key=lambda kv: (-kv[1], kv[0]))]
    rows = sorted(repos.values(), key=lambda b: b["totals"][row_sort], reverse=True)

    for b in rows:
        for cell in b["cells"].values():
            cell["intensity"] = round(float(cell[cell_key]) / value_max, 4) if value_max else 0.0
            cell["primary_label"] = _matrix_primary_label(metric, cell)
            if metric == "errors":
                cell["secondary_label"] = f"{cell['sessions']:,} sess"
                cell["secondary_hot"] = False
            else:
                # Only surface the error rate when there's something to flag —
                # a clean cell shouldn't carry a "0%" that reads as an alert
                # next to its (volume-driven) shading.
                cell["secondary_label"] = f"{cell['error_rate']:.0%}" if cell["error_rate"] > 0 else ""
                cell["secondary_hot"] = cell["error_rate"] >= 0.10
        b["total_label"] = _matrix_primary_label(metric, b["totals"]) if metric != "errors" \
            else f"{b['totals']['tool_errors']:,}"

    out["agents"] = agents
    out["agent_labels"] = {a: adapter_label(a) for a in agents}
    out["repos_total"] = len(rows)
    out["rows"] = rows[:top_repos]
    out["repos_shown"] = len(out["rows"])
    out["scale"]["sessions_max"] = sessions_max
    out["scale"]["value_max"] = round(value_max, 4)
    return out


def _treatment_day(project_key: str, source_repo: str | None) -> str | None:
    """First curator-run day for this project (the "skills landed here" marker),
    as a local ISO date. Reads state.runs; None when there's no recorded run.
    Mirrors viewer.homepage._treatment_date_for_project but kept local so this
    module doesn't depend on the viewer."""
    if not STATE_DB.exists():
        return None
    try:
        with sqlite3.connect(str(STATE_DB)) as conn:
            row = conn.execute(
                "SELECT MIN(started_at) FROM runs "
                "WHERE project_key = ? AND kind LIKE 'curator%' AND status = 'ok'",
                (project_key,),
            ).fetchone()
    except sqlite3.Error:
        return None
    return _local_date(row[0]) if row and row[0] else None


def repo_swimlane(project_key: str, weeks: int = 16, source_repo: str | None = None) -> dict:
    """One repo's story over time: a daily lane per agent, so you can see when
    work happened, which agent carried it, and where it handed off — with the
    "skills landed here" marker overlaid for the before/after read.

    Returns (JSON-able; the SVG is built separately by `repo_swimlane_svg`):
        {
          "project_key", "project_dir", "weeks": 16,
          "start": "2026-02-09", "end": "2026-06-02",
          "agents": ["claude_code", ...],   # lanes, busiest first
          "agent_labels": {...},
          "days": ["2026-02-09", ...],       # every day in the window, ordered
          "lanes": {agent: {day: {sessions, cost, errors}}},  # sparse
          "max_day": 7,                      # busiest single (agent, day) cell
          "landing": "2026-04-18" | None,    # first curator run, if in window
          "total_sessions": 142,
        }

    Never raises; missing corpus.db / untracked project → empty (total 0)."""
    out: dict = {
        "project_key": project_key, "project_dir": source_repo, "weeks": weeks,
        "start": None, "end": None, "agents": [], "agent_labels": {}, "days": [],
        "lanes": {}, "max_day": 0, "landing": None, "total_sessions": 0,
        "total_skill_fires": 0,
    }
    project_dir = source_repo or _project_dir_for_key(project_key)
    out["project_dir"] = project_dir
    if not project_dir or not CORPUS_DB.exists():
        return out

    end = date.today()
    start = end - timedelta(days=weeks * 7 - 1)
    out["start"], out["end"] = start.isoformat(), end.isoformat()
    out["days"] = [(start + timedelta(days=i)).isoformat() for i in range(weeks * 7)]

    with sqlite3.connect(str(CORPUS_DB)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT date(started_at, 'localtime') AS d, agent AS agent,
                      COUNT(*) AS sessions,
                      COALESCE(SUM(cost_usd), 0.0) AS cost,
                      COALESCE(SUM(tool_error_count), 0) AS errors
                 FROM sessions
                WHERE is_subagent = 0 AND project_dir = ?
                  AND date(started_at, 'localtime') >= ?
                GROUP BY d, agent""",
            (project_dir, start.isoformat()),
        ).fetchall()
        # Skill-fire overlay: how many curated skills actually fired per
        # (agent, day) — watchmen's output being *used*, not just shipped.
        # Keyed by the session's start day so it aligns with the lane cells.
        fire_rows = conn.execute(
            """SELECT date(s.started_at, 'localtime') AS d, s.agent AS agent,
                      COUNT(*) AS fires
                 FROM tool_calls t JOIN sessions s ON t.session_id = s.session_id
                WHERE t.skill_name IS NOT NULL AND t.skill_name <> ''
                  AND s.is_subagent = 0 AND s.project_dir = ?
                  AND date(s.started_at, 'localtime') >= ?
                GROUP BY d, agent""",
            (project_dir, start.isoformat()),
        ).fetchall()

    lanes: dict[str, dict] = {}
    agent_totals: dict[str, int] = {}
    max_day = 0
    total = 0
    for r in rows:
        n = r["sessions"]
        lanes.setdefault(r["agent"], {})[r["d"]] = {
            "sessions": n, "cost": round(float(r["cost"] or 0.0), 4),
            "errors": r["errors"] or 0, "skill_fires": 0,
        }
        agent_totals[r["agent"]] = agent_totals.get(r["agent"], 0) + n
        max_day = max(max_day, n)
        total += n

    total_skill_fires = 0
    for r in fire_rows:
        cell = lanes.get(r["agent"], {}).get(r["d"])
        if cell is not None:
            cell["skill_fires"] = r["fires"]
            total_skill_fires += r["fires"]

    agents = [a for a, _ in sorted(agent_totals.items(), key=lambda kv: (-kv[1], kv[0]))]
    landing = _treatment_day(project_key, project_dir)
    out.update({
        "agents": agents,
        "agent_labels": {a: adapter_label(a) for a in agents},
        "lanes": lanes,
        "max_day": max_day,
        "landing": landing if (landing and start.isoformat() <= landing <= end.isoformat()) else None,
        "total_sessions": total,
        "total_skill_fires": total_skill_fires,
    })
    return out


_MONTHS = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def repo_swimlane_svg(data: dict, width: int = 900) -> str:
    """Render `repo_swimlane` data as a server-side SVG: one horizontal lane
    per agent, each day a cell shaded by that day's session count, a month
    axis along the top, and a dashed 'skills landed' marker. Returns '' when
    there's nothing to draw (the template shows an empty state)."""
    agents = data.get("agents") or []
    days = data.get("days") or []
    max_day = data.get("max_day") or 0
    if not agents or not days or max_day <= 0:
        return ""

    gutter, top, lane_h, gap, pad_r = 100, 24, 24, 6, 8
    plot_w = width - gutter - pad_r
    n = len(days)
    cw = plot_w / n
    height = top + len(agents) * (lane_h + gap) + 10
    fg = "hsl(var(--muted-foreground))"
    parts = [
        f'<svg viewBox="0 0 {width} {height}" width="{width}" height="{height}" '
        f'font-family="ui-sans-serif, system-ui, sans-serif" role="img" '
        f'aria-label="Per-agent session activity over time">'
    ]

    # Month axis along the top.
    prev = None
    for i, d in enumerate(days):
        ym = d[:7]
        if ym != prev:
            x = gutter + i * cw
            label = f"{_MONTHS[int(d[5:7])]}"
            parts.append(
                f'<line x1="{x:.1f}" y1="{top - 4}" x2="{x:.1f}" y2="{height - 6}" '
                f'stroke="{fg}" stroke-opacity="0.12" stroke-width="1"/>'
            )
            parts.append(
                f'<text x="{x + 2:.1f}" y="{top - 8}" font-size="10" fill="{fg}">{label}</text>'
            )
            prev = ym

    # Lanes.
    for li, agent in enumerate(agents):
        y = top + li * (lane_h + gap)
        color = _agent_color(agent, li)
        label = _xml_escape(data["agent_labels"].get(agent, agent))
        parts.append(
            f'<rect x="{gutter - 12:.1f}" y="{y + lane_h / 2 - 4:.1f}" width="8" height="8" '
            f'rx="2" fill="{color}"/>'
        )
        parts.append(
            f'<text x="{gutter - 18:.1f}" y="{y + lane_h / 2 + 4:.1f}" font-size="11" '
            f'fill="hsl(var(--foreground))" text-anchor="end">{label}</text>'
        )
        # Lane baseline so empty lanes still read as a track.
        parts.append(
            f'<rect x="{gutter:.1f}" y="{y:.1f}" width="{plot_w:.1f}" height="{lane_h}" '
            f'fill="{fg}" fill-opacity="0.04" rx="3"/>'
        )
        lane = data["lanes"].get(agent, {})
        for i, d in enumerate(days):
            cell = lane.get(d)
            if not cell:
                continue
            inten = cell["sessions"] / max_day
            x = gutter + i * cw
            opacity = round(0.2 + 0.8 * inten, 3)
            tip = f'{label} · {d} · {cell["sessions"]} session(s)'
            if cell["errors"]:
                tip += f' · {cell["errors"]} tool errors'
            fires = cell.get("skill_fires", 0)
            if fires:
                tip += f' · {fires} skill fire(s)'
            parts.append(
                f'<rect x="{x:.2f}" y="{y:.1f}" width="{max(cw - 0.5, 1):.2f}" height="{lane_h}" '
                f'fill="{color}" fill-opacity="{opacity}"><title>{_xml_escape(tip)}</title></rect>'
            )
            # Skill-fire overlay: an amber dot on days a curated skill ran —
            # watchmen's output actually being used in this lane.
            if fires:
                r = min(max(cw / 2 - 0.6, 1.4), 2.6)
                cx = x + max(cw - 0.5, 1) / 2
                parts.append(
                    f'<circle cx="{cx:.2f}" cy="{y + 3.5:.1f}" r="{r:.1f}" fill="#f59e0b" '
                    f'stroke="#fff" stroke-width="0.5"/>'
                )

    # "Skills landed here" marker.
    landing = data.get("landing")
    if landing and landing in days:
        idx = days.index(landing)
        x = gutter + idx * cw
        parts.append(
            f'<line x1="{x:.1f}" y1="{top - 4}" x2="{x:.1f}" y2="{height - 6}" '
            f'stroke="#ef4444" stroke-width="1.5" stroke-dasharray="3 3"/>'
        )
        parts.append(
            f'<text x="{x + 3:.1f}" y="{height - 1}" font-size="9" fill="#ef4444">skills landed</text>'
        )

    # Legend for the skill-fire dot (only when there's at least one),
    # top-right so it can't collide with the "skills landed" label.
    if data.get("total_skill_fires"):
        ly = top - 8
        parts.append(f'<circle cx="{width - 150:.1f}" cy="{ly - 3:.1f}" r="2.4" fill="#f59e0b" stroke="#fff" stroke-width="0.5"/>')
        parts.append(
            f'<text x="{width - 143:.1f}" y="{ly:.1f}" font-size="9" fill="{fg}">= a curated skill fired</text>'
        )

    parts.append("</svg>")
    return "".join(parts)


def agent_comparison_facts(days: int = 90, tracked_only: bool = False) -> dict:
    """Per-adapter rollup rich enough to feed the cross-agent comparison LLM
    narrative AND a deterministic side-by-side render. Extends
    `adapter_breakdown_all` with derived ratios (cost/prompt, error rate,
    cache hit, prompts/session), session-shape (active days, avg duration),
    behavioral mix (top tools, top projects, dominant model), and a
    suggestions-fired counter scoped per adapter.

    Returns a dict ready to JSON-stringify into a prompt:

        {
          "window_days": 90,
          "since": "2026-02-15",
          "adapters": [{...}, ...],   # one per agent with sessions in window
        }

    Empty `adapters` when corpus.db is absent or no sessions in window.
    The function never raises — corrupt rows are skipped silently."""
    out: dict = {"window_days": days, "since": None, "adapters": []}
    if not CORPUS_DB.exists():
        return out

    cutoff = date.today() - timedelta(days=days - 1)
    out["since"] = cutoff.isoformat()

    tracked_filter = ""
    tracked_params: list = []
    if tracked_only:
        tracked_dirs = _tracked_project_dirs()
        if not tracked_dirs:
            return out
        ph = ",".join("?" for _ in tracked_dirs)
        tracked_filter = f" AND s.project_dir IN ({ph})"
        tracked_params = list(tracked_dirs)

    # Q1 — base aggregate per adapter. Active days uses COUNT DISTINCT on the
    # localtime calendar day; avg session duration is over duration_seconds
    # (NULL when a session never closed → AVG drops it, which is the right
    # behavior — half-finished sessions shouldn't pull the average down).
    base_sql = f"""
        SELECT s.agent                                 AS agent,
               COUNT(*)                                AS sessions,
               COUNT(DISTINCT date(s.started_at, 'localtime')) AS active_days,
               COUNT(DISTINCT s.project_dir)           AS projects,
               COALESCE(SUM(s.user_prompt_count), 0)   AS prompts,
               COALESCE(SUM(s.tool_use_count), 0)      AS tool_calls,
               COALESCE(SUM(s.tool_error_count), 0)    AS tool_errors,
               COALESCE(SUM(s.input_tokens), 0)        AS input_tokens,
               COALESCE(SUM(s.cache_creation_tokens), 0) AS cache_creation_tokens,
               COALESCE(SUM(s.cache_read_tokens), 0)   AS cache_read_tokens,
               COALESCE(SUM(s.output_tokens), 0)       AS output_tokens,
               COALESCE(SUM(s.cost_usd), 0.0)          AS cost_usd,
               AVG(s.duration_seconds)                 AS avg_duration_seconds
          FROM sessions s
         WHERE s.is_subagent = 0
           AND date(s.started_at, 'localtime') >= ?
           {tracked_filter}
         GROUP BY s.agent
         ORDER BY sessions DESC
    """
    params: list = [cutoff.isoformat()] + tracked_params

    rows: list[dict] = []
    with sqlite3.connect(str(CORPUS_DB)) as conn:
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute(base_sql, params).fetchall()]

        # Q2 — top 5 tools per adapter (excluding subagent sessions, same
        # window). JOIN on session_id so we can scope tool_calls to a given
        # agent. We don't store agent on tool_calls directly; this join is
        # cheap thanks to idx_tool_calls_session.
        tools_sql = f"""
            SELECT s.agent AS agent, t.tool_name AS tool_name, COUNT(*) AS n
              FROM tool_calls t
              JOIN sessions s ON t.session_id = s.session_id
             WHERE s.is_subagent = 0
               AND date(s.started_at, 'localtime') >= ?
               {tracked_filter}
               AND t.tool_name IS NOT NULL AND t.tool_name <> ''
             GROUP BY s.agent, t.tool_name
             ORDER BY s.agent ASC, n DESC
        """
        top_tools: dict[str, list[tuple[str, int]]] = {}
        for r in conn.execute(tools_sql, params).fetchall():
            top_tools.setdefault(r["agent"], []).append((r["tool_name"], r["n"]))

        # Q3 — top 3 projects per adapter (where the agent gets used most).
        # Translates project_dir → human label via tracked project index
        # (`util.adapter_breakdown`-style) so the narrative can name "kai"
        # instead of "/Users/foo/dev/kai".
        projects_sql = f"""
            SELECT s.agent AS agent, s.project_dir AS project_dir, COUNT(*) AS n
              FROM sessions s
             WHERE s.is_subagent = 0
               AND date(s.started_at, 'localtime') >= ?
               {tracked_filter}
               AND s.project_dir IS NOT NULL AND s.project_dir <> ''
             GROUP BY s.agent, s.project_dir
             ORDER BY s.agent ASC, n DESC
        """
        top_projects: dict[str, list[tuple[str, int]]] = {}
        for r in conn.execute(projects_sql, params).fetchall():
            top_projects.setdefault(r["agent"], []).append((r["project_dir"], r["n"]))

        # Q4 — dominant model per adapter. We trust sessions.model_dominant
        # (already chosen as max-output-tokens model in that session). Tally
        # session counts per (agent, dominant model) and pick the top one.
        models_sql = f"""
            SELECT s.agent AS agent, s.model_dominant AS model, COUNT(*) AS n
              FROM sessions s
             WHERE s.is_subagent = 0
               AND date(s.started_at, 'localtime') >= ?
               {tracked_filter}
               AND s.model_dominant IS NOT NULL AND s.model_dominant <> ''
             GROUP BY s.agent, s.model_dominant
             ORDER BY s.agent ASC, n DESC
        """
        dominant_model: dict[str, str] = {}
        for r in conn.execute(models_sql, params).fetchall():
            dominant_model.setdefault(r["agent"], r["model"])

    # Skill-suggestion firings per adapter. Read the plugin's append-only
    # suggestions log and join to sessions.agent in memory. Best-effort:
    # missing log → all zero; malformed lines skipped silently.
    suggestions_fired: dict[str, int] = {}
    sugg_path = Path.home() / ".watchmen" / "suggestions.jsonl"
    if sugg_path.exists() and rows:
        agent_by_session: dict[str, str] = {}
        with sqlite3.connect(str(CORPUS_DB)) as conn2:
            conn2.row_factory = sqlite3.Row
            for r in conn2.execute(
                "SELECT session_id, agent FROM sessions WHERE date(started_at, 'localtime') >= ?",
                (cutoff.isoformat(),),
            ).fetchall():
                if r["session_id"]:
                    agent_by_session[r["session_id"]] = r["agent"]
        try:
            with sugg_path.open() as fh:
                for line in fh:
                    try:
                        evt = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    sid = evt.get("session_id")
                    a = agent_by_session.get(sid)
                    if a:
                        suggestions_fired[a] = suggestions_fired.get(a, 0) + 1
        except OSError:
            pass

    # Derive ratios + attach behavioral lists. Guard each division so a
    # zero-activity adapter doesn't NaN out the whole comparison.
    adapters: list[dict] = []
    for row in rows:
        agent = row["agent"]
        prompts = row["prompts"] or 0
        sessions = row["sessions"] or 0
        tool_calls = row["tool_calls"] or 0
        cost = float(row["cost_usd"] or 0.0)
        input_t = row["input_tokens"] or 0
        cache_r = row["cache_read_tokens"] or 0
        adapters.append({
            "agent": agent,
            "label": adapter_label(agent),
            "sessions": sessions,
            "active_days": row["active_days"] or 0,
            "projects": row["projects"] or 0,
            "prompts": prompts,
            "tool_calls": tool_calls,
            "tool_errors": row["tool_errors"] or 0,
            "cost_usd": round(cost, 4),
            "input_tokens": input_t,
            "cache_creation_tokens": row["cache_creation_tokens"] or 0,
            "cache_read_tokens": cache_r,
            "output_tokens": row["output_tokens"] or 0,
            # Derived ratios
            "cost_per_prompt":     round(cost / prompts, 4) if prompts > 0 else 0.0,
            "cost_per_session":    round(cost / sessions, 4) if sessions > 0 else 0.0,
            "prompts_per_session": round(prompts / sessions, 2) if sessions > 0 else 0.0,
            "tool_calls_per_session": round(tool_calls / sessions, 2) if sessions > 0 else 0.0,
            "tool_error_rate":     round(row["tool_errors"] / tool_calls, 4) if tool_calls > 0 else 0.0,
            "cache_hit_rate":      round(cache_r / (input_t + cache_r), 4) if (input_t + cache_r) > 0 else 0.0,
            "avg_session_seconds": round(row["avg_duration_seconds"] or 0.0, 1),
            # Behavioral
            "top_tools":    top_tools.get(agent, [])[:5],
            "top_projects": top_projects.get(agent, [])[:3],
            "dominant_model": dominant_model.get(agent),
            "suggestions_fired": suggestions_fired.get(agent, 0),
        })

    out["adapters"] = adapters
    return out


def activity_calendar_all(weeks: int = 26, tracked_only: bool = False) -> list[tuple[str, int]]:
    """Calendar across all activity, optionally restricted to tracked projects."""
    if not CORPUS_DB.exists():
        return []
    today = date.today()
    to_sunday = (today.weekday() + 1) % 7
    end = today
    start = today - timedelta(days=(weeks * 7 - 1 - to_sunday))

    sql = """SELECT date(p.timestamp, 'localtime') AS d, COUNT(*) AS n
             FROM prompts p JOIN sessions s ON p.session_id = s.session_id
             WHERE s.is_subagent = 0
               AND date(p.timestamp, 'localtime') >= ?
               AND date(p.timestamp, 'localtime') <= ?"""
    params: list = [start.isoformat(), end.isoformat()]
    if tracked_only:
        tracked_dirs = _tracked_project_dirs()
        if not tracked_dirs:
            return [((start + timedelta(days=i)).isoformat(), 0) for i in range(weeks * 7) if start + timedelta(days=i) <= end]
        placeholders = ",".join("?" for _ in tracked_dirs)
        sql += f" AND s.project_dir IN ({placeholders})"
        params.extend(tracked_dirs)
    sql += " GROUP BY date(p.timestamp, 'localtime')"

    with sqlite3.connect(str(CORPUS_DB)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, params).fetchall()
    counts = {r["d"]: r["n"] for r in rows}
    out = []
    for i in range(weeks * 7):
        d = start + timedelta(days=i)
        if d > end:
            break
        out.append((d.isoformat(), counts.get(d.isoformat(), 0)))
    return out


def activity_by_hour_dow_all(days: int = 90, tracked_only: bool = False) -> list[list[int]]:
    """Hour-of-day × day-of-week heatmap, optionally restricted to tracked projects."""
    if not CORPUS_DB.exists():
        return [[0] * 24 for _ in range(7)]
    cutoff = (date.today() - timedelta(days=days)).isoformat()

    sql = """SELECT CAST(strftime('%w', p.timestamp, 'localtime') AS INT) AS dow,
                    CAST(strftime('%H', p.timestamp, 'localtime') AS INT) AS hr,
                    COUNT(*) AS n
             FROM prompts p JOIN sessions s ON p.session_id = s.session_id
             WHERE s.is_subagent = 0
               AND date(p.timestamp, 'localtime') >= ?"""
    params: list = [cutoff]
    if tracked_only:
        tracked_dirs = _tracked_project_dirs()
        if not tracked_dirs:
            return [[0] * 24 for _ in range(7)]
        placeholders = ",".join("?" for _ in tracked_dirs)
        sql += f" AND s.project_dir IN ({placeholders})"
        params.extend(tracked_dirs)
    sql += " GROUP BY dow, hr"

    with sqlite3.connect(str(CORPUS_DB)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, params).fetchall()
    m = [[0] * 24 for _ in range(7)]
    for r in rows:
        if r["dow"] is None or r["hr"] is None:
            continue
        m[r["dow"]][r["hr"]] = r["n"]
    return m


def per_project_totals(days: int = 30) -> list[dict]:
    """Per-project rollup over the window, sorted by cost descending.
    Used in the aggregated metrics page to show which projects drive the totals."""
    from watchmen import state as _state
    out = []
    for p in _state.list_projects():
        rows = daily_metrics(p["project_key"], days=days)
        total = summarize_window(rows, days)
        total["project_key"] = p["project_key"]
        out.append(total)
    out.sort(key=lambda r: r["cost_usd"], reverse=True)
    return out


def _median(xs: list[float]) -> float:
    """Plain median; 0.0 on empty input. Mirrors viewer.homepage._median so
    the per-skill numbers line up with the project impact card's style."""
    if not xs:
        return 0.0
    s = sorted(xs)
    n = len(s)
    return float(s[n // 2]) if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def _effectiveness_verdict(
    *, n_fired: int, n_base: int, delta_error: float | None, delta_cost: float | None
) -> str:
    """Plain-language summary of a skill's fired-vs-comparison outcomes.

    ASSOCIATION ONLY — never claims the skill *caused* the difference.
    Sessions that invoke a skill self-select for harder or more procedural
    work, which confounds any naive fired-vs-not comparison. Same no-causation
    discipline as `viewer.homepage.project_impact`'s verdict."""
    if n_fired < 3:
        return f"Too few fired sessions to compare (n={n_fired}, need ≥3)."
    if n_base < 3 or delta_error is None or delta_cost is None:
        return "No comparable non-fired sessions in the same projects."
    parts: list[str] = []
    if delta_error < 0:
        parts.append(f"{abs(delta_error) * 100:.1f}pp lower tool-error rate")
    elif delta_error > 0:
        parts.append(f"{delta_error * 100:.1f}pp higher tool-error rate")
    else:
        parts.append("the same tool-error rate")
    if delta_cost < 0:
        parts.append(f"${abs(delta_cost):.3f} lower median cost")
    elif delta_cost > 0:
        parts.append(f"${delta_cost:.3f} higher median cost")
    else:
        parts.append("the same median cost")
    return (
        f"Sessions using this skill showed {parts[0]} and {parts[1]} "
        f"(association only; n={n_fired} fired vs {n_base} comparable)."
    )


def skill_effectiveness(days: int = 90, project_key: str | None = None) -> list[dict]:
    """Per-skill usage + outcome association across captured sessions.

    Answers two questions the maintainer cares about in equal measure:

    USAGE — how is this skill actually being used?
        fires            total Skill/skill tool invocations in the window
        sessions_fired   distinct non-subagent sessions it fired in
        projects         distinct project_dirs it fired in
        first_fired      ISO local date of first / last invocation
        last_fired
        fires_recent     fires in the trailing half of the window vs the
        fires_prior      leading half — a cheap decay/staleness signal
        cost_usd         accrued per-skill cost (the #94 `tool_calls.cost_usd`
                         accrual; 0.0 on adapters/rows that never populated it)

    EFFECTIVENESS — among sessions in the SAME projects + window, compare
    sessions where the skill fired against sessions where it did not:
        fired / baseline   each {sessions_n, median_error_rate, median_cost,
                           median_duration_seconds}
        delta_error_rate   signed (fired − baseline); None if either side <3
        delta_cost
        verdict            plain-language, ASSOCIATION-ONLY (never causal)

    The comparison is confounded by selection (skills fire on harder tasks) —
    the field names and verdict say so. It is a descriptive signal to triage
    skills, not a causal claim. Sorted by `fires` desc; missing corpus.db → [].
    Never raises — the underlying data is best-effort."""
    if not CORPUS_DB.exists():
        return []

    today = date.today()
    cutoff = (today - timedelta(days=days - 1)).isoformat()
    # Split point for the decay signal: midpoint of the window.
    mid = (today - timedelta(days=(days - 1) // 2)).isoformat()

    project_dir: str | None = None
    if project_key is not None:
        project_dir = _project_dir_for_key(project_key)
        if project_dir is None:
            return []
    proj_filter = " AND s.project_dir = ?" if project_dir else ""
    proj_param = [project_dir] if project_dir else []

    with sqlite3.connect(str(CORPUS_DB)) as conn:
        conn.row_factory = sqlite3.Row
        # Q1 — per-skill usage. skill_name is set on the activation row;
        # cost_usd carries the #94 per-skill accrual.
        usage_rows = [
            dict(r)
            for r in conn.execute(
                f"""
                SELECT t.skill_name AS skill,
                       COUNT(*) AS fires,
                       COUNT(DISTINCT t.session_id) AS sessions_fired,
                       COUNT(DISTINCT s.project_dir) AS projects,
                       MIN(t.timestamp) AS first_fired,
                       MAX(t.timestamp) AS last_fired,
                       COALESCE(SUM(t.cost_usd), 0.0) AS cost_usd,
                       SUM(CASE WHEN date(t.timestamp,'localtime') >= ?
                                THEN 1 ELSE 0 END) AS fires_recent
                  FROM tool_calls t
                  JOIN sessions s ON t.session_id = s.session_id
                 WHERE t.skill_name IS NOT NULL AND t.skill_name <> ''
                   AND s.is_subagent = 0
                   AND date(s.started_at,'localtime') >= ?
                   {proj_filter}
                 GROUP BY t.skill_name
                 ORDER BY fires DESC
                """,
                [mid, cutoff] + proj_param,
            ).fetchall()
        ]
        if not usage_rows:
            return []

        # Per-session outcomes for the window (one pass, pooled in memory — a
        # personal corpus is small). error_rate normalizes errors by tool
        # calls so a long session isn't penalized for sheer volume.
        sessions: dict[str, dict] = {}
        for r in conn.execute(
            f"""SELECT s.session_id AS sid, s.project_dir AS pd,
                       s.tool_error_count AS errs, s.tool_use_count AS tools,
                       s.cost_usd AS cost, s.duration_seconds AS dur
                  FROM sessions s
                 WHERE s.is_subagent = 0
                   AND date(s.started_at,'localtime') >= ?
                   {proj_filter}""",
            [cutoff] + proj_param,
        ).fetchall():
            sessions[r["sid"]] = {
                "pd": r["pd"],
                "error_rate": (r["errs"] / r["tools"]) if r["tools"] else 0.0,
                "cost": float(r["cost"] or 0.0),
                "dur": r["dur"],
            }

        # skill -> set of session_ids it fired in (window-scoped).
        fired_by_skill: dict[str, set[str]] = {}
        for r in conn.execute(
            f"""SELECT DISTINCT t.skill_name AS skill, t.session_id AS sid
                  FROM tool_calls t
                  JOIN sessions s ON t.session_id = s.session_id
                 WHERE t.skill_name IS NOT NULL AND t.skill_name <> ''
                   AND s.is_subagent = 0
                   AND date(s.started_at,'localtime') >= ?
                   {proj_filter}""",
            [cutoff] + proj_param,
        ).fetchall():
            fired_by_skill.setdefault(r["skill"], set()).add(r["sid"])

    # Pre-bucket session ids by project so the baseline lookup is O(1).
    sids_by_project: dict[str | None, set[str]] = {}
    for sid, s in sessions.items():
        sids_by_project.setdefault(s["pd"], set()).add(sid)

    def _stats(group: set[str]) -> dict:
        g = [sessions[sid] for sid in group if sid in sessions]
        return {
            "sessions_n": len(g),
            "median_error_rate": round(_median([x["error_rate"] for x in g]), 4),
            "median_cost": round(_median([x["cost"] for x in g]), 4),
            "median_duration_seconds": round(
                _median([x["dur"] for x in g if x["dur"] is not None]), 1
            ),
        }

    out: list[dict] = []
    for u in usage_rows:
        skill = u["skill"]
        fired = fired_by_skill.get(skill, set())
        projects_of_skill = {sessions[sid]["pd"] for sid in fired if sid in sessions}
        # Baseline = sessions in the SAME projects that did NOT fire this skill.
        baseline: set[str] = set()
        for pd in projects_of_skill:
            baseline |= sids_by_project.get(pd, set())
        baseline -= fired

        fired_stats = _stats(fired)
        base_stats = _stats(baseline)
        have_both = fired_stats["sessions_n"] >= 3 and base_stats["sessions_n"] >= 3
        delta_error = (
            round(fired_stats["median_error_rate"] - base_stats["median_error_rate"], 4)
            if have_both
            else None
        )
        delta_cost = (
            round(fired_stats["median_cost"] - base_stats["median_cost"], 4)
            if have_both
            else None
        )
        fires = u["fires"]
        fires_recent = u["fires_recent"] or 0

        out.append(
            {
                "skill_name": skill,
                "fires": fires,
                "sessions_fired": u["sessions_fired"],
                "projects": u["projects"],
                "first_fired": _local_date(u["first_fired"]),
                "last_fired": _local_date(u["last_fired"]),
                "fires_recent": fires_recent,
                "fires_prior": fires - fires_recent,
                "cost_usd": round(float(u["cost_usd"] or 0.0), 4),
                "fired": fired_stats,
                "baseline": base_stats,
                "delta_error_rate": delta_error,
                "delta_cost": delta_cost,
                "verdict": _effectiveness_verdict(
                    n_fired=fired_stats["sessions_n"],
                    n_base=base_stats["sessions_n"],
                    delta_error=delta_error,
                    delta_cost=delta_cost,
                ),
            }
        )
    return out


# Recency half-life (days) for the friction-ledger score. A mistake last seen
# two weeks ago counts half as urgent as one seen today, at equal recurrence.
_FRICTION_HALFLIFE_DAYS = 14.0


def friction_ledger(
    days: int = 90,
    project_key: str | None = None,
    weeks: int = 12,
    limit: int = 40,
    min_occurrences: int = 2,
) -> dict:
    """Recurring tool failures, grouped by normalized `error_signature` —
    the "do I keep making the same mistake?" view (#110).

    Groups errored `tool_calls` by their folded signature (see
    `adapters/_shared.normalize_error_signature`) and ranks them by
    *recurrence × recency*, so a frequent mistake you made today outranks an
    equally frequent one you stopped making a month ago. Only signatures with
    `>= min_occurrences` count (a one-off isn't a recurring mistake);
    user-cancelled / rejected calls never get a signature, so they're absent
    by construction.

    Each entry:
        signature        the folded error text (the grouping key)
        occurrences      total errored calls with this signature
        sessions         distinct sessions it occurred in
        repos            distinct project_dirs it occurred in
        agents           distinct agents that hit it (list)
        top_tool         the tool that most often produced it
        first_seen       ISO local date, first / last occurrence in window
        last_seen
        days_since_last  recency, in days
        spark            weekly counts over `weeks` (oldest→newest), for a
                         "is the curve falling?" sparkline
        score            recurrence × recency rank key

    Returns (JSON-able):
        {
          "generated_for": project_key | None, "days", "weeks",
          "total_errors":     errored calls in window (incl. unsignatured),
          "signatured":       errored calls that carry a signature,
          "distinct":         distinct recurring signatures (>= min_occurrences),
          "singletons":       signatures seen exactly once (reported, not listed),
          "repos_total":      distinct repos with any signatured error,
          "landing":          treatment day if project-scoped + in window,
          "entries":          [ ... ranked, truncated to `limit` ... ],
        }

    Never raises; missing corpus.db → empty (all zeros)."""
    out: dict = {
        "generated_for": project_key, "days": days, "weeks": weeks,
        "total_errors": 0, "signatured": 0, "distinct": 0, "singletons": 0,
        "repos_total": 0, "landing": None, "entries": [],
    }
    if not CORPUS_DB.exists():
        return out

    today = date.today()
    cutoff = (today - timedelta(days=days - 1)).isoformat()

    project_dir: str | None = None
    if project_key is not None:
        project_dir = _project_dir_for_key(project_key)
        if project_dir is None:
            return out
    proj_filter = " AND s.project_dir = ?" if project_dir else ""
    proj_param = [project_dir] if project_dir else []

    with sqlite3.connect(str(CORPUS_DB)) as conn:
        conn.row_factory = sqlite3.Row
        # Total errored calls (denominator) — includes unsignatured (cancels).
        out["total_errors"] = conn.execute(
            """SELECT COUNT(*) FROM tool_calls t JOIN sessions s
                 ON t.session_id = s.session_id
                WHERE t.is_error = 1 AND s.is_subagent = 0
                  AND date(t.timestamp, 'localtime') >= ?""" + proj_filter,
            [cutoff] + proj_param,
        ).fetchone()[0]
        # Every signatured errored call in the window — aggregate in Python so
        # we can build per-signature agent sets, top-tool, and weekly sparks
        # without three more GROUP BY round-trips. The filter (errored AND
        # signatured) keeps this to the low thousands even on a big corpus.
        rows = conn.execute(
            """SELECT t.error_signature AS sig, t.session_id AS sid,
                      s.project_dir AS repo, s.agent AS agent,
                      t.tool_name AS tool,
                      date(t.timestamp, 'localtime') AS d
                 FROM tool_calls t JOIN sessions s ON t.session_id = s.session_id
                WHERE t.is_error = 1 AND t.error_signature IS NOT NULL
                  AND s.is_subagent = 0
                  AND date(t.timestamp, 'localtime') >= ?""" + proj_filter,
            [cutoff] + proj_param,
        ).fetchall()

    out["signatured"] = len(rows)
    if not rows:
        return out

    # Weekly buckets: index 0 = oldest week, `weeks-1` = current week.
    span_start = today - timedelta(days=weeks * 7 - 1)

    def _week_idx(d_str: str) -> int | None:
        try:
            d = date.fromisoformat(d_str)
        except (ValueError, TypeError):
            return None
        delta = (d - span_start).days
        if delta < 0:
            return 0  # older than the spark window → fold into the first bin
        idx = delta // 7
        return min(idx, weeks - 1)

    groups: dict[str, dict] = {}
    repos_all: set[str] = set()
    for r in rows:
        sig = r["sig"]
        repos_all.add(r["repo"])
        g = groups.get(sig)
        if g is None:
            g = groups[sig] = {
                "occurrences": 0, "sessions": set(), "repos": set(),
                "agents": set(), "tools": {}, "first_seen": r["d"],
                "last_seen": r["d"], "spark": [0] * weeks,
            }
        g["occurrences"] += 1
        g["sessions"].add(r["sid"])
        g["repos"].add(r["repo"])
        g["agents"].add(r["agent"])
        g["tools"][r["tool"]] = g["tools"].get(r["tool"], 0) + 1
        if r["d"] and r["d"] < g["first_seen"]:
            g["first_seen"] = r["d"]
        if r["d"] and r["d"] > g["last_seen"]:
            g["last_seen"] = r["d"]
        wi = _week_idx(r["d"])
        if wi is not None:
            g["spark"][wi] += 1

    out["repos_total"] = len(repos_all)
    singletons = sum(1 for g in groups.values() if g["occurrences"] < min_occurrences)
    out["singletons"] = singletons

    entries = []
    for sig, g in groups.items():
        if g["occurrences"] < min_occurrences:
            continue
        try:
            days_since = (today - date.fromisoformat(g["last_seen"])).days
        except (ValueError, TypeError):
            days_since = days
        recency = 0.5 ** (max(days_since, 0) / _FRICTION_HALFLIFE_DAYS)
        score = g["occurrences"] * recency
        top_tool = max(g["tools"].items(), key=lambda kv: (kv[1], kv[0]))[0]
        entries.append({
            "signature": sig,
            "occurrences": g["occurrences"],
            "sessions": len(g["sessions"]),
            "repos": len(g["repos"]),
            "agents": sorted(g["agents"]),
            "top_tool": top_tool,
            "first_seen": g["first_seen"],
            "last_seen": g["last_seen"],
            "days_since_last": days_since,
            "spark": g["spark"],
            "score": round(score, 3),
        })

    entries.sort(key=lambda e: (-e["score"], -e["occurrences"], e["signature"]))
    out["distinct"] = len(entries)
    out["entries"] = entries[:limit]
    if project_key is not None:
        landing = _treatment_day(project_key, project_dir)
        if landing and span_start.isoformat() <= landing <= today.isoformat():
            out["landing"] = landing
    return out


def activity_calendar(project_key: str, weeks: int = 26) -> list[tuple[str, int]]:
    """Per-day prompt counts for the last `weeks` weeks (Sunday-aligned).
    Returns [(date_iso, prompt_count)] in chronological order."""
    project_dir = _project_dir_for_key(project_key)
    if not project_dir or not CORPUS_DB.exists():
        return []
    today = date.today()
    # Sunday-align: roll back to the Sunday of this week, then go N-1 weeks back.
    # weekday(): Mon=0..Sun=6 → days to subtract to reach Sunday
    to_sunday = (today.weekday() + 1) % 7
    end = today  # inclusive
    start = today - timedelta(days=(weeks * 7 - 1 - to_sunday))

    with sqlite3.connect(str(CORPUS_DB)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT date(p.timestamp, 'localtime') AS d, COUNT(*) AS n
               FROM prompts p JOIN sessions s ON p.session_id = s.session_id
               WHERE s.project_dir = ? AND s.is_subagent = 0
                 AND date(p.timestamp, 'localtime') >= ?
                 AND date(p.timestamp, 'localtime') <= ?
               GROUP BY date(p.timestamp, 'localtime')""",
            (project_dir, start.isoformat(), end.isoformat()),
        ).fetchall()
    counts = {r["d"]: r["n"] for r in rows}
    out = []
    for i in range(weeks * 7):
        d = start + timedelta(days=i)
        if d > end:
            break
        out.append((d.isoformat(), counts.get(d.isoformat(), 0)))
    return out


def activity_by_hour_dow(project_key: str, days: int = 90) -> list[list[int]]:
    """7×24 matrix of prompt counts. Row 0 = Sunday, col 0 = midnight (local).
    Returns matrix[dow][hour] = count."""
    project_dir = _project_dir_for_key(project_key)
    if not project_dir or not CORPUS_DB.exists():
        return [[0] * 24 for _ in range(7)]
    cutoff = (date.today() - timedelta(days=days)).isoformat()

    with sqlite3.connect(str(CORPUS_DB)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT CAST(strftime('%w', p.timestamp, 'localtime') AS INT) AS dow,
                      CAST(strftime('%H', p.timestamp, 'localtime') AS INT) AS hr,
                      COUNT(*) AS n
               FROM prompts p JOIN sessions s ON p.session_id = s.session_id
               WHERE s.project_dir = ? AND s.is_subagent = 0
                 AND date(p.timestamp, 'localtime') >= ?
               GROUP BY dow, hr""",
            (project_dir, cutoff),
        ).fetchall()
    m = [[0] * 24 for _ in range(7)]
    for r in rows:
        if r["dow"] is None or r["hr"] is None:
            continue
        m[r["dow"]][r["hr"]] = r["n"]
    return m


def _heatmap_color(n: int, hi: int, palette: list[str]) -> str:
    if n <= 0 or hi <= 0:
        return palette[0]
    # 4 active buckets above the empty bucket.
    bucket = min(4, max(1, int((n / hi) * 4) + (1 if n > 0 else 0)))
    return palette[bucket]


def calendar_heatmap_svg(daily: list[tuple[str, int]], weeks: int = 26) -> str:
    """GitHub-style contribution grid. 7 rows (Sun→Sat), N cols (weeks)."""
    cell = 12
    gap = 3
    pad_left = 28
    pad_top = 18
    width = pad_left + weeks * (cell + gap)
    height = pad_top + 7 * (cell + gap) + 12
    palette = ["#f3f4f6", "#dbeafe", "#93c5fd", "#3b82f6", "#1e40af"]
    counts = [c for _, c in daily]
    hi = max(counts) if counts else 0

    rects = []
    if daily:
        first_d = date.fromisoformat(daily[0][0])
        for idx, (d_str, n) in enumerate(daily):
            d = date.fromisoformat(d_str)
            col = (d - first_d).days // 7
            row = (d.weekday() + 1) % 7  # Sun=0
            x = pad_left + col * (cell + gap)
            y = pad_top + row * (cell + gap)
            fill = _heatmap_color(n, hi, palette)
            rects.append(
                f'<rect x="{x}" y="{y}" width="{cell}" height="{cell}" rx="2" fill="{fill}">'
                f'<title>{d_str}: {n} prompt{"s" if n != 1 else ""}</title></rect>'
            )

    # Day labels (Sun, Mon...)
    day_labels = ""
    for i, name in enumerate(["Sun", "", "Tue", "", "Thu", "", "Sat"]):
        if name:
            y = pad_top + i * (cell + gap) + cell - 2
            day_labels += f'<text x="0" y="{y}" font-size="10" fill="#6b7280">{name}</text>'

    legend = ""
    lg_x = width - 5 * (cell + gap) - 60
    legend += f'<text x="{lg_x}" y="{height - 4}" font-size="10" fill="#6b7280">less</text>'
    for i, c in enumerate(palette):
        x = lg_x + 28 + i * (cell + 2)
        legend += f'<rect x="{x}" y="{height - 14}" width="{cell}" height="{cell}" rx="2" fill="{c}"/>'
    legend += f'<text x="{lg_x + 28 + 5 * (cell + 2) + 4}" y="{height - 4}" font-size="10" fill="#6b7280">more</text>'

    return (
        f'<svg width="{width}" height="{height}" xmlns="http://www.w3.org/2000/svg">'
        f'{day_labels}{"".join(rects)}{legend}</svg>'
    )


def hour_dow_heatmap_svg(matrix: list[list[int]]) -> str:
    """7-row × 24-col heatmap. Row 0 = Sunday."""
    cell_w = 22
    cell_h = 22
    gap = 3
    pad_left = 36
    pad_top = 22
    width = pad_left + 24 * (cell_w + gap)
    height = pad_top + 7 * (cell_h + gap) + 10
    palette = ["#f3f4f6", "#fce7f3", "#f9a8d4", "#ec4899", "#9d174d"]
    hi = max((max(row) for row in matrix), default=0)

    cells = ""
    for dow in range(7):
        for hr in range(24):
            n = matrix[dow][hr]
            fill = _heatmap_color(n, hi, palette)
            x = pad_left + hr * (cell_w + gap)
            y = pad_top + dow * (cell_h + gap)
            day_name = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"][dow]
            cells += (
                f'<rect x="{x}" y="{y}" width="{cell_w}" height="{cell_h}" rx="3" fill="{fill}">'
                f'<title>{day_name} {hr:02d}:00 — {n} prompt{"s" if n != 1 else ""}</title></rect>'
            )

    # Hour labels along the top (every 3 hours)
    hour_labels = ""
    for hr in range(0, 24, 3):
        x = pad_left + hr * (cell_w + gap) + cell_w / 2
        hour_labels += f'<text x="{x}" y="14" font-size="10" fill="#6b7280" text-anchor="middle">{hr:02d}</text>'

    # Day labels down the left
    day_labels = ""
    for i, name in enumerate(["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]):
        y = pad_top + i * (cell_h + gap) + cell_h - 6
        day_labels += f'<text x="0" y="{y}" font-size="11" fill="#6b7280">{name}</text>'

    return (
        f'<svg width="{width}" height="{height}" xmlns="http://www.w3.org/2000/svg">'
        f'{hour_labels}{day_labels}{cells}</svg>'
    )


def tool_usage(
    project_key: str | None = None,
    days: int = 30,
    limit: int = 12,
    tracked_only: bool = False,
) -> list[dict]:
    """Top N tools by call count over the window, plus error counts.

    project_key=None aggregates across all sessions; pair with tracked_only=True
    to restrict to tracked projects only."""
    if not CORPUS_DB.exists():
        return []
    cutoff = (date.today() - timedelta(days=days)).isoformat()

    sql = """SELECT t.tool_name, COUNT(*) AS n, SUM(t.is_error) AS errors
             FROM tool_calls t JOIN sessions s ON t.session_id = s.session_id
             WHERE s.is_subagent = 0
               AND date(t.timestamp, 'localtime') >= ?"""
    params: list = [cutoff]
    if project_key:
        proj_dir = _project_dir_for_key(project_key)
        if not proj_dir:
            return []
        sql += " AND s.project_dir = ?"
        params.append(proj_dir)
    elif tracked_only:
        dirs = _tracked_project_dirs()
        if not dirs:
            return []
        placeholders = ",".join("?" for _ in dirs)
        sql += f" AND s.project_dir IN ({placeholders})"
        params.extend(dirs)
    sql += f" GROUP BY t.tool_name ORDER BY n DESC LIMIT {limit}"

    with sqlite3.connect(str(CORPUS_DB)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, params).fetchall()
    return [{"tool": r["tool_name"], "count": r["n"], "errors": r["errors"] or 0} for r in rows]


def streak_stats(
    project_key: str | None = None,
    weeks: int = 26,
    tracked_only: bool = False,
) -> dict:
    """Current and longest consecutive-active-day streak in the calendar window.
    'Active' = at least one prompt that day."""
    if project_key:
        daily = activity_calendar(project_key, weeks=weeks)
    else:
        daily = activity_calendar_all(weeks=weeks, tracked_only=tracked_only)
    if not daily:
        return {"current": 0, "longest": 0, "longest_end": None}

    today_str = date.today().isoformat()

    # Current streak: count consecutive active days ending at today (or last activity).
    current = 0
    for d, n in reversed(daily):
        if d > today_str:
            continue
        if n > 0:
            current += 1
        else:
            break

    # Longest streak in the window.
    longest = 0
    longest_end = None
    run = 0
    run_end = None
    for d, n in daily:
        if n > 0:
            run += 1
            run_end = d
            if run > longest:
                longest = run
                longest_end = run_end
        else:
            run = 0
            run_end = None
    return {"current": current, "longest": longest, "longest_end": longest_end}


def hbar_chart_svg(
    rows: list[tuple[str, float]],
    width: int = 360,
    bar_height: int = 18,
    gap: int = 4,
    color: str = "#2563eb",
    label_width: int = 110,
    value_fmt: str = "{:,.0f}",
) -> str:
    """Inline-SVG horizontal bar chart. Each row is (label, value), top-down
    in the order given. Bar widths are scaled to the row with the largest
    value. Empty input renders an empty SVG so the template can plug it in
    without branching. Used for top-tools-per-repo, frustration-per-repo,
    and similar low-cardinality categorical charts that read better as
    horizontal bars than as a vertical chart."""
    if not rows:
        return f'<svg width="{width}" height="{bar_height + gap}"></svg>'
    height = (bar_height + gap) * len(rows)
    bars_width = width - label_width - 60  # leave room for value text on the right
    max_v = max((v for _, v in rows), default=0) or 1
    parts = [
        f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif">'
    ]
    for i, (label, value) in enumerate(rows):
        y = i * (bar_height + gap)
        bar_w = max(2, (value / max_v) * bars_width) if value > 0 else 0
        # Truncate long labels so the chart never overflows.
        label_text = (label[: label_width // 7] + "…") if len(label) > label_width // 7 else label
        parts.append(
            f'<text x="0" y="{y + bar_height * 0.72:.1f}" font-size="11" fill="#374151">{_xml_escape(label_text)}</text>'
            f'<rect x="{label_width}" y="{y}" width="{bar_w:.1f}" height="{bar_height}" rx="2" ry="2" fill="{color}" fill-opacity="0.85"/>'
            f'<text x="{label_width + bar_w + 4:.1f}" y="{y + bar_height * 0.72:.1f}" font-size="11" fill="#6b7280" font-variant-numeric="tabular-nums">{_xml_escape(value_fmt.format(value))}</text>'
        )
    parts.append("</svg>")
    return "".join(parts)


def _xml_escape(s) -> str:
    """Minimal escaper for label/value text inside SVG <text> nodes. Avoids
    a stdlib `html` import; only need <, >, &, ", '."""
    s = str(s)
    return (
        s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        .replace('"', "&quot;").replace("'", "&#39;")
    )


def sparkline_svg(values: Iterable[float], width: int = 220, height: int = 40, color: str = "#2563eb") -> str:
    """Tiny inline-SVG line/area sparkline. Zero-deps, renders in any browser."""
    vals = list(values)
    if not vals:
        return f'<svg width="{width}" height="{height}"></svg>'
    if max(vals) == 0 and min(vals) == 0:
        return f'<svg width="{width}" height="{height}"><line x1="0" y1="{height/2}" x2="{width}" y2="{height/2}" stroke="#e5e7eb" stroke-width="1"/></svg>'
    lo, hi = min(vals), max(vals)
    rng = (hi - lo) or 1
    n = len(vals)
    dx = width / max(n - 1, 1)
    points = []
    for i, v in enumerate(vals):
        x = i * dx
        y = height - ((v - lo) / rng) * (height - 4) - 2
        points.append(f"{x:.1f},{y:.1f}")
    pts_str = " ".join(points)
    area_pts = f"0,{height} {pts_str} {width},{height}"
    return (
        f'<svg width="{width}" height="{height}" preserveAspectRatio="none" viewBox="0 0 {width} {height}">'
        f'<polygon points="{area_pts}" fill="{color}" fill-opacity="0.12"/>'
        f'<polyline points="{pts_str}" fill="none" stroke="{color}" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>'
        f'</svg>'
    )


# ─── Profile card (per-user spider stats) ────────────────────────────────
#
# Shown at the top of /metrics. Six axes computed from corpus.db and the
# bundles directory, normalized 0..1 against tunable elite caps, then
# rendered as a Football Manager–style stat card: 3-column color-coded
# attribute grid, hex spider chart with tinted green→yellow→red rings,
# procedural "player traits" derived from the stats.
#
# The caps are deliberately reachable — fresh users still get a card
# (Newcomer archetype, rating 40), and a power user with months of
# tracked sessions ends up in the 80s/90s.

CARD_AXES = ("throughput", "frugality", "reliability", "curiosity", "range", "mastery")

# Elite caps for normalization. A user hitting all six caps lands at 99.
# Tuned against my own ~3-month corpus to put a heavy user in the 85-95 band.
_CARD_CAPS = {
    "throughput":    40.0,   # prompts per active day
    "frugality":     0.04,   # USD per prompt — LOWER is better (inverted below)
    "reliability":   1.0,    # 1 - error rate; already 0..1
    "curiosity":     30.0,   # distinct tool names used
    "range":         12.0,   # distinct project dirs touched
    "mastery":       25.0,   # curated skills across all bundles
}

# Archetype name + 1-line flavor, keyed by the dominant axis.
_ARCHETYPES = {
    "throughput":   ("Speedrunner",   "Volume player. Ships in bursts, iterates fast."),
    "frugality":    ("Minimalist",    "Wastes no tokens. Surgical prompts."),
    "reliability":  ("Perfectionist", "Tool calls land. Errors are rare."),
    "curiosity":    ("Explorer",      "Reaches for new tools instead of the same hammer."),
    "range":        ("Polyglot",      "Hops repos without losing context."),
    "mastery":      ("Curator",       "Turns sessions into reusable skill bundles."),
}

# Returned when every axis is below 0.15 — no data yet to characterize.
_NEWCOMER = ("Newcomer", "Brand new corpus. Stats sharpen as you use it.")
_GENERALIST = ("Generalist", "Balanced across the board. No single dominant strength.")


def _normalize(value: float, cap: float, *, invert: bool = False) -> float:
    """Map a raw axis value into 0..1, capped. `invert=True` is for axes
    where lower is better (cost-per-prompt → frugality)."""
    if cap <= 0:
        return 0.0
    if invert:
        if value <= 0:
            return 1.0
        return max(0.0, min(1.0, cap / value))
    return max(0.0, min(1.0, value / cap))


def _kind_from_norm(score: float) -> str:
    """Bucket a 0..1 normalized score into the 3 color tiers used by the
    profile card's stat columns. Tuned so most stats fall into "mid"
    rather than the extremes — green should feel earned."""
    if score >= 0.7:
        return "elite"
    if score >= 0.4:
        return "mid"
    return "low"


def compute_card_stats(days: int = 90) -> dict:
    """Inputs for the Football Manager–style profile card shown on
    /metrics. Pulls from corpus.db over the last `days` days plus the
    BUNDLES_DIR for the always-current mastery axis. Returns a dict
    with:

      - axes:           {axis → 0..1 normalized}
      - axes_raw:       {axis → human-readable raw value}
      - rating:         int 40..99 (FIFA convention; floor so a Newcomer
                        still gets a respectable card)
      - archetype:      (name, flavor) tuple
      - attribute_groups: list of 3 dicts (Volume / Efficiency / Breadth)
                          each with {label, stats: [{label, raw, kind}]}.
                          kind is "elite" / "mid" / "low" / "neutral"; the
                          template just renders, no math. The key name is
                          "stats" rather than "items" because Jinja's
                          `group.items` resolves to dict.items() first.
      - traits:         list of strings — procedural badges like
                        "Codex-first", "Tool collector".
      - sessions, prompts, cost_usd, active_days, tool_calls, tool_errors,
        projects, distinct_tools, distinct_agents, agents (dict),
        cache_hit_ratio, prompts_per_session, cost_per_session
      - first_seen, last_seen, top_agent, top_tool

    Empty corpus → Newcomer card, rating 40, every axis 0.
    """
    from watchmen.paths import BUNDLES_DIR

    out: dict = {
        "axes": {a: 0.0 for a in CARD_AXES},
        "axes_raw": {a: 0.0 for a in CARD_AXES},
        "sessions": 0, "prompts": 0, "cost_usd": 0.0,
        "active_days": 0, "tool_calls": 0, "tool_errors": 0,
        "projects": 0, "distinct_tools": 0, "distinct_agents": 0,
        "input_tokens": 0, "cache_creation_tokens": 0,
        "cache_read_tokens": 0, "output_tokens": 0,
        "agents": {},
        "top_tools": [],
        "first_seen": None, "last_seen": None,
        "top_agent": None, "top_tool": None,
    }

    if CORPUS_DB.exists():
        cutoff = (date.today() - timedelta(days=days - 1)).isoformat()
        with sqlite3.connect(str(CORPUS_DB)) as conn:
            conn.row_factory = sqlite3.Row
            agg = conn.execute(
                """SELECT COUNT(*)                                  AS sessions,
                          COALESCE(SUM(user_prompt_count),0)        AS prompts,
                          COALESCE(SUM(tool_use_count),0)           AS tool_calls,
                          COALESCE(SUM(tool_error_count),0)         AS tool_errors,
                          COALESCE(SUM(cost_usd),0.0)               AS cost_usd,
                          COALESCE(SUM(input_tokens),0)             AS input_tokens,
                          COALESCE(SUM(cache_creation_tokens),0)    AS cache_creation_tokens,
                          COALESCE(SUM(cache_read_tokens),0)        AS cache_read_tokens,
                          COALESCE(SUM(output_tokens),0)            AS output_tokens,
                          COUNT(DISTINCT project_dir)               AS projects,
                          COUNT(DISTINCT date(started_at, 'localtime')) AS active_days,
                          COUNT(DISTINCT agent)                     AS distinct_agents,
                          MIN(date(started_at, 'localtime'))        AS first_seen,
                          MAX(date(started_at, 'localtime'))        AS last_seen
                     FROM sessions
                    WHERE is_subagent = 0
                      AND date(started_at, 'localtime') >= ?""",
                [cutoff],
            ).fetchone()
            for k in ("sessions", "prompts", "tool_calls", "tool_errors",
                      "cost_usd", "input_tokens", "cache_creation_tokens",
                      "cache_read_tokens", "output_tokens", "projects",
                      "active_days", "distinct_agents",
                      "first_seen", "last_seen"):
                out[k] = (agg[k] if agg[k] is not None
                          else (0.0 if k == "cost_usd" else 0))
            out["distinct_tools"] = conn.execute(
                """SELECT COUNT(DISTINCT tc.tool_name) AS n
                     FROM tool_calls tc
                     JOIN sessions s ON s.session_id = tc.session_id
                    WHERE s.is_subagent = 0
                      AND date(s.started_at, 'localtime') >= ?""",
                [cutoff],
            ).fetchone()["n"] or 0
            agent_rows = conn.execute(
                """SELECT agent, COUNT(*) AS n
                     FROM sessions
                    WHERE is_subagent = 0
                      AND date(started_at, 'localtime') >= ?
                 GROUP BY agent ORDER BY n DESC""",
                [cutoff],
            ).fetchall()
            out["agents"] = {r["agent"]: r["n"] for r in agent_rows}
            if agent_rows:
                out["top_agent"] = (agent_rows[0]["agent"], agent_rows[0]["n"])
            tool_rows = conn.execute(
                """SELECT tc.tool_name AS t, COUNT(*) AS n
                     FROM tool_calls tc
                     JOIN sessions s ON s.session_id = tc.session_id
                    WHERE s.is_subagent = 0
                      AND date(s.started_at, 'localtime') >= ?
                 GROUP BY tc.tool_name ORDER BY n DESC LIMIT 8""",
                [cutoff],
            ).fetchall()
            out["top_tools"] = [(r["t"], r["n"]) for r in tool_rows]
            if tool_rows:
                out["top_tool"] = (tool_rows[0]["t"], tool_rows[0]["n"])

    # Mastery from on-disk bundles, independent of the corpus window. A
    # curated skill stays curated even if the source sessions aged out.
    curated_skills = 0
    if BUNDLES_DIR.exists():
        for proj_dir in BUNDLES_DIR.iterdir():
            skills_dir = proj_dir / "skills"
            if skills_dir.is_dir():
                curated_skills += sum(1 for d in skills_dir.iterdir() if d.is_dir())
    out["curated_skills"] = curated_skills

    # Derived stats used by the 3-column attribute grid.
    out["prompts_per_session"] = (out["prompts"] / out["sessions"]) if out["sessions"] else 0.0
    out["cost_per_session"]    = (out["cost_usd"] / out["sessions"]) if out["sessions"] else 0.0
    total_in = out["input_tokens"] + out["cache_creation_tokens"] + out["cache_read_tokens"]
    out["cache_hit_ratio"]     = (out["cache_read_tokens"] / total_in) if total_in else 0.0

    # Raw axis values.
    throughput   = (out["prompts"] / out["active_days"]) if out["active_days"] else 0.0
    cost_per_prm = (out["cost_usd"] / out["prompts"]) if out["prompts"] else 0.0
    reliability  = (1.0 - out["tool_errors"] / out["tool_calls"]) if out["tool_calls"] else 0.0

    out["axes_raw"] = {
        "throughput":  round(throughput, 1),
        "frugality":   round(cost_per_prm, 4),
        "reliability": round(reliability, 3),
        "curiosity":   out["distinct_tools"],
        "range":       out["projects"],
        "mastery":     curated_skills,
    }
    # Frugality + reliability only register when there's source data. A
    # corpus with zero spend or zero tool calls is missing-data, not
    # perfect frugality / perfect reliability — otherwise Newcomers
    # would falsely score 1.0 on both axes from a divide-by-zero.
    frugality_axis = (
        _normalize(cost_per_prm, _CARD_CAPS["frugality"], invert=True)
        if out["prompts"] > 0 and out["cost_usd"] > 0 else 0.0
    )
    reliability_axis = (
        _normalize(reliability, _CARD_CAPS["reliability"])
        if out["tool_calls"] > 0 else 0.0
    )
    out["axes"] = {
        "throughput":  _normalize(throughput,  _CARD_CAPS["throughput"]),
        "frugality":   frugality_axis,
        "reliability": reliability_axis,
        "curiosity":   _normalize(out["distinct_tools"], _CARD_CAPS["curiosity"]),
        "range":       _normalize(out["projects"],       _CARD_CAPS["range"]),
        "mastery":     _normalize(curated_skills,        _CARD_CAPS["mastery"]),
    }

    # Rating: weighted average mapped to 40..99 (FIFA convention).
    avg = sum(out["axes"].values()) / len(out["axes"])
    out["rating"] = max(40, min(99, round(40 + 59 * avg)))

    # Archetype: dominant axis if it's clearly ahead, else Generalist.
    if max(out["axes"].values()) < 0.15 and out["sessions"] < 5:
        out["archetype"] = _NEWCOMER
    else:
        top_axis = max(out["axes"], key=out["axes"].get)
        top_val = out["axes"][top_axis]
        runner_up = sorted(out["axes"].values(), reverse=True)[1]
        if top_val >= 0.4 and (runner_up == 0 or top_val / runner_up >= 1.25):
            out["archetype"] = _ARCHETYPES[top_axis]
        else:
            out["archetype"] = _GENERALIST

    out["attribute_groups"] = _compute_attribute_groups(out, days)
    out["traits"] = _compute_traits(out)
    return out


def _compute_attribute_groups(stats: dict, window_days: int) -> list[dict]:
    """Split derived stats into 3 FM-style columns. Each item gets a
    `kind` ("elite" / "mid" / "low" / "neutral") so the template just
    renders without recomputing thresholds. Thresholds scale with the
    window so a 30-day card and a 365-day card both have meaningful
    color coding."""
    # Window-scaled targets — at the elite tier, the user is "on" most
    # days of the window with substantial volume.
    elite_active   = max(7,   window_days * 0.55)
    elite_sessions = max(20,  window_days * 1.1)
    elite_toolcalls = max(500, window_days * 35)

    def stat(label, raw, score, *, neutral=False):
        return {
            "label": label,
            "raw": raw,
            "kind": "neutral" if neutral else _kind_from_norm(score),
        }

    axes = stats["axes"]
    volume = [
        stat("Throughput",
             f"{stats['axes_raw']['throughput']}/d",  axes["throughput"]),
        stat("Sessions",
             f"{stats['sessions']:,}",
             _normalize(stats["sessions"], elite_sessions)),
        stat("Active days",
             f"{stats['active_days']}",
             _normalize(stats["active_days"], elite_active)),
        stat("Tool calls",
             _format_kn(stats["tool_calls"]),
             _normalize(stats["tool_calls"], elite_toolcalls)),
        stat("Prompts / sess",
             f"{stats['prompts_per_session']:.1f}",
             _normalize(stats["prompts_per_session"], 25.0)),
    ]
    efficiency = [
        stat("Reliability",
             f"{int(stats['axes_raw']['reliability'] * 100)}%" if stats["tool_calls"] else "—",
             axes["reliability"]),
        stat("Frugality",
             f"${stats['axes_raw']['frugality']:.3f}/prm" if stats["prompts"] and stats["cost_usd"] else "—",
             axes["frugality"]),
        stat("Cache hit",
             f"{int(stats['cache_hit_ratio'] * 100)}%" if stats["cache_hit_ratio"] else "—",
             _normalize(stats["cache_hit_ratio"], 0.7)),
        stat("Cost / sess",
             f"${stats['cost_per_session']:.2f}" if stats["sessions"] else "—",
             _normalize(stats["cost_per_session"], 5.0, invert=True) if stats["cost_per_session"] else 0.0),
        stat("Total spend",
             f"${stats['cost_usd']:,.2f}", 0.0, neutral=True),
    ]
    top_agent_label = adapter_label(stats["top_agent"][0]) if stats["top_agent"] else "—"
    breadth = [
        stat("Curiosity",
             f"{stats['axes_raw']['curiosity']} tools", axes["curiosity"]),
        stat("Range",
             f"{stats['axes_raw']['range']} repos",     axes["range"]),
        stat("Mastery",
             f"{stats['axes_raw']['mastery']} skills",  axes["mastery"]),
        stat("Agents",
             f"{stats['distinct_agents']}",
             _normalize(stats["distinct_agents"], 3.0)),
        stat("Top agent",
             top_agent_label, 0.0, neutral=True),
    ]
    return [
        {"label": "Volume",     "stats": volume},
        {"label": "Efficiency", "stats": efficiency},
        {"label": "Breadth",    "stats": breadth},
    ]


def _compute_traits(stats: dict) -> list[str]:
    """Procedural one-line badges derived from the stats. Mirrors FM's
    'player traits' field. Order is stable (most distinctive first) and
    duplicates filter out so we never claim two contradicting flavors."""
    out: list[str] = []
    axes = stats["axes"]
    if stats["sessions"] < 5 and max(axes.values()) < 0.15:
        return ["Newcomer"]

    # Agent allegiance — if one agent owns the majority of sessions.
    if stats["top_agent"]:
        slug, n = stats["top_agent"]
        if stats["sessions"] and n / stats["sessions"] >= 0.55:
            out.append(f"{adapter_label(slug)}-first")
    if stats["distinct_agents"] >= 3:
        out.append("Multi-agent")

    if axes["throughput"] >= 0.7:
        out.append("Speedrunner")
    if axes["curiosity"] >= 0.7:
        out.append("Tool collector")
    if axes["range"] >= 0.7:
        out.append("Multi-repo hopper")
    if axes["reliability"] >= 0.95:
        out.append("Reliability master")
    if axes["mastery"] >= 0.5:
        out.append("Curator")
    if stats["cache_hit_ratio"] >= 0.7:
        out.append("Cache wizard")
    if axes["frugality"] > 0 and axes["frugality"] < 0.3:
        out.append("Heavy spender")
    elif axes["frugality"] >= 0.7:
        out.append("Frugal")

    if not out:
        out = ["Generalist"]
    # Cap at 5 so the row doesn't wrap awkwardly.
    return out[:5]


def _format_kn(n: int) -> str:
    """Compact integer formatter: 1500 → 1.5k, 1500000 → 1.5M."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 10_000:
        return f"{n / 1_000:.0f}k"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return f"{n:,}"


def agent_donut_svg(agent_counts: dict, *, size: int = 220, total_label: str = "sessions") -> str:
    """Donut chart of per-agent session shares. Each agent is a colored
    segment, center shows the total + label. Empty input renders a
    muted "no data" donut so the layout doesn't collapse on empty
    corpora."""
    import math
    cx = cy = size / 2
    r_outer = size * 0.42
    r_inner = size * 0.28

    # Adapter palette tuned to match the page's blue/cyan/teal accent
    # trio rather than the prior indigo-everywhere look.
    colors = {
        "claude_code": "#3b82f6",   # blue-500
        "codex":       "#06b6d4",   # cyan-500
        "pi":          "#14b8a6",   # teal-500
    }
    fallback_palette = ["#10b981", "#f59e0b", "#ef4444", "#8b5cf6", "#ec4899"]

    total = sum(agent_counts.values())
    if total == 0:
        return (
            f'<svg viewBox="0 0 {size} {size}" xmlns="http://www.w3.org/2000/svg" role="img">'
            f'<circle cx="{cx}" cy="{cy}" r="{r_outer}" fill="hsl(var(--muted))"/>'
            f'<circle cx="{cx}" cy="{cy}" r="{r_inner}" fill="white"/>'
            f'<text x="{cx}" y="{cy + 4}" text-anchor="middle" font-size="11" '
            f'fill="hsl(var(--muted-foreground))">no data</text>'
            f'</svg>'
        )

    segs: list[str] = []
    angle = -math.pi / 2
    fallback_i = 0
    sorted_agents = sorted(agent_counts.items(), key=lambda kv: kv[1], reverse=True)
    for slug, n in sorted_agents:
        if not n:
            continue
        frac = n / total
        next_angle = angle + 2 * math.pi * frac
        # Avoid a single-segment "Z" arc rendering as nothing — when one
        # agent owns 100%, force a tiny gap so the path is well-formed.
        if frac >= 0.9999:
            next_angle = angle + 2 * math.pi * 0.9999
        large_arc = 1 if frac > 0.5 else 0
        x1 = cx + r_outer * math.cos(angle);    y1 = cy + r_outer * math.sin(angle)
        x2 = cx + r_outer * math.cos(next_angle); y2 = cy + r_outer * math.sin(next_angle)
        ix1 = cx + r_inner * math.cos(next_angle); iy1 = cy + r_inner * math.sin(next_angle)
        ix2 = cx + r_inner * math.cos(angle);    iy2 = cy + r_inner * math.sin(angle)
        color = colors.get(slug)
        if not color:
            color = fallback_palette[fallback_i % len(fallback_palette)]
            fallback_i += 1
        path = (
            f"M {x1:.1f},{y1:.1f} "
            f"A {r_outer:.1f},{r_outer:.1f} 0 {large_arc} 1 {x2:.1f},{y2:.1f} "
            f"L {ix1:.1f},{iy1:.1f} "
            f"A {r_inner:.1f},{r_inner:.1f} 0 {large_arc} 0 {ix2:.1f},{iy2:.1f} Z"
        )
        segs.append(f'<path d="{path}" fill="{color}" stroke="white" stroke-width="2"/>')
        angle = next_angle

    total_str = _format_kn(total)
    return (
        f'<svg viewBox="0 0 {size} {size}" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="agent mix donut">'
        f'{"".join(segs)}'
        f'<text x="{cx}" y="{cy - 4}" text-anchor="middle" font-size="24" font-weight="700" '
        f'fill="hsl(220 9% 18%)" font-variant-numeric="tabular-nums">{total_str}</text>'
        f'<text x="{cx}" y="{cy + 14}" text-anchor="middle" font-size="9" font-weight="600" '
        f'fill="hsl(220 9% 46%)" letter-spacing="0.14em">{total_label.upper()}</text>'
        f'</svg>'
    )


def agent_donut_legend(agent_counts: dict) -> list[dict]:
    """Companion data for the donut: list of dicts with slug, label,
    count, share (0..1), color. Template renders the legend with the
    same color order the donut uses."""
    total = sum(agent_counts.values()) or 1
    colors = {"claude_code": "#3b82f6", "codex": "#06b6d4", "pi": "#14b8a6"}
    fallback = ["#10b981", "#f59e0b", "#ef4444", "#8b5cf6", "#ec4899"]
    out, fb_i = [], 0
    for slug, n in sorted(agent_counts.items(), key=lambda kv: kv[1], reverse=True):
        if not n:
            continue
        if slug in colors:
            color = colors[slug]
        else:
            color = fallback[fb_i % len(fallback)]
            fb_i += 1
        out.append({
            "slug": slug, "label": adapter_label(slug),
            "count": n, "share": n / total, "color": color,
        })
    return out


def card_svg(stats: dict, *, size: int = 440) -> str:
    """Hex spider chart with FM-style tinted concentric rings.

    Red core (0.0–0.25) → orange (0.25–0.5) → yellow (0.5–0.75) →
    green outer ring (0.75–1.0). The user's polygon is drawn on top in
    white with an indigo border so it pops against the tinted rings.

    No header / footer / rating in the SVG — those moved into HTML on
    the profile section of /metrics so labels stay copy-pasteable and
    the SVG fits any column width.
    """
    import math

    cx, cy = size / 2, size / 2
    r = size * 0.42  # outer ring radius
    n = len(CARD_AXES)
    angles = [(-math.pi / 2) + (2 * math.pi * i / n) for i in range(n)]

    def _ring_points(scale: float) -> str:
        return " ".join(
            f"{cx + r * scale * math.cos(a):.1f},{cy + r * scale * math.sin(a):.1f}"
            for a in angles
        )

    # Tinted ring colors, outer → inner. Each band is the area between
    # one ring and the next; we draw the OUTER one first as a green
    # background, then layer smaller polygons of warmer colors on top.
    # That gives the FM "elite zone is green, weak zone is red" effect.
    ring_polys = [
        f'<polygon points="{_ring_points(1.00)}" fill="hsl(142 60% 86%)" stroke="hsl(220 13% 86%)" stroke-width="1"/>',
        f'<polygon points="{_ring_points(0.75)}" fill="hsl(45  90% 90%)" stroke="hsl(220 13% 86%)" stroke-width="1"/>',
        f'<polygon points="{_ring_points(0.50)}" fill="hsl(25  95% 90%)" stroke="hsl(220 13% 86%)" stroke-width="1"/>',
        f'<polygon points="{_ring_points(0.25)}" fill="hsl(0   75% 92%)" stroke="hsl(220 13% 86%)" stroke-width="1"/>',
    ]

    # Spokes from center to outer ring.
    spokes: list[str] = []
    labels: list[str] = []
    for i, a in enumerate(angles):
        x, y = cx + r * math.cos(a), cy + r * math.sin(a)
        spokes.append(
            f'<line x1="{cx:.1f}" y1="{cy:.1f}" x2="{x:.1f}" y2="{y:.1f}" '
            f'stroke="white" stroke-width="1.4" stroke-opacity="0.8"/>'
        )
        # Label sits just past the outer ring.
        lx, ly = cx + (r + 18) * math.cos(a), cy + (r + 18) * math.sin(a)
        if abs(math.cos(a)) < 0.1:
            anchor = "middle"
        elif math.cos(a) > 0:
            anchor = "start"
        else:
            anchor = "end"
        labels.append(
            f'<text x="{lx:.1f}" y="{ly + 4:.1f}" text-anchor="{anchor}" '
            f'font-size="10" font-weight="700" fill="hsl(220 9% 25%)" '
            f'letter-spacing="0.08em">{CARD_AXES[i].upper()}</text>'
        )

    # The user's polygon, on top of the rings.
    user_pts = []
    for i, a in enumerate(angles):
        v = max(0.02, stats["axes"][CARD_AXES[i]])
        x = cx + r * v * math.cos(a)
        y = cy + r * v * math.sin(a)
        user_pts.append(f"{x:.1f},{y:.1f}")
    user_poly = " ".join(user_pts)

    return f'''<svg viewBox="0 0 {size} {size + 36}" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="watchmen profile spider chart">
  <defs>
    <linearGradient id="spider-fill" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%"   stop-color="hsl(217 91% 60%)" stop-opacity="0.75"/>
      <stop offset="100%" stop-color="hsl(189 94% 50%)" stop-opacity="0.55"/>
    </linearGradient>
  </defs>
  {''.join(ring_polys)}
  {''.join(spokes)}
  <polygon points="{user_poly}" data-user="1"
           fill="url(#spider-fill)"
           stroke="hsl(217 91% 50%)" stroke-width="2.5" stroke-linejoin="round"/>
  {''.join(f'<circle cx="{cx + r * max(0.02, stats["axes"][CARD_AXES[i]]) * math.cos(a):.1f}" cy="{cy + r * max(0.02, stats["axes"][CARD_AXES[i]]) * math.sin(a):.1f}" r="3.5" fill="white" stroke="hsl(217 91% 50%)" stroke-width="2"/>' for i, a in enumerate(angles))}
  {''.join(labels)}
</svg>'''


def card_tier_colors(rating: int) -> dict:
    """Return CSS-ready color tokens for the rating tier so the template
    can render the header strip + rating number with matching tints.
    Mirrors FIFA: gold / silver / bronze / indigo (default)."""
    if rating >= 90:
        return {"name": "gold", "from": "#fef3c7", "to": "#f59e0b",
                "text": "#78350f", "border": "#d97706"}
    if rating >= 80:
        return {"name": "silver", "from": "#f3f4f6", "to": "#9ca3af",
                "text": "#1f2937", "border": "#6b7280"}
    if rating >= 70:
        return {"name": "bronze", "from": "#fed7aa", "to": "#c2410c",
                "text": "#7c2d12", "border": "#9a3412"}
    return {"name": "indigo", "from": "#e0e7ff", "to": "#3b82f6",
            "text": "#3730a3", "border": "#2563eb"}


if __name__ == "__main__":
    # Quick CLI for dev: python metrics.py <project_key>
    import sys
    if len(sys.argv) < 2:
        print("usage: python metrics.py <project_key>")
        sys.exit(1)
    rows = daily_metrics(sys.argv[1], days=30)
    for r in rows[:10]:
        print(f"  {r['date']}  sessions={r['sessions']:>2}  prompts={r['prompts']:>3}  errors={r['tool_errors']:>2}  ${r['cost_usd']:>5.2f}  suggestions={r['suggestions_fired']:>2}  uptake={r['uptake']}")
