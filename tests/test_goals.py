"""Codex goal ingestion + aggregations.

Codex 0.133.0 stores goals in `~/.codex/state_*.sqlite::thread_goals`. These
tests build a synthetic state DB that mirrors codex's schema verbatim, run
`goals.sync_from_codex` against it, and assert the rows land in
`corpus.db::goals` with the right shape — including the LEFT JOIN to
`threads` for project_dir attribution and the LEFT JOIN to `sessions` for
cost approximation.

Pinning the schema here means a future codex release that drops or renames
columns trips these tests instead of corrupting a live corpus.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from watchmen import goals as _goals
from watchmen.corpus import init_db


def _build_codex_state(path: Path, *, threads: list[dict]) -> None:
    """Build the codex state DB with just the `threads` table the sync
    needs for project_dir attribution. Post-migration-34 codex no longer
    stores `thread_goals` here; that table moved to a dedicated goals DB
    (see `_build_codex_goals_db`)."""
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE threads (
            id TEXT PRIMARY KEY,
            cwd TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT ''
        );
        """
    )
    conn.executemany(
        "INSERT INTO threads (id, cwd) VALUES (:id, :cwd)",
        threads,
    )
    conn.commit()
    conn.close()


def _build_codex_goals_db(path: Path, *, goals: list[dict]) -> None:
    """Mirror codex 0.133.0 post-migration-34 (`goals_1.sqlite`) — the
    dedicated goal DB with the full 6-status CHECK enum."""
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE thread_goals (
            thread_id TEXT PRIMARY KEY NOT NULL,
            goal_id TEXT NOT NULL,
            objective TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN (
                'active', 'paused', 'blocked', 'usage_limited',
                'budget_limited', 'complete'
            )),
            token_budget INTEGER,
            tokens_used INTEGER NOT NULL DEFAULT 0,
            time_used_seconds INTEGER NOT NULL DEFAULT 0,
            created_at_ms INTEGER NOT NULL,
            updated_at_ms INTEGER NOT NULL
        );
        """
    )
    conn.executemany(
        """INSERT INTO thread_goals
           (thread_id, goal_id, objective, status, token_budget,
            tokens_used, time_used_seconds, created_at_ms, updated_at_ms)
           VALUES (:thread_id, :goal_id, :objective, :status, :token_budget,
                   :tokens_used, :time_used_seconds, :created_at_ms, :updated_at_ms)""",
        goals,
    )
    conn.commit()
    conn.close()


def _build_legacy_codex_state(path: Path, *, threads: list[dict], goals: list[dict]) -> None:
    """Pre-migration-34 codex (early 0.133.0): thread_goals colocated with
    threads inside the state DB. CHECK still allows the 4 original statuses.
    Used to exercise the fallback path."""
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE threads (
            id TEXT PRIMARY KEY,
            cwd TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE thread_goals (
            thread_id TEXT PRIMARY KEY NOT NULL,
            goal_id TEXT NOT NULL,
            objective TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('active','paused','budget_limited','complete')),
            token_budget INTEGER,
            tokens_used INTEGER NOT NULL DEFAULT 0,
            time_used_seconds INTEGER NOT NULL DEFAULT 0,
            created_at_ms INTEGER NOT NULL,
            updated_at_ms INTEGER NOT NULL
        );
        """
    )
    conn.executemany("INSERT INTO threads (id, cwd) VALUES (:id, :cwd)", threads)
    conn.executemany(
        """INSERT INTO thread_goals
           (thread_id, goal_id, objective, status, token_budget,
            tokens_used, time_used_seconds, created_at_ms, updated_at_ms)
           VALUES (:thread_id, :goal_id, :objective, :status, :token_budget,
                   :tokens_used, :time_used_seconds, :created_at_ms, :updated_at_ms)""",
        goals,
    )
    conn.commit()
    conn.close()


def _seed_sessions(corpus_path: Path, rows: list[dict]) -> None:
    """Seed the sessions table that goal aggregations LEFT JOIN against."""
    conn = sqlite3.connect(corpus_path)
    conn.executemany(
        """INSERT INTO sessions
           (session_id, project_dir, cost_usd, agent, input_tokens,
            cache_creation_tokens, cache_read_tokens, output_tokens)
           VALUES (:session_id, :project_dir, :cost_usd, :agent,
                   0, 0, 0, 0)""",
        rows,
    )
    conn.commit()
    conn.close()


