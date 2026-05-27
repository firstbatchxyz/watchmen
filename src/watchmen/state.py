"""state.db — per-project run tracking for watchmen.

Tables:
  projects: tracked projects (project_key, source_repo, last-run timestamps)
  runs:     run history (project, kind, status, timing, cost estimate)

Most operations idempotent. Schema is migrate-on-open: adding columns is fine, drop/rename requires manual migration.
"""

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from watchmen.corpus_filters import substantive_filter
from watchmen.paths import ANALYSES_DIR, CORPUS_DB, BUNDLES_DIR, STATE_DB, repo_dir_sql_predicate

ROOT = Path(__file__).parent


SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    project_key TEXT PRIMARY KEY,
    source_repo TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    threshold_new_prompts INTEGER NOT NULL DEFAULT 30,
    last_analyst_run TEXT,
    last_analyst_day TEXT,
    last_curator_run TEXT,
    last_curator_skill_count INTEGER,
    notes TEXT,
    approval_required INTEGER NOT NULL DEFAULT 0,
    skip_overlapping_skills INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_key TEXT NOT NULL,
    kind TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    cost_estimate_usd REAL,
    notes TEXT,
    FOREIGN KEY (project_key) REFERENCES projects(project_key)
);
CREATE INDEX IF NOT EXISTS idx_runs_project ON runs(project_key);
CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started_at);
"""


@contextmanager
def conn():
    STATE_DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(STATE_DB)
    c.row_factory = sqlite3.Row
    try:
        yield c
    finally:
        c.close()


def init_db() -> None:
    with conn() as c:
        c.executescript(SCHEMA)
        _migrate_project_columns(c)
        c.commit()


def _migrate_project_columns(c) -> None:
    """Idempotent column-level migrations for the `projects` table. Same
    pattern as corpus.py's _migrate_sessions_columns — pre-existing
    state.db files built before a new setting was added still work after
    a pull, no `watchmen ingest --full` needed.

    History:
      - `approval_required`: gates new bundles to bundles/<repo>/_pending/
        until reviewed via `watchmen review`. Default 0 (autonomy preserved).
      - `skip_overlapping_skills`: makes `watchmen curate` drop candidates
        that overlap with installed harness skills entirely, rather than
        proposing them as enhancements (the default). Default 0."""
    cols = {r[1] for r in c.execute("PRAGMA table_info(projects)").fetchall()}
    if "approval_required" not in cols:
        c.execute("ALTER TABLE projects ADD COLUMN approval_required INTEGER NOT NULL DEFAULT 0")
    if "skip_overlapping_skills" not in cols:
        c.execute("ALTER TABLE projects ADD COLUMN skip_overlapping_skills INTEGER NOT NULL DEFAULT 0")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ─── Projects ───────────────────────────────────────────────────────────────


def track_project(project_key: str, source_repo: str, threshold: int = 30) -> None:
    with conn() as c:
        c.execute(
            """INSERT INTO projects (project_key, source_repo, threshold_new_prompts)
               VALUES (?, ?, ?)
               ON CONFLICT(project_key) DO UPDATE SET
                   source_repo = excluded.source_repo,
                   threshold_new_prompts = excluded.threshold_new_prompts,
                   updated_at = datetime('now')""",
            (project_key, source_repo, threshold),
        )
        c.commit()


def list_projects() -> list[dict]:
    with conn() as c:
        rows = c.execute("SELECT * FROM projects ORDER BY project_key").fetchall()
        return [dict(r) for r in rows]


def get_project(project_key: str) -> dict | None:
    with conn() as c:
        row = c.execute("SELECT * FROM projects WHERE project_key = ?", (project_key,)).fetchone()
        return dict(row) if row else None


def update_project(project_key: str, **fields) -> None:
    if not fields:
        return
    sets = ", ".join(f"{k} = ?" for k in fields)
    with conn() as c:
        c.execute(
            f"UPDATE projects SET {sets}, updated_at = datetime('now') WHERE project_key = ?",
            (*fields.values(), project_key),
        )
        c.commit()


def set_enabled(project_key: str, enabled: bool) -> None:
    update_project(project_key, enabled=int(enabled))


# ─── Runs ───────────────────────────────────────────────────────────────────


def start_run(project_key: str, kind: str, notes: str | None = None) -> int:
    with conn() as c:
        cur = c.execute(
            "INSERT INTO runs (project_key, kind, started_at, notes) VALUES (?, ?, ?, ?)",
            (project_key, kind, now_iso(), notes),
        )
        c.commit()
        return cur.lastrowid


def finish_run(
    run_id: int,
    status: str = "ok",
    notes: str | None = None,
    cost_estimate_usd: float | None = None,
) -> None:
    with conn() as c:
        c.execute(
            """UPDATE runs SET ended_at = ?, status = ?,
                                  notes = COALESCE(?, notes),
                                  cost_estimate_usd = COALESCE(?, cost_estimate_usd)
               WHERE id = ?""",
            (now_iso(), status, notes, cost_estimate_usd, run_id),
        )
        c.commit()


def recent_runs(limit: int = 20, project_key: str | None = None) -> list[dict]:
    with conn() as c:
        if project_key:
            rows = c.execute(
                "SELECT * FROM runs WHERE project_key = ? ORDER BY id DESC LIMIT ?",
                (project_key, limit),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]


# ─── Derived state from corpus.db ───────────────────────────────────────────


def get_project_progress(project_key: str) -> dict:
    """Cross-reference state.db with corpus.db to derive: last day in corpus, last day in
    thesis, count of new prompts since last_analyst_day. Caller decides whether to trigger a run."""
    proj = get_project(project_key)
    if not CORPUS_DB.exists():
        return {"error": "corpus.db not found"}
    if not proj:
        return {"error": f"project not tracked: {project_key}"}

    cc = sqlite3.connect(CORPUS_DB)
    cc.row_factory = sqlite3.Row

    project_where, project_params = _project_dir_predicate(proj["source_repo"])
    sub = substantive_filter("s")
    last_corpus_day = cc.execute(
        f"""SELECT MAX(substr(p.timestamp, 1, 10)) AS d
           FROM prompts p JOIN sessions s ON p.session_id = s.session_id
           WHERE {project_where} AND s.is_subagent = 0 AND {sub}""",
        project_params,
    ).fetchone()["d"]

    last_analyst_day = (proj or {}).get("last_analyst_day")
    new_prompts = 0
    if last_analyst_day:
        new_prompts = cc.execute(
            f"""SELECT COUNT(*) AS n
               FROM prompts p JOIN sessions s ON p.session_id = s.session_id
               WHERE {project_where} AND s.is_subagent = 0 AND {sub}
                 AND substr(p.timestamp, 1, 10) > ?""",
            (*project_params, last_analyst_day),
        ).fetchone()["n"]
    else:
        new_prompts = cc.execute(
            f"""SELECT COUNT(*) AS n
               FROM prompts p JOIN sessions s ON p.session_id = s.session_id
               WHERE {project_where} AND s.is_subagent = 0 AND {sub}""",
            project_params,
        ).fetchone()["n"]
    cc.close()
    return {
        "project_key": project_key,
        "last_corpus_day": last_corpus_day,
        "last_analyst_day": last_analyst_day,
        "new_prompts_since_last_analysis": new_prompts,
        "needs_analysis": new_prompts >= (proj.get("threshold_new_prompts", 30)),
    }


def _project_dir_predicate(source_repo: str, alias: str = "s") -> tuple[str, tuple[str | int, ...]]:
    """SQL predicate for sessions inside a tracked repo root (incl. subfolders).

    Delegates to the shared, separator-agnostic helper so subfolder rollup works
    the same on POSIX and Windows.
    """
    return repo_dir_sql_predicate(source_repo, alias)


def sync_from_disk(project_key: str) -> dict:
    """Look at analyses/<project>/*.md and bundles/<project>/skills to derive last-run state.
    Updates state.db with what's on disk. Returns a summary of what was synced."""
    summary = {"analyst": False, "curator": False}
    analyses_dir = ANALYSES_DIR / project_key
    if analyses_dir.exists():
        day_files = sorted(p.stem for p in analyses_dir.glob("20*.md"))
        if day_files:
            latest = day_files[-1]
            mtime_iso = datetime.fromtimestamp(
                (analyses_dir / f"{latest}.md").stat().st_mtime, tz=timezone.utc
            ).isoformat(timespec="seconds")
            update_project(project_key, last_analyst_day=latest, last_analyst_run=mtime_iso)
            summary["analyst"] = {"last_day": latest, "files": len(day_files)}

    skills_dir = BUNDLES_DIR / project_key / "skills"
    if skills_dir.exists():
        skill_count = sum(1 for d in skills_dir.iterdir() if d.is_dir())
        if skill_count:
            claude_md = BUNDLES_DIR / project_key / "CLAUDE.md"
            mtime_iso = (
                datetime.fromtimestamp(claude_md.stat().st_mtime, tz=timezone.utc).isoformat(timespec="seconds")
                if claude_md.exists()
                else now_iso()
            )
            update_project(project_key, last_curator_run=mtime_iso, last_curator_skill_count=skill_count)
            summary["curator"] = {"skill_count": skill_count}
    return summary


def auto_detect_projects() -> list[dict]:
    """Scan corpus.db for projects that have main-session activity. Returns list with
    project_key (last path segment), source_repo (real cwd from the adapter), prompt count.

    Post-normalization, all adapters store real cwds in corpus.db.sessions.project_dir,
    so we just take that string as the source_repo. The project_key is the dir name
    of the path (used as a friendly id; collisions are rare and resolved by the user
    during onboarding)."""
    if not CORPUS_DB.exists():
        return []
    cc = sqlite3.connect(CORPUS_DB)
    cc.row_factory = sqlite3.Row
    rows = cc.execute(
        """SELECT s.project_dir, COUNT(*) AS prompts, COUNT(DISTINCT s.session_id) AS sessions
           FROM prompts p JOIN sessions s ON p.session_id = s.session_id
           WHERE s.is_subagent = 0 AND s.project_dir IS NOT NULL
           GROUP BY s.project_dir
           HAVING prompts >= 30
           ORDER BY prompts DESC"""
    ).fetchall()
    cc.close()

    detected = []
    for r in rows:
        source_repo = r["project_dir"]
        project_key = Path(source_repo).name or source_repo
        detected.append({
            "project_key": project_key,
            "source_repo": source_repo,
            "prompts": r["prompts"],
            "sessions": r["sessions"],
        })
    return detected
