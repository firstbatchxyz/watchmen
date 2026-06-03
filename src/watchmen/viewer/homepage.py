"""Aggregations powering the mission-control homepage.

Three buckets of data, all read-only against corpus.db + state.db:

  - `impact_strip()`  — three KPI cards (skill calls, tool errors/sess,
                        active repos) with this-7d vs prior-7d deltas.
  - `skill_leaderboard()` — top repos by `Skill` tool invocations in
                            window, with curated-skill count alongside.
  - `weekly_sparkline_data()` — last 12 weeks of skill calls + tool
                                errors-per-session for the trend strip.
  - `status_tiles()` — per-tracked-project health (healthy / stale /
                       uncurated), counts of skills + pending + last
                       activity. Drives the status grid.

Everything degrades to empty/None when corpus.db is absent — the
homepage is still useful on a fresh install with no captured sessions.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from watchmen.paths import BUNDLES_DIR, CORPUS_DB, repo_dir_sql_predicate
from watchmen import state


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _conn_ro() -> sqlite3.Connection | None:
    """Open corpus.db read-only. Returns None when the file is missing."""
    if not CORPUS_DB.exists():
        return None
    c = sqlite3.connect(f"file:{CORPUS_DB}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def _pct_delta(this: float, prior: float) -> float | None:
    """Percent change, or None when the prior bucket is empty."""
    if not prior:
        return None
    return (this - prior) / prior * 100.0


# ─── 1. Impact strip — 3 KPI cards with week-over-week delta ────────────────

def impact_strip() -> dict:
    """Four this-7d vs prior-7d KPI cards.

    Returns a dict with keys `skill_calls`, `tool_errors`, `active_repos`,
    and `subagent_sessions` — each having {this, prior, delta_pct, fmt}.
    `subagent_sessions` additionally carries `cost_share_pct` (this 7d's
    subagent cost / total cost, for the subtitle).  `fmt` is a hint for
    the template — int for counts, float-1 for per-session means.

    All values fall back to zero when corpus.db is missing.  delta_pct
    is None when the prior week was zero (avoid divide-by-zero, render
    as a dash).
    """
    out = {
        "skill_calls":       {"this": 0,   "prior": 0,   "delta_pct": None, "fmt": "int"},
        "tool_errors":       {"this": 0.0, "prior": 0.0, "delta_pct": None, "fmt": "float1"},
        "active_repos":      {"this": 0,   "prior": 0,   "delta_pct": None, "fmt": "int"},
        "subagent_sessions": {"this": 0,   "prior": 0,   "delta_pct": None, "fmt": "int",
                              "cost_share_pct": None},
    }
    cc = _conn_ro()
    if cc is None:
        return out
    try:
        now = _now()
        cutoff_7  = (now - timedelta(days=7)).isoformat()
        cutoff_14 = (now - timedelta(days=14)).isoformat()

        # Skill calls
        row = cc.execute(
            "SELECT "
            " SUM(CASE WHEN timestamp >= ? THEN 1 ELSE 0 END) AS this_, "
            " SUM(CASE WHEN timestamp >= ? AND timestamp < ? THEN 1 ELSE 0 END) AS prior_ "
            "FROM tool_calls WHERE tool_name = 'Skill' AND timestamp >= ?",
            (cutoff_7, cutoff_14, cutoff_7, cutoff_14),
        ).fetchone()
        out["skill_calls"]["this"]  = row["this_"]  or 0
        out["skill_calls"]["prior"] = row["prior_"] or 0
        out["skill_calls"]["delta_pct"] = _pct_delta(out["skill_calls"]["this"], out["skill_calls"]["prior"])

        # Tool errors per session (mean), over the same windows
        for label, start, end in [("this", cutoff_7, None), ("prior", cutoff_14, cutoff_7)]:
            sql = (
                "SELECT AVG(CAST(tool_error_count AS FLOAT)) AS m "
                "FROM sessions WHERE is_subagent = 0 AND started_at >= ?"
            )
            args: tuple = (start,)
            if end:
                sql += " AND started_at < ?"
                args = (start, end)
            v = cc.execute(sql, args).fetchone()["m"]
            out["tool_errors"][label] = float(v or 0.0)
        out["tool_errors"]["delta_pct"] = _pct_delta(out["tool_errors"]["this"], out["tool_errors"]["prior"])

        # Active repos (distinct project_dir with ≥1 session)
        for label, start, end in [("this", cutoff_7, None), ("prior", cutoff_14, cutoff_7)]:
            sql = (
                "SELECT COUNT(DISTINCT project_dir) AS n "
                "FROM sessions WHERE is_subagent = 0 AND started_at >= ?"
            )
            args = (start,)
            if end:
                sql += " AND started_at < ?"
                args = (start, end)
            out["active_repos"][label] = cc.execute(sql, args).fetchone()["n"] or 0
        out["active_repos"]["delta_pct"] = _pct_delta(out["active_repos"]["this"], out["active_repos"]["prior"])

        # Subagent sessions started in each window + cost share for the
        # current window. Count is what the card value shows; cost share
        # rides in the subtitle as the headline qualifier ("16% of cost"),
        # since count alone is misleading (subagents dominate count even
        # when they're a minority of spend — see the launch session that
        # found 97%-by-count but only 17%-by-cost).
        for label, start, end in [("this", cutoff_7, None), ("prior", cutoff_14, cutoff_7)]:
            sql = (
                "SELECT COUNT(*) AS n "
                "FROM sessions WHERE is_subagent = 1 AND started_at >= ?"
            )
            args = (start,)
            if end:
                sql += " AND started_at < ?"
                args = (start, end)
            out["subagent_sessions"][label] = cc.execute(sql, args).fetchone()["n"] or 0
        out["subagent_sessions"]["delta_pct"] = _pct_delta(
            out["subagent_sessions"]["this"], out["subagent_sessions"]["prior"]
        )

        # Current-window cost share: SUM(sub cost) / SUM(total cost) over
        # the last 7d.  Stays None when the window has no cost at all so
        # the template can render a dash instead of a misleading 0%.
        row = cc.execute(
            "SELECT "
            "  COALESCE(SUM(CASE WHEN is_subagent=1 THEN cost_usd ELSE 0 END), 0) AS sub_cost, "
            "  COALESCE(SUM(cost_usd), 0) AS total_cost "
            "FROM sessions WHERE started_at >= ?",
            (cutoff_7,),
        ).fetchone()
        sub_c = float(row["sub_cost"] or 0.0)
        tot_c = float(row["total_cost"] or 0.0)
        out["subagent_sessions"]["cost_share_pct"] = (
            (100.0 * sub_c / tot_c) if tot_c > 0 else None
        )
    finally:
        cc.close()
    return out


# ─── 2. Skill leaderboard — top repos by Skill calls this week ──────────────

def skill_leaderboard(window_days: int = 7, limit: int = 8) -> list[dict]:
    """Top projects by `Skill` tool invocations in the last N days.

    Returns a list of {project_dir, project_name, project_key, count,
    skills_curated, max_count} sorted desc by count.  `max_count` is the
    leader's count, used by the template to render proportional bars
    without computing it in Jinja.
    """
    cc = _conn_ro()
    if cc is None:
        return []
    try:
        cutoff = (_now() - timedelta(days=window_days)).isoformat()
        rows = cc.execute(
            """
            SELECT s.project_dir AS project_dir, COUNT(*) AS n
            FROM tool_calls t
            JOIN sessions s ON s.session_id = t.session_id
            WHERE t.tool_name = 'Skill' AND t.timestamp >= ? AND s.is_subagent = 0
            GROUP BY s.project_dir ORDER BY n DESC LIMIT ?
            """,
            (cutoff, limit),
        ).fetchall()
    finally:
        cc.close()
    if not rows:
        return []

    # Map project_dir → curated-skills count by walking state.db once.
    state.init_db()
    projects = {p["source_repo"]: p for p in state.list_projects()}

    max_count = rows[0]["n"]
    out: list[dict] = []
    for r in rows:
        path = r["project_dir"] or ""
        proj = projects.get(path)
        name = proj["project_key"] if proj else (Path(path).name or "(unknown)")
        # Count curated skills via BUNDLES_DIR (watchmen's view).  Same
        # accounting as actions.py: a sub-directory under
        # bundles/<key>/skills/ counts as one curated skill.
        skills_n = 0
        if proj:
            skills_root = BUNDLES_DIR / proj["project_key"] / "skills"
            if skills_root.exists():
                skills_n = sum(1 for x in skills_root.iterdir() if x.is_dir())
        out.append({
            "project_dir": path,
            "project_name": name,
            "project_key": proj["project_key"] if proj else None,
            "count": r["n"],
            "skills_curated": skills_n,
            "max_count": max_count,
        })
    return out


# ─── 3. Weekly sparklines — last 12 weeks of skill calls + tool errors ──────

def weekly_sparkline_data(weeks: int = 12) -> dict:
    """Last N completed-or-current ISO weeks of two metrics.

    Returns
      { "skill_calls": [{date, value}, …],
        "tool_errors": [{date, value}, …] }
    The `date` for a week is the Monday of that ISO week, so the chart
    renders along a real time axis (not just labels).  Empty list when
    corpus.db is missing.
    """
    cc = _conn_ro()
    if cc is None:
        return {"skill_calls": [], "tool_errors": []}
    try:
        now = _now()
        # Anchor at the Monday of (now - weeks weeks). All buckets aligned to ISO weeks.
        anchor = now - timedelta(weeks=weeks)
        anchor_iso = anchor.isoformat()
        # Skill calls per week
        skill_rows = cc.execute(
            """
            SELECT strftime('%Y-%W', timestamp) AS yw, MIN(timestamp) AS first_ts, COUNT(*) AS n
            FROM tool_calls WHERE tool_name = 'Skill' AND timestamp >= ?
            GROUP BY yw ORDER BY yw
            """,
            (anchor_iso,),
        ).fetchall()
        # Tool errors per session, weekly mean
        err_rows = cc.execute(
            """
            SELECT strftime('%Y-%W', started_at) AS yw, MIN(started_at) AS first_ts,
                   AVG(CAST(tool_error_count AS FLOAT)) AS m
            FROM sessions WHERE is_subagent = 0 AND started_at >= ?
            GROUP BY yw ORDER BY yw
            """,
            (anchor_iso,),
        ).fetchall()
    finally:
        cc.close()

    def to_series(rs, key) -> list[dict]:
        # Use the first timestamp in the bucket as the label so the chart
        # has a real ISO date to plot against, not just "YYYY-WW".
        out = []
        for r in rs:
            ts = r["first_ts"] or ""
            iso_date = ts[:10] if ts else ""
            v = r[key] or 0
            out.append({"date": iso_date, "value": float(v) if isinstance(v, float) else int(v)})
        return out

    return {
        "skill_calls": to_series(skill_rows, "n"),
        "tool_errors": to_series(err_rows, "m"),
    }


# ─── 4. Status tiles — per-project health glance ────────────────────────────

def status_tiles() -> list[dict]:
    """Compact per-tracked-project status for the grid.

    Each tile carries:
      - project_key, source_repo
      - skills_n (curated SKILL.md files on disk)
      - pending_n (from state.skill_candidates)
      - sessions_7d (corpus.db count)
      - last_curator_run (state.projects.last_curator_run, ISO string or None)
      - status: 'healthy' | 'stale' | 'uncurated'
        * uncurated → 0 skills curated yet
        * stale     → last curator run > 30 days ago
        * healthy   → has skills and curator ran recently
    """
    state.init_db()
    projects = state.list_projects()
    cc = _conn_ro()
    sessions_by_path: dict[str, int] = {}
    if cc is not None:
        try:
            cutoff = (_now() - timedelta(days=7)).isoformat()
            rows = cc.execute(
                "SELECT project_dir, COUNT(*) AS n FROM sessions "
                "WHERE is_subagent = 0 AND started_at >= ? GROUP BY project_dir",
                (cutoff,),
            ).fetchall()
            sessions_by_path = {r["project_dir"]: r["n"] for r in rows if r["project_dir"]}
        finally:
            cc.close()

    out: list[dict] = []
    for p in projects:
        # Same accounting as actions._skills_count / _pending_count: count
        # sub-dirs under BUNDLES_DIR/<key>/{skills, _pending}.
        skills_root = BUNDLES_DIR / p["project_key"] / "skills"
        skills_n = sum(1 for x in skills_root.iterdir() if x.is_dir()) if skills_root.exists() else 0
        pending_root = BUNDLES_DIR / p["project_key"] / "_pending"
        pending_n = sum(1 for x in pending_root.iterdir() if x.is_dir()) if pending_root.exists() else 0

        last_run = p.get("last_curator_run")
        days_since_curator: int | None = None
        if last_run:
            try:
                ts = datetime.fromisoformat(last_run.replace("Z", "+00:00"))
                days_since_curator = (_now() - ts).days
            except ValueError:
                pass

        if skills_n == 0:
            status = "uncurated"
        elif days_since_curator is not None and days_since_curator > 30:
            status = "stale"
        else:
            status = "healthy"

        out.append({
            "project_key": p["project_key"],
            "source_repo": p["source_repo"],
            "skills_n": skills_n,
            "pending_n": pending_n,
            "sessions_7d": sessions_by_path.get(p["source_repo"], 0),
            "last_curator_run": last_run,
            "days_since_curator": days_since_curator,
            "status": status,
        })
    return out


# ─── 5. Per-project impact card  ────────────────────────────────────────────

def _median(xs: list[float]) -> float:
    """Plain median, returns 0.0 on empty input (matches the rest of this module's
    "degrade-gracefully" style)."""
    if not xs:
        return 0.0
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def _treatment_date_for_project(project_key: str) -> datetime | None:
    """Earliest skill-curation timestamp for this project.

    Prefers `state.runs` (recorded events) but falls back to SKILL.md file
    mtimes when `runs` is empty — early curator runs predate the runs-table
    schema, so SKILL.md mtime is the only ground truth for those projects.
    """
    # `state.conn()` is a @contextmanager — must be entered with `with`.
    # The previous code did `state.conn().execute(...)`, which threw an
    # AttributeError that the broad `except Exception` swallowed silently;
    # the function then always fell through to the SKILL.md-mtime fallback.
    # Symptom: impact card's treatment date matched whenever the bundle
    # was last regenerated, not when the curator first landed.
    try:
        with state.conn() as c:
            rows = list(
                c.execute(
                    "SELECT MIN(started_at) AS first_run FROM runs "
                    "WHERE project_key=? AND kind LIKE 'curator%' AND status='ok'",
                    (project_key,),
                )
            )
        if rows and rows[0]["first_run"]:
            try:
                return datetime.fromisoformat(rows[0]["first_run"].replace("Z", "+00:00"))
            except ValueError:
                pass
    except Exception:
        pass
    # Fallback: file-mtime of the earliest SKILL.md under bundles/<key>/skills/
    skills_root = BUNDLES_DIR / project_key / "skills"
    if skills_root.exists():
        mtimes: list[float] = []
        for sd in skills_root.iterdir():
            sk = sd / "SKILL.md"
            if sk.exists():
                mtimes.append(sk.stat().st_mtime)
        if mtimes:
            return datetime.fromtimestamp(min(mtimes), tz=timezone.utc)
    return None


def project_impact(project_key: str, weeks: int = 16) -> dict:
    """Per-project before/after view for the Impact card on /p/<key>.

    Returns a dict shaped for the template — same `degrade-gracefully`
    contract as the other functions: missing corpus.db or no sessions
    yields `enough_data: False` with `verdict_reason` filled in so the
    template can render a polite empty state.

    Schema:
      treatment_date         : ISO date string  (or None)
      treatment_bucket_date  : ISO date of the *weekly bucket* that
                               contains the treatment date — used by the
                               chart to position its dashed annotation
      days_since_treatment   : int (or None)
      weekly_series          : [{date, value, sessions}] — value is
                               mean tool_error_count for that week
      pre  / post            : {sessions_n, median_errors,
                                median_prompts, median_cost}
      verdict                : short string describing the direction of
                               the deltas; never makes a causation claim
      enough_data            : bool — True iff treatment_date is set AND
                               pre/post each have ≥3 sessions
      verdict_reason         : string explaining why enough_data is False
                               (only set when False)
    """
    out = {
        "treatment_date": None,
        "treatment_bucket_date": None,
        "days_since_treatment": None,
        "weekly_series": [],
        "pre": {"sessions_n": 0, "median_errors": 0.0, "median_prompts": 0.0, "median_cost": 0.0},
        "post": {"sessions_n": 0, "median_errors": 0.0, "median_prompts": 0.0, "median_cost": 0.0},
        "verdict": "",
        "verdict_reason": "",
        "enough_data": False,
    }

    proj = state.get_project(project_key)
    if not proj:
        out["verdict_reason"] = "Project not tracked."
        return out
    source_repo = proj["source_repo"]

    treatment = _treatment_date_for_project(project_key)
    if treatment is None:
        out["verdict_reason"] = "No curator runs yet for this project."
        return out
    out["treatment_date"] = treatment.date().isoformat()
    out["days_since_treatment"] = (_now() - treatment).days

    cc = _conn_ro()
    if cc is None:
        out["verdict_reason"] = "corpus.db not found — no session data captured yet."
        return out

    try:
        anchor = (_now() - timedelta(weeks=weeks)).isoformat()
        # Roll up subfolder sessions (#93 shared predicate) so the Impact card
        # counts the same sessions as the swimlane / metrics on this page.
        repo_where, repo_params = repo_dir_sql_predicate(source_repo, "s")
        sessions = list(
            cc.execute(
                "SELECT s.started_at, s.tool_error_count, s.user_prompt_count, s.cost_usd "
                "FROM sessions s WHERE s.is_subagent = 0 AND " + repo_where + " "
                "AND s.started_at >= ? ORDER BY s.started_at",
                (*repo_params, anchor),
            )
        )
    finally:
        cc.close()

    if not sessions:
        out["verdict_reason"] = "No sessions captured for this project in the last 16 weeks."
        return out

    # ── Weekly series (all sessions, project-scoped, mean errors/session)
    buckets: dict[str, list[sqlite3.Row]] = {}
    bucket_first_ts: dict[str, str] = {}
    for s in sessions:
        ts_raw = s["started_at"]
        try:
            ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        iso = ts.isocalendar()
        bk = f"{iso.year}-W{iso.week:02d}"
        if bk not in buckets:
            buckets[bk] = []
            bucket_first_ts[bk] = ts_raw[:10]
        buckets[bk].append(s)
    weekly: list[dict] = []
    for bk in sorted(buckets):
        rs = buckets[bk]
        weekly.append({
            "date": bucket_first_ts[bk],
            "value": sum((r["tool_error_count"] or 0) for r in rs) / len(rs),
            "sessions": len(rs),
        })
    out["weekly_series"] = weekly

    # Map the treatment timestamp to a bucket label that ACTUALLY appears
    # in the series. If the treatment week itself has zero sessions, we
    # fall back to the first bucket date >= treatment, so the marker
    # snaps onto a real category instead of vanishing.
    treat_iso = treatment.isocalendar()
    treat_bk = f"{treat_iso.year}-W{treat_iso.week:02d}"
    if treat_bk in bucket_first_ts:
        out["treatment_bucket_date"] = bucket_first_ts[treat_bk]
    else:
        # First bucket on or after treatment date
        treat_date_str = treatment.date().isoformat()
        for bk in sorted(buckets):
            if bucket_first_ts[bk] >= treat_date_str:
                out["treatment_bucket_date"] = bucket_first_ts[bk]
                break

    # ── Pre / post aggregates ─────────────────────────────────────────
    pre_rows: list[sqlite3.Row] = []
    post_rows: list[sqlite3.Row] = []
    treat_iso_str = treatment.isoformat()
    for s in sessions:
        if (s["started_at"] or "") < treat_iso_str:
            pre_rows.append(s)
        else:
            post_rows.append(s)

    def _agg(rs: list[sqlite3.Row]) -> dict:
        return {
            "sessions_n": len(rs),
            "median_errors":  _median([float(r["tool_error_count"] or 0) for r in rs]),
            "median_prompts": _median([float(r["user_prompt_count"] or 0) for r in rs]),
            "median_cost":    _median([float(r["cost_usd"] or 0.0)         for r in rs]),
        }

    out["pre"]  = _agg(pre_rows)
    out["post"] = _agg(post_rows)

    if out["pre"]["sessions_n"] < 3 or out["post"]["sessions_n"] < 3:
        out["verdict_reason"] = (
            f"Need ≥3 sessions on each side of the curator date "
            f"(have {out['pre']['sessions_n']} pre, {out['post']['sessions_n']} post)."
        )
        return out

    out["enough_data"] = True

    # ── Verdict line (descriptive, not causal) ────────────────────────
    bits: list[str] = []
    if out["pre"]["median_errors"] != out["post"]["median_errors"]:
        d = out["post"]["median_errors"] - out["pre"]["median_errors"]
        bits.append(f"tool errors {'↓' if d < 0 else '↑'}")
    if out["pre"]["median_prompts"] != out["post"]["median_prompts"]:
        d = out["post"]["median_prompts"] - out["pre"]["median_prompts"]
        bits.append(f"prompts/session {'↓' if d < 0 else '↑'}")
    out["verdict"] = ", ".join(bits) if bits else "no measurable change"
    return out