@pytest.fixture
def fresh_corpus(tmp_path, monkeypatch):
    """Build an empty corpus.db at a tmp path and redirect both `corpus.DB_PATH`
    and the `_conn_ro` reader in `goals` to use it."""
    corpus_path = tmp_path / "corpus.db"
    from watchmen import corpus as _corpus
    monkeypatch.setattr(_corpus, "DB_PATH", corpus_path)
    monkeypatch.setattr(_goals, "CORPUS_DB", corpus_path)
    init_db(full=True)
    return corpus_path


def test_sync_from_codex_writes_rows_with_project_dir_from_threads(fresh_corpus, tmp_path):
    """Happy path (post-migration-34 layout): threads in state DB, goals in
    dedicated goals DB. ATTACH bridges them so cwd lands as project_dir."""
    state_db = tmp_path / "state_5.sqlite"
    goals_db = tmp_path / "goals_1.sqlite"
    _build_codex_state(state_db, threads=[{"id": "thr-aaa", "cwd": "/Users/dev/proj-a"}])
    _build_codex_goals_db(goals_db, goals=[{
        "thread_id": "thr-aaa",
        "goal_id": "goal-001",
        "objective": "Refactor the auth middleware",
        "status": "active",
        "token_budget": 200_000,
        "tokens_used": 45_000,
        "time_used_seconds": 1830,
        "created_at_ms": 1779_400_000_000,
        "updated_at_ms": 1779_400_900_000,
    }])

    conn = sqlite3.connect(fresh_corpus)
    written = _goals.sync_from_codex(conn, goals_db=goals_db, state_db=state_db)
    conn.close()
    assert written == 1

    conn = sqlite3.connect(fresh_corpus)
    row = conn.execute("SELECT * FROM goals WHERE goal_id = 'goal-001'").fetchone()
    cols = [c[1] for c in conn.execute("PRAGMA table_info(goals)").fetchall()]
    conn.close()
    d = dict(zip(cols, row))
    assert d["thread_id"] == "thr-aaa"
    assert d["project_dir"] == "/Users/dev/proj-a"
    assert d["objective"] == "Refactor the auth middleware"
    assert d["status"] == "active"
    assert d["token_budget"] == 200_000
    assert d["tokens_used"] == 45_000
    assert d["time_used_seconds"] == 1830
    assert d["agent"] == "codex"
    # ms → ISO; we don't lock the exact string but it must be a non-empty ISO.
    assert d["created_at"] and "T" in d["created_at"]


def test_sync_from_codex_upserts_on_repeat(fresh_corpus, tmp_path):
    """Codex updates tokens_used + status live as the thread progresses. A
    second sync must refresh, not duplicate."""
    state_db = tmp_path / "state_5.sqlite"
    goals_db = tmp_path / "goals_1.sqlite"
    _build_codex_state(state_db, threads=[{"id": "thr-bbb", "cwd": "/proj"}])
    _build_codex_goals_db(goals_db, goals=[{
        "thread_id": "thr-bbb",
        "goal_id": "goal-002",
        "objective": "Land feature X",
        "status": "active",
        "token_budget": None,
        "tokens_used": 1000,
        "time_used_seconds": 60,
        "created_at_ms": 1779_400_000_000,
        "updated_at_ms": 1779_400_100_000,
    }])
    conn = sqlite3.connect(fresh_corpus)
    _goals.sync_from_codex(conn, goals_db=goals_db, state_db=state_db)
    conn.close()

    # Codex bumps the row: more tokens, status → complete. Note: numeric
    # literals in raw SQL can't use Python's underscore separator — SQLite
    # (Ubuntu CI's newer build especially) tokenizes the underscore as a
    # separate token and errors. Underscores are fine in Python-side `int`
    # literals, but inside the SQL string they must go.
    src = sqlite3.connect(goals_db)
    src.execute(
        "UPDATE thread_goals SET tokens_used=5000, status='complete', updated_at_ms=1779400500000 WHERE goal_id='goal-002'"
    )
    src.commit()
    src.close()

    conn = sqlite3.connect(fresh_corpus)
    written = _goals.sync_from_codex(conn, goals_db=goals_db, state_db=state_db)
    n = conn.execute("SELECT COUNT(*) FROM goals").fetchone()[0]
    row = conn.execute("SELECT tokens_used, status FROM goals WHERE goal_id='goal-002'").fetchone()
    conn.close()
    assert written == 1  # one upsert, not a duplicate
    assert n == 1
    assert row == (5000, "complete")


