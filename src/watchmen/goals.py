"""Codex goal ingestion + aggregations.

Codex 0.133.0 (May 2026) introduced first-class goals: one active goal per
thread, persisted in `~/.codex/state_*.sqlite::thread_goals` and updated
live as the agent makes progress. This module reads that state into
`corpus.db::goals` and exposes per-project aggregations the CLI surfaces.

Why a separate table (not a column on `sessions`):

- `sessions` is one-row-per-transcript; one transcript holds one thread.
  Codex goals are 1:1 with threads today, but the data shape (objective
  text, status enum, token budget) is goal-scoped, not session-scoped.
  Hoisting it onto `sessions` would mean every CC/pi/opencode session
  carries six NULL columns forever.
- Future expansion (CC TodoWrite ingestion, deferred to v2) doesn't fit
  the 1:1-with-session model — TodoWrite produces a fluctuating list per
  session. Keeping `goals` decoupled lets that land additively later.

What we read from codex:

- `thread_goals` rows verbatim: goal_id, objective, status, token_budget,
  tokens_used, time_used_seconds, created_at_ms, updated_at_ms.
- `threads.cwd` for project_dir attribution. Joining inside the codex DB
  avoids dependence on watchmen ingestion order (a thread can have a
  goal before its rollout has been scanned into our `sessions`).

What we do NOT read in v1:

- `ThreadGoalUpdated` rollout events. Those carry the per-turn time-
  series of status transitions. Useful for "time-to-completion" or
  "abandonment after N turns" analysis, but the current snapshot in
  `thread_goals` already gives us status + tokens_used + time. Wire the
  time-series later when the use case crystallizes.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from watchmen.paths import CORPUS_DB

_CODEX_DIR = Path.home() / ".codex"

# Valid codex goal statuses per the SQLite CHECK constraint. We mirror exactly
# so an unknown future variant trips the corpus.db CHECK at insert time rather
# than silently corrupting the aggregation.
#
# Codex migration timeline:
#   - Migration 29 "thread goals" (2026-04-13): 4 statuses — active, paused,
#     budget_limited, complete.
#   - Migration 33 "thread goal stopped statuses" (2026-05-22): added
#     `blocked` and `usage_limited`. Migration 34 "drop thread goals"
#     simultaneously moved the table from state_*.sqlite into a dedicated
#     goals_*.sqlite (PR #23300).
_VALID_STATUSES = {
    "active", "paused", "blocked", "usage_limited", "budget_limited", "complete",
}


def _conn_ro() -> sqlite3.Connection | None:
    if not CORPUS_DB.exists():
        return None
    c = sqlite3.connect(f"file:{CORPUS_DB}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def _ms_to_iso(ms: int | None) -> str | None:
    """Codex stores timestamps as millisecond epochs. Mirror watchmen's
    convention (ISO 8601 UTC) so downstream rendering is consistent."""
    if ms is None:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()
    except (OSError, OverflowError, ValueError):
        return None


def _find_codex_dbs() -> tuple[Path | None, Path | None]:
    """Resolve the two codex SQLite files we need:

    - **goals DB**: ``~/.codex/goals_*.sqlite``. Codex migration 34
      ("drop thread goals", 2026-05-22) moved the goal table into this
      dedicated DB (PR #23300). Pre-migration codex installs keep the
      table inside the state DB instead — in that case this returns
      ``None`` for the goals DB and we fall back to reading goals from
      the state DB directly.
    - **state DB**: ``~/.codex/state_*.sqlite``. Always the source for
      the ``threads.cwd`` lookup we use for project_dir attribution.

    Both filenames are version-suffixed; we pick the highest-numbered file
    so we follow codex forward on schema bumps without code changes."""
    if not _CODEX_DIR.exists():
        return None, None
    state = sorted(_CODEX_DIR.glob("state_*.sqlite"))
    goals = sorted(_CODEX_DIR.glob("goals_*.sqlite"))
    return (goals[-1] if goals else None), (state[-1] if state else None)


def sync_from_codex(
    conn: sqlite3.Connection,
    *,
    goals_db: Path | None = None,
    state_db: Path | None = None,
) -> int:
    """Read codex thread_goals into corpus.db::goals. Returns the number of
    rows written. No-op if codex isn't installed or has no goals yet.

    Reads from the dedicated `goals_*.sqlite` (codex 0.133.0+ post-
    migration-34) when present, and falls back to `state_*.sqlite` for
    older codex installs that still keep `thread_goals` colocated with
    `threads`. In either case `threads.cwd` always comes from the state
    DB — that table never moved.

    Strategy: full replace per sync. The codex table is small (one row
    per thread that ever had a goal) and we don't track per-thread
    mtimes, so incremental sync would be more code than it's worth.
    UPSERTs into goals so existing rows get refreshed token counts /
    status when codex updates them.
    """
    resolved_goals = goals_db
    resolved_state = state_db
    if resolved_goals is None and resolved_state is None:
        resolved_goals, resolved_state = _find_codex_dbs()

    # The threads table lives in state — we need it for project_dir.
    if resolved_state is None or not resolved_state.exists():
        return 0

    try:
        src = sqlite3.connect(f"file:{resolved_state}?mode=ro", uri=True)
    except sqlite3.Error:
        return 0

    src.row_factory = sqlite3.Row
    try:
        # Prefer the dedicated goals DB (post-migration-34 codex). When it
        # exists, ATTACH it to the open state-DB connection so we can JOIN
        # `goals_db.thread_goals` against `threads` in one SQL statement.
        # Otherwise read from `thread_goals` colocated inside the state DB.
        if resolved_goals is not None and resolved_goals.exists():
            try:
                src.execute(
                    f"ATTACH DATABASE 'file:{resolved_goals}?mode=ro' AS goals_db"
                )
            except sqlite3.OperationalError:
                # ATTACH can fail on some sqlite builds with URI mode + ro;
                # retry with a plain path.
                src.execute(f"ATTACH DATABASE '{resolved_goals}' AS goals_db")
            goals_source = "goals_db.thread_goals"
        else:
            goals_source = "thread_goals"

        # JOIN to threads for project_dir (cwd). LEFT JOIN so a goal with a
        # missing thread row (orphan, shouldn't happen but be defensive)
        # still lands with project_dir=NULL rather than vanishing.
        try:
            rows = src.execute(
                f"""
                SELECT g.thread_id,
                       g.goal_id,
                       g.objective,
                       g.status,
                       g.token_budget,
                       g.tokens_used,
                       g.time_used_seconds,
                       g.created_at_ms,
                       g.updated_at_ms,
                       t.cwd AS project_dir
                FROM {goals_source} g
                LEFT JOIN threads t ON t.id = g.thread_id
                """
            ).fetchall()
        except sqlite3.OperationalError:
            # Either: the dedicated goals DB exists but has no `thread_goals`
            # yet (codex created the file but hasn't populated it), or the
            # state DB doesn't have `thread_goals` (post-migration-34 with
            # no goals_db.sqlite). Either way, empty result.
            return 0
    finally:
        src.close()

    written = 0
    for r in rows:
        status = r["status"]
        if status not in _VALID_STATUSES:
            # Skip rather than crash — a future codex schema extension
            # (e.g. a new "blocked" status) shouldn't break ingestion.
            continue
        conn.execute(
            """
            INSERT INTO goals
                (goal_id, thread_id, project_dir, objective, status,
                 token_budget, tokens_used, time_used_seconds,
                 created_at, updated_at, agent)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'codex')
            ON CONFLICT(goal_id) DO UPDATE SET
                thread_id = excluded.thread_id,
                project_dir = excluded.project_dir,
                objective = excluded.objective,
                status = excluded.status,
                token_budget = excluded.token_budget,
                tokens_used = excluded.tokens_used,
                time_used_seconds = excluded.time_used_seconds,
                updated_at = excluded.updated_at
            """,
            (
                r["goal_id"],
                r["thread_id"],
                r["project_dir"],
                r["objective"],
                status,
                r["token_budget"],
                int(r["tokens_used"] or 0),
                int(r["time_used_seconds"] or 0),
                _ms_to_iso(r["created_at_ms"]),
                _ms_to_iso(r["updated_at_ms"]),
            ),
        )
        written += 1
    conn.commit()
    return written


# ─── Result dataclasses ───────────────────────────────────────────────────


@dataclass
class GoalRow:
    """One goal row, joined with whatever per-session data is reachable."""

    goal_id: str
    thread_id: str
    project_dir: str | None
    objective: str
    status: str
    token_budget: int | None
    tokens_used: int
    time_used_seconds: int
    created_at: str | None
    updated_at: str | None
    cost_usd: float  # approximated from sessions.cost_usd via thread_id join


@dataclass
class ProjectGoalSummary:
    """Goal-level rollup for one project."""

    project_key: str
    goal_count: int
    completed: int
    active: int
    paused: int
    budget_limited: int
    total_tokens_used: int
    total_cost_usd: float
    total_time_seconds: int

    @property
    def completion_rate_pct(self) -> float | None:
        if self.goal_count == 0:
            return None
        return 100.0 * self.completed / self.goal_count


# ─── Aggregations ─────────────────────────────────────────────────────────


def list_for_project(project_key: str, source_repo: str, *, limit: int = 50) -> list[GoalRow]:
    """All goals associated with one project, joined to session cost.

    Cost attribution: codex models one goal per thread, so the session's
    `cost_usd` is a faithful proxy for the goal's cost when the goal spans
    the whole session. Precise per-goal cost (when the user has multiple
    goals over a thread's lifetime — not codex's model today) would need
    rollout-event replay, deferred to v2.
    """
    # Avoid the heavier `_project_dir_predicate` import surface from
    # watchmen.state — for goals we just match on project_dir LIKE %key%
    # to mirror how subagents.aggregate_for_project filters. We accept the
    # source_repo arg for future use (LIKE on the absolute repo path) but
    # don't need it today since codex writes the same cwd into threads.cwd.
    del source_repo  # reserved for v2 — see docstring
    conn = _conn_ro()
    if conn is None:
        return []
    try:
        rows = conn.execute(
            """
            SELECT g.goal_id, g.thread_id, g.project_dir, g.objective, g.status,
                   g.token_budget, g.tokens_used, g.time_used_seconds,
                   g.created_at, g.updated_at,
                   COALESCE(s.cost_usd, 0.0) AS cost_usd
            FROM goals g
            LEFT JOIN sessions s ON s.session_id = g.thread_id
            WHERE g.project_dir LIKE ?
            ORDER BY COALESCE(s.cost_usd, 0.0) DESC, g.tokens_used DESC
            LIMIT ?
            """,
            (f"%{project_key}%", limit),
        ).fetchall()
    finally:
        conn.close()
    return [
        GoalRow(
            goal_id=r["goal_id"],
            thread_id=r["thread_id"],
            project_dir=r["project_dir"],
            objective=r["objective"],
            status=r["status"],
            token_budget=r["token_budget"],
            tokens_used=int(r["tokens_used"] or 0),
            time_used_seconds=int(r["time_used_seconds"] or 0),
            created_at=r["created_at"],
            updated_at=r["updated_at"],
            cost_usd=float(r["cost_usd"] or 0.0),
        )
        for r in rows
    ]


def aggregate_per_project() -> list[ProjectGoalSummary]:
    """One ProjectGoalSummary per project that has at least one goal. Sorted
    by total cost descending so the most expensive goal-bearing projects
    surface first in the overview table."""
    conn = _conn_ro()
    if conn is None:
        return []
    try:
        rows = conn.execute(
            """
            SELECT g.project_dir AS project_dir,
                   COUNT(*) AS goal_count,
                   SUM(CASE WHEN g.status = 'complete' THEN 1 ELSE 0 END) AS completed,
                   SUM(CASE WHEN g.status = 'active' THEN 1 ELSE 0 END) AS active,
                   SUM(CASE WHEN g.status = 'paused' THEN 1 ELSE 0 END) AS paused,
                   SUM(CASE WHEN g.status = 'budget_limited' THEN 1 ELSE 0 END) AS budget_limited,
                   SUM(g.tokens_used) AS total_tokens,
                   SUM(g.time_used_seconds) AS total_time,
                   COALESCE(SUM(s.cost_usd), 0.0) AS total_cost
            FROM goals g
            LEFT JOIN sessions s ON s.session_id = g.thread_id
            WHERE g.project_dir IS NOT NULL
            GROUP BY g.project_dir
            ORDER BY total_cost DESC, total_tokens DESC
            """
        ).fetchall()
    finally:
        conn.close()
    return [
        ProjectGoalSummary(
            project_key=r["project_dir"] or "(unknown)",
            goal_count=int(r["goal_count"] or 0),
            completed=int(r["completed"] or 0),
            active=int(r["active"] or 0),
            paused=int(r["paused"] or 0),
            budget_limited=int(r["budget_limited"] or 0),
            total_tokens_used=int(r["total_tokens"] or 0),
            total_cost_usd=float(r["total_cost"] or 0.0),
            total_time_seconds=int(r["total_time"] or 0),
        )
        for r in rows
    ]


def totals() -> dict:
    """Headline numbers for the overview heading: total goals, status mix."""
    conn = _conn_ro()
    if conn is None:
        return {"goal_count": 0, "completed": 0, "active": 0, "paused": 0,
                "budget_limited": 0, "total_cost_usd": 0.0}
    try:
        r = conn.execute(
            """
            SELECT COUNT(*) AS goal_count,
                   SUM(CASE WHEN status = 'complete' THEN 1 ELSE 0 END) AS completed,
                   SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END) AS active,
                   SUM(CASE WHEN status = 'paused' THEN 1 ELSE 0 END) AS paused,
                   SUM(CASE WHEN status = 'budget_limited' THEN 1 ELSE 0 END) AS budget_limited,
                   COALESCE((SELECT SUM(s.cost_usd)
                             FROM goals g LEFT JOIN sessions s ON s.session_id = g.thread_id), 0.0)
                       AS total_cost
            FROM goals
            """
        ).fetchone()
    finally:
        conn.close()
    return {
        "goal_count": int(r["goal_count"] or 0),
        "completed": int(r["completed"] or 0),
        "active": int(r["active"] or 0),
        "paused": int(r["paused"] or 0),
        "budget_limited": int(r["budget_limited"] or 0),
        "total_cost_usd": float(r["total_cost"] or 0.0),
    }