def test_sync_handles_orphan_goal_without_thread(fresh_corpus, tmp_path):
    """If a thread row is missing (shouldn't happen but be defensive), the
    LEFT JOIN keeps the goal and just leaves project_dir NULL."""
    state_db = tmp_path / "state_5.sqlite"
    goals_db = tmp_path / "goals_1.sqlite"
    _build_codex_state(state_db, threads=[])  # no threads at all
    _build_codex_goals_db(goals_db, goals=[{
        "thread_id": "thr-orphan",
        "goal_id": "goal-orphan",
        "objective": "Orphan goal",
        "status": "paused",
        "token_budget": None,
        "tokens_used": 0,
        "time_used_seconds": 0,
        "created_at_ms": 1779_400_000_000,
        "updated_at_ms": 1779_400_000_000,
    }])
    conn = sqlite3.connect(fresh_corpus)
    _goals.sync_from_codex(conn, goals_db=goals_db, state_db=state_db)
    row = conn.execute("SELECT project_dir, status FROM goals WHERE goal_id='goal-orphan'").fetchone()
    conn.close()
    assert row == (None, "paused")


def test_sync_no_codex_install_is_noop(fresh_corpus, tmp_path):
    """When the codex state DB doesn't exist, sync returns 0 and writes nothing."""
    conn = sqlite3.connect(fresh_corpus)
    written = _goals.sync_from_codex(
        conn,
        goals_db=tmp_path / "goals_missing.sqlite",
        state_db=tmp_path / "state_missing.sqlite",
    )
    n = conn.execute("SELECT COUNT(*) FROM goals").fetchone()[0]
    conn.close()
    assert written == 0
    assert n == 0


def test_sync_skips_unknown_status_rather_than_crashes(fresh_corpus, tmp_path):
    """A future codex release that adds a new status enum value beyond the
    current 6 must not break ingestion — the unknown row is dropped and
    the rest still land."""
    state_db = tmp_path / "state_5.sqlite"
    goals_db = tmp_path / "goals_1.sqlite"

    _build_codex_state(state_db, threads=[
        {"id": "thr-x", "cwd": "/proj"},
        {"id": "thr-y", "cwd": "/proj"},
    ])
    # Build goals DB without the CHECK constraint so we can inject a hypothetical
    # future status value that codex hasn't shipped yet.
    conn = sqlite3.connect(goals_db)
    conn.executescript(
        """
        CREATE TABLE thread_goals (
            thread_id TEXT PRIMARY KEY NOT NULL,
            goal_id TEXT NOT NULL,
            objective TEXT NOT NULL,
            status TEXT NOT NULL,
            token_budget INTEGER,
            tokens_used INTEGER NOT NULL DEFAULT 0,
            time_used_seconds INTEGER NOT NULL DEFAULT 0,
            created_at_ms INTEGER NOT NULL,
            updated_at_ms INTEGER NOT NULL
        );
        """
    )
    conn.executemany(
        "INSERT INTO thread_goals VALUES (?,?,?,?,?,?,?,?,?)",
        [
            ("thr-x", "g-good", "ok", "complete", None, 100, 10, 1, 1),
            ("thr-y", "g-bad", "future enum", "stalled_future", None, 200, 20, 1, 1),
        ],
    )
    conn.commit()
    conn.close()

    cconn = sqlite3.connect(fresh_corpus)
    written = _goals.sync_from_codex(cconn, goals_db=goals_db, state_db=state_db)
    n = cconn.execute("SELECT COUNT(*) FROM goals").fetchone()[0]
    cconn.close()
    assert written == 1
    assert n == 1


def test_sync_falls_back_to_legacy_state_db_thread_goals(fresh_corpus, tmp_path):
    """Pre-migration-34 codex (early 0.133.0) kept `thread_goals` colocated
    with `threads` inside the state DB. When no dedicated `goals_*.sqlite`
    exists yet, sync must still read goals from the state DB itself."""
    legacy_state = tmp_path / "state_5.sqlite"
    _build_legacy_codex_state(
        legacy_state,
        threads=[{"id": "thr-legacy", "cwd": "/legacy-proj"}],
        goals=[{
            "thread_id": "thr-legacy",
            "goal_id": "goal-legacy",
            "objective": "Legacy goal",
            "status": "complete",
            "token_budget": None,
            "tokens_used": 9000,
            "time_used_seconds": 90,
            "created_at_ms": 1, "updated_at_ms": 2,
        }],
    )

    conn = sqlite3.connect(fresh_corpus)
    # No goals_db arg — passing only state_db should hit the legacy path.
    written = _goals.sync_from_codex(conn, goals_db=None, state_db=legacy_state)
    row = conn.execute(
        "SELECT project_dir, status, tokens_used FROM goals WHERE goal_id='goal-legacy'"
    ).fetchone()
    conn.close()
    assert written == 1
    assert row == ("/legacy-proj", "complete", 9000)


def test_sync_accepts_blocked_and_usage_limited_statuses(fresh_corpus, tmp_path):
    """Codex migration 33 (2026-05-22) added `blocked` and `usage_limited`
    to the status enum. corpus.db's CHECK must accept both."""
    state_db = tmp_path / "state_5.sqlite"
    goals_db = tmp_path / "goals_1.sqlite"
    _build_codex_state(state_db, threads=[
        {"id": "thr-b", "cwd": "/p"},
        {"id": "thr-u", "cwd": "/p"},
    ])
    _build_codex_goals_db(goals_db, goals=[
        {"thread_id": "thr-b", "goal_id": "g-blocked", "objective": "x",
         "status": "blocked", "token_budget": None, "tokens_used": 0,
         "time_used_seconds": 0, "created_at_ms": 1, "updated_at_ms": 2},
        {"thread_id": "thr-u", "goal_id": "g-usage", "objective": "y",
         "status": "usage_limited", "token_budget": 100, "tokens_used": 100,
         "time_used_seconds": 0, "created_at_ms": 1, "updated_at_ms": 2},
    ])
    conn = sqlite3.connect(fresh_corpus)
    written = _goals.sync_from_codex(conn, goals_db=goals_db, state_db=state_db)
    statuses = {r[0] for r in conn.execute("SELECT status FROM goals").fetchall()}
    conn.close()
    assert written == 2
    assert statuses == {"blocked", "usage_limited"}


def test_aggregate_per_project_groups_and_sorts(fresh_corpus, tmp_path):
    """aggregate_per_project joins goals to sessions by thread_id for cost
    and orders by total_cost DESC."""
    state_db = tmp_path / "state_5.sqlite"
    goals_db = tmp_path / "goals_1.sqlite"
    _build_codex_state(state_db, threads=[
        {"id": "thr-a1", "cwd": "/proj-cheap"},
        {"id": "thr-a2", "cwd": "/proj-cheap"},
        {"id": "thr-b1", "cwd": "/proj-pricey"},
    ])
    _build_codex_goals_db(goals_db, goals=[
        {"thread_id": "thr-a1", "goal_id": "g1", "objective": "x", "status": "complete",
         "token_budget": None, "tokens_used": 1000, "time_used_seconds": 100,
         "created_at_ms": 1, "updated_at_ms": 2},
        {"thread_id": "thr-a2", "goal_id": "g2", "objective": "y", "status": "active",
         "token_budget": None, "tokens_used": 2000, "time_used_seconds": 200,
         "created_at_ms": 1, "updated_at_ms": 2},
        {"thread_id": "thr-b1", "goal_id": "g3", "objective": "z", "status": "complete",
         "token_budget": None, "tokens_used": 5000, "time_used_seconds": 500,
         "created_at_ms": 1, "updated_at_ms": 2},
    ])
    _seed_sessions(fresh_corpus, [
        {"session_id": "thr-a1", "project_dir": "/proj-cheap", "cost_usd": 0.50, "agent": "codex"},
        {"session_id": "thr-a2", "project_dir": "/proj-cheap", "cost_usd": 0.75, "agent": "codex"},
        {"session_id": "thr-b1", "project_dir": "/proj-pricey", "cost_usd": 10.00, "agent": "codex"},
    ])
    conn = sqlite3.connect(fresh_corpus)
    _goals.sync_from_codex(conn, goals_db=goals_db, state_db=state_db)
    conn.close()

    summaries = _goals.aggregate_per_project()
    assert [s.project_key for s in summaries] == ["/proj-pricey", "/proj-cheap"]
    pricey, cheap = summaries
    assert pricey.goal_count == 1
    assert pricey.completed == 1
    assert pricey.total_cost_usd == pytest.approx(10.0)
    assert cheap.goal_count == 2
    assert cheap.completed == 1
    assert cheap.active == 1
    assert cheap.total_cost_usd == pytest.approx(1.25)
    assert cheap.total_tokens_used == 3000


def test_migrate_schema_rebuilds_goals_table_with_new_status_check(tmp_path, monkeypatch):
    """In-flight installs that pulled this PR before codex shipped the new
    status enum have a goals table whose CHECK only accepts the original 4
    statuses. After they pull again, migrate_schema must rebuild the table
    with the 6-status CHECK and preserve any rows already in place."""
    import sqlite3
    from watchmen import corpus as _corpus

    db_path = tmp_path / "corpus.db"
    c = sqlite3.connect(str(db_path))
    c.executescript("""
        CREATE TABLE sessions (
            session_id TEXT PRIMARY KEY,
            project_dir TEXT,
            transcript_path TEXT,
            file_mtime REAL,
            started_at TEXT,
            ended_at TEXT,
            duration_seconds REAL,
            is_subagent INTEGER NOT NULL DEFAULT 0,
            parent_session_id TEXT,
            message_count INTEGER NOT NULL DEFAULT 0,
            user_prompt_count INTEGER NOT NULL DEFAULT 0,
            assistant_text_count INTEGER NOT NULL DEFAULT 0,
            assistant_thinking_count INTEGER NOT NULL DEFAULT 0,
            tool_use_count INTEGER NOT NULL DEFAULT 0,
            tool_error_count INTEGER NOT NULL DEFAULT 0,
            models TEXT,
            input_tokens INTEGER NOT NULL DEFAULT 0,
            cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
            cache_read_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            model_dominant TEXT,
            cost_usd REAL NOT NULL DEFAULT 0,
            agent TEXT NOT NULL DEFAULT 'claude_code'
        );
        CREATE TABLE prompts (id INTEGER PRIMARY KEY, session_id TEXT);
        CREATE TABLE tool_calls (id INTEGER PRIMARY KEY, session_id TEXT);
        -- Old 4-status goals table — what a teammate testing the in-flight PR
        -- would have on disk before pulling the new commits.
        CREATE TABLE goals (
            goal_id TEXT PRIMARY KEY,
            thread_id TEXT NOT NULL,
            project_dir TEXT,
            objective TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('active','paused','budget_limited','complete')),
            token_budget INTEGER,
            tokens_used INTEGER NOT NULL DEFAULT 0,
            time_used_seconds INTEGER NOT NULL DEFAULT 0,
            created_at TEXT,
            updated_at TEXT,
            agent TEXT NOT NULL DEFAULT 'codex'
        );
    """)
    # Seed a row so we can assert it survives the rebuild.
    c.execute(
        "INSERT INTO goals (goal_id, thread_id, objective, status, agent) "
        "VALUES ('keep-me', 'thr-keep', 'something', 'complete', 'codex')"
    )
    c.commit()
    c.close()

    monkeypatch.setattr(_corpus, "DB_PATH", db_path)
    _corpus.migrate_schema()

    c = sqlite3.connect(str(db_path))
    try:
        # New CHECK accepts the new statuses.
        c.execute(
            "INSERT INTO goals (goal_id, thread_id, objective, status, agent) "
            "VALUES ('new-blocked', 'thr-n', 'x', 'blocked', 'codex')"
        )
        c.execute(
            "INSERT INTO goals (goal_id, thread_id, objective, status, agent) "
            "VALUES ('new-usage', 'thr-n2', 'x', 'usage_limited', 'codex')"
        )
        c.commit()
        statuses = {r[0] for r in c.execute("SELECT status FROM goals").fetchall()}
    finally:
        c.close()
    assert statuses == {"complete", "blocked", "usage_limited"}, (
        "rebuild must preserve existing rows AND accept the new status values"
    )


def test_migrate_schema_creates_goals_table_on_legacy_db(tmp_path, monkeypatch):
    """corpus.migrate_schema() must land the `goals` table on a DB that
    predates this feature, otherwise the first `watchmen goals` command
    after a pull will OperationalError on a real user's machine.

    The migration must also be robust to pre-adapter legacy DBs (sessions
    without the `agent` column) — `_CREATE_TABLES` contains
    `CREATE INDEX idx_sessions_agent ON sessions(agent)` so the
    column-ALTER migrations must run before the executescript or the
    whole batch fails partway through and the goals table is never
    created."""
    import sqlite3
    from watchmen import corpus as _corpus

    # Genuine pre-goals DB: matches the current canonical schema except
    # without the `goals` table. Also without the `agent` column to
    # exercise the doubly-legacy path.
    pre_goals_schema = """
        CREATE TABLE sessions (
            session_id TEXT PRIMARY KEY,
            project_dir TEXT,
            transcript_path TEXT,
            file_mtime REAL,
            started_at TEXT,
            ended_at TEXT,
            duration_seconds REAL,
            is_subagent INTEGER NOT NULL DEFAULT 0,
            parent_session_id TEXT,
            message_count INTEGER NOT NULL DEFAULT 0,
            user_prompt_count INTEGER NOT NULL DEFAULT 0,
            assistant_text_count INTEGER NOT NULL DEFAULT 0,
            assistant_thinking_count INTEGER NOT NULL DEFAULT 0,
            tool_use_count INTEGER NOT NULL DEFAULT 0,
            tool_error_count INTEGER NOT NULL DEFAULT 0,
            models TEXT,
            input_tokens INTEGER NOT NULL DEFAULT 0,
            cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
            cache_read_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            model_dominant TEXT,
            cost_usd REAL NOT NULL DEFAULT 0
        );
        CREATE TABLE prompts (id INTEGER PRIMARY KEY, session_id TEXT);
        CREATE TABLE tool_calls (id INTEGER PRIMARY KEY, session_id TEXT);
    """
    db_path = tmp_path / "corpus.db"
    c = sqlite3.connect(str(db_path))
    c.executescript(pre_goals_schema)
    c.commit()
    c.close()

    monkeypatch.setattr(_corpus, "DB_PATH", db_path)
    _corpus.migrate_schema()

    c = sqlite3.connect(str(db_path))
    try:
        tables = {r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        sessions_cols = {r[1] for r in c.execute("PRAGMA table_info(sessions)").fetchall()}
        goals_cols = {r[1] for r in c.execute("PRAGMA table_info(goals)").fetchall()}
    finally:
        c.close()

    assert "goals" in tables, "migrate_schema must create the goals table"
    assert "agent" in sessions_cols, (
        "column migrations must run before _CREATE_TABLES — "
        "otherwise CREATE INDEX idx_sessions_agent dies and the goals "
        "CREATE never executes"
    )
    # Spot-check goals schema lines up with codex's thread_goals shape.
    assert {"goal_id", "thread_id", "project_dir", "objective", "status",
            "token_budget", "tokens_used", "time_used_seconds",
            "created_at", "updated_at", "agent"} <= goals_cols


def test_list_for_project_returns_cost_via_sessions_join(fresh_corpus, tmp_path):
    """list_for_project pulls per-goal cost from sessions.cost_usd by
    thread_id == session_id."""
    state_db = tmp_path / "state_5.sqlite"
    goals_db = tmp_path / "goals_1.sqlite"
    _build_codex_state(state_db, threads=[{"id": "thr-q", "cwd": "/proj-q"}])
    _build_codex_goals_db(goals_db, goals=[{
        "thread_id": "thr-q", "goal_id": "gq", "objective": "Q",
        "status": "complete", "token_budget": None, "tokens_used": 100,
        "time_used_seconds": 10, "created_at_ms": 1, "updated_at_ms": 2,
    }])
    _seed_sessions(fresh_corpus, [
        {"session_id": "thr-q", "project_dir": "/proj-q", "cost_usd": 3.14, "agent": "codex"},
    ])
    conn = sqlite3.connect(fresh_corpus)
    _goals.sync_from_codex(conn, goals_db=goals_db, state_db=state_db)
    conn.close()

    rows = _goals.list_for_project("proj-q", "/proj-q")
    assert len(rows) == 1
    assert rows[0].goal_id == "gq"
    assert rows[0].cost_usd == pytest.approx(3.14)
    assert rows[0].status == "complete"
