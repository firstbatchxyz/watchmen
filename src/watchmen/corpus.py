"""Walk every coding-agent transcript on this device and load it into a queryable SQLite corpus.

Dispatches to per-agent adapters in `adapters/` (claude_code, codex, …). Each
adapter knows its own install path and JSONL schema; this module just iterates
discover() → scan() and stores the normalized rows.

  uv run corpus.py scan        # incremental: re-parse only files whose mtime changed
  uv run corpus.py scan --full # DROP+rebuild from scratch (after schema changes)
  uv run corpus.py overview    # high-level stats per project, prompt length, tool usage

Incremental scan keys on transcript_path → file_mtime. A file whose mtime
matches the stored value gets skipped entirely (no parse, no DB write). For
the 80% of daemon cycles where nothing changed, scan drops from 12-17s to <1s.
"""

import sqlite3
import sys
from pathlib import Path

from watchmen.adapters import ADAPTERS
from watchmen.paths import CORPUS_DB

ROOT = Path(__file__).parent
DB_PATH = CORPUS_DB


_CREATE_TABLES = """
CREATE TABLE IF NOT EXISTS sessions (
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
CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(project_dir);
CREATE INDEX IF NOT EXISTS idx_sessions_subagent ON sessions(is_subagent);
CREATE INDEX IF NOT EXISTS idx_sessions_agent ON sessions(agent);
CREATE INDEX IF NOT EXISTS idx_sessions_path ON sessions(transcript_path);

CREATE TABLE IF NOT EXISTS prompts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    timestamp TEXT,
    text TEXT,
    word_count INTEGER,
    char_count INTEGER,
    is_first_in_session INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_prompts_session ON prompts(session_id);

CREATE TABLE IF NOT EXISTS tool_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    timestamp TEXT,
    tool_name TEXT,
    is_error INTEGER NOT NULL DEFAULT 0,
    -- `skill_name` is populated only when `tool_name = 'Skill'` and the
    -- Claude Code transcript carries `input.skill = '<slug>'`. Used by
    -- the prune pipeline to count how often each curated skill actually
    -- fires in real sessions.
    skill_name TEXT
);
CREATE INDEX IF NOT EXISTS idx_tool_calls_session ON tool_calls(session_id);
CREATE INDEX IF NOT EXISTS idx_tool_calls_tool ON tool_calls(tool_name);
-- idx_tool_calls_skill is created inside `_migrate_tool_calls_columns`
-- so legacy DBs missing the `skill_name` column don't blow up here.

-- `goals` mirrors codex 0.133.0's `thread_goals` table (lives in
-- ~/.codex/state_*.sqlite). Codex models a single active goal per thread,
-- so `thread_id` is the natural foreign key into `sessions.session_id`.
-- We denormalize `project_dir` from the joined session at sync time so
-- per-project queries don't need a JOIN. `agent` is forward-looking —
-- only codex populates this table today, but the column is there so a
-- future CC TodoWrite ingestion can land without a migration.
CREATE TABLE IF NOT EXISTS goals (
    goal_id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL,
    project_dir TEXT,
    objective TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active','paused','blocked','usage_limited','budget_limited','complete')),
    token_budget INTEGER,
    tokens_used INTEGER NOT NULL DEFAULT 0,
    time_used_seconds INTEGER NOT NULL DEFAULT 0,
    created_at TEXT,
    updated_at TEXT,
    agent TEXT NOT NULL DEFAULT 'codex'
);
CREATE INDEX IF NOT EXISTS idx_goals_thread ON goals(thread_id);
CREATE INDEX IF NOT EXISTS idx_goals_project ON goals(project_dir);
CREATE INDEX IF NOT EXISTS idx_goals_status ON goals(status);
CREATE INDEX IF NOT EXISTS idx_goals_agent ON goals(agent);
"""


def init_db(*, full: bool = False) -> sqlite3.Connection:
    """Open corpus.db. If full=True, DROP and recreate all tables (forced full
    rebuild). If full=False, idempotent CREATE IF NOT EXISTS + run any
    pending column migrations on the existing schema."""
    conn = sqlite3.connect(DB_PATH)
    if full:
        conn.executescript(
            "DROP TABLE IF EXISTS sessions; "
            "DROP TABLE IF EXISTS prompts; "
            "DROP TABLE IF EXISTS tool_calls; "
            "DROP TABLE IF EXISTS goals;"
        )
    conn.executescript(_CREATE_TABLES)
    _migrate_sessions_columns(conn)
    _migrate_tool_calls_columns(conn)
    conn.commit()
    return conn


def _migrate_sessions_columns(conn: sqlite3.Connection) -> None:
    """Idempotent column-level migrations for the `sessions` table. Each
    entry is a no-op when the column already exists, so the function is
    safe to call repeatedly (and on every CLI startup — see `migrate_schema`).

    History:
      - `file_mtime` added with the incremental-scan optimization
      - `agent` added with multi-adapter support (Codex + pi.dev). Legacy DBs
        built before this fail with `OperationalError: no such column: agent`
        on `watchmen insights` and other read paths — that's the regression
        this migration fixes."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)").fetchall()}
    if "file_mtime" not in cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN file_mtime REAL")
    if "agent" not in cols:
        # NOT NULL DEFAULT 'claude_code' matches the canonical CREATE TABLE
        # so existing rows get tagged as Claude Code sessions (true for any
        # corpus built before adapter support landed).
        conn.execute(
            "ALTER TABLE sessions ADD COLUMN agent TEXT NOT NULL DEFAULT 'claude_code'"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_agent ON sessions(agent)")


def _migrate_tool_calls_columns(conn: sqlite3.Connection) -> None:
    """Idempotent column-level migrations for `tool_calls`.

    History:
      - `skill_name` added with the prune pipeline (v0.6.1) so the
        per-skill usage signal can be extracted without a full rebuild.
        Existing rows get NULL — that's correct since pre-migration
        sessions weren't scanned for the input.skill payload.
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(tool_calls)").fetchall()}
    if "skill_name" not in cols:
        conn.execute("ALTER TABLE tool_calls ADD COLUMN skill_name TEXT")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tool_calls_skill ON tool_calls(skill_name)"
        )


_ENSURE_GOALS_TABLE = """
CREATE TABLE IF NOT EXISTS goals (
    goal_id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL,
    project_dir TEXT,
    objective TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active','paused','blocked','usage_limited','budget_limited','complete')),
    token_budget INTEGER,
    tokens_used INTEGER NOT NULL DEFAULT 0,
    time_used_seconds INTEGER NOT NULL DEFAULT 0,
    created_at TEXT,
    updated_at TEXT,
    agent TEXT NOT NULL DEFAULT 'codex'
);
CREATE INDEX IF NOT EXISTS idx_goals_thread ON goals(thread_id);
CREATE INDEX IF NOT EXISTS idx_goals_project ON goals(project_dir);
CREATE INDEX IF NOT EXISTS idx_goals_status ON goals(status);
CREATE INDEX IF NOT EXISTS idx_goals_agent ON goals(agent);
"""


def _migrate_goals_check_constraint(conn: sqlite3.Connection) -> None:
    """Codex migration 33 ("thread goal stopped statuses", 2026-05-22) added
    `blocked` and `usage_limited` to the goal status enum. Any in-flight
    install with an earlier 4-status CHECK on its `goals` table will reject
    the new statuses on INSERT.

    SQLite can't ALTER a CHECK constraint in place, so we rebuild the table
    via temp swap when the existing CHECK is missing either new value.
    No-op when the constraint is current."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='goals'"
    ).fetchone()
    if row is None or row[0] is None:
        return
    sql_text = row[0]
    if "blocked" in sql_text and "usage_limited" in sql_text:
        return

    conn.executescript("""
        CREATE TABLE goals_new (
            goal_id TEXT PRIMARY KEY,
            thread_id TEXT NOT NULL,
            project_dir TEXT,
            objective TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('active','paused','blocked','usage_limited','budget_limited','complete')),
            token_budget INTEGER,
            tokens_used INTEGER NOT NULL DEFAULT 0,
            time_used_seconds INTEGER NOT NULL DEFAULT 0,
            created_at TEXT,
            updated_at TEXT,
            agent TEXT NOT NULL DEFAULT 'codex'
        );
        INSERT INTO goals_new
            (goal_id, thread_id, project_dir, objective, status,
             token_budget, tokens_used, time_used_seconds,
             created_at, updated_at, agent)
        SELECT goal_id, thread_id, project_dir, objective, status,
               token_budget, tokens_used, time_used_seconds,
               created_at, updated_at, agent
        FROM goals;
        DROP TABLE goals;
        ALTER TABLE goals_new RENAME TO goals;
        CREATE INDEX IF NOT EXISTS idx_goals_thread ON goals(thread_id);
        CREATE INDEX IF NOT EXISTS idx_goals_project ON goals(project_dir);
        CREATE INDEX IF NOT EXISTS idx_goals_status ON goals(status);
        CREATE INDEX IF NOT EXISTS idx_goals_agent ON goals(agent);
    """)


def migrate_schema() -> None:
    """Open corpus.db just to run pending column migrations + create any
    newly-added tables, then close. Called once from `cli.main()` so every
    watchmen command auto-applies pending schema migrations without the
    user having to know about `watchmen ingest --full`. No-op when the DB
    doesn't exist yet (fresh install). Swallows sqlite errors so a
    degraded DB can't break CLI startup — the real command will surface
    the actual problem.

    Why only goals (not the full `_CREATE_TABLES`): running the full
    canonical script against a legacy DB also re-runs the
    `CREATE INDEX ... ON sessions(agent) / tool_calls(tool_name)` lines,
    which die on partial legacy schemas missing those columns. Targeted
    DDL for newly-added tables avoids re-touching settled tables and
    keeps migrations idempotent + minimal."""
    if not DB_PATH.exists():
        return
    try:
        conn = sqlite3.connect(DB_PATH)
        try:
            # Only migrate if the sessions table exists — otherwise there's
            # nothing to alter (fresh corpus.db touched but not populated).
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='sessions'"
            ).fetchone()
            if row is not None:
                _migrate_sessions_columns(conn)
                _migrate_tool_calls_columns(conn)
                conn.executescript(_ENSURE_GOALS_TABLE)
                _migrate_goals_check_constraint(conn)
                conn.commit()
        finally:
            conn.close()
    except sqlite3.Error:
        pass


def _known_mtimes(conn: sqlite3.Connection) -> dict[str, float | None]:
    """Snapshot transcript_path → file_mtime for every existing session. Used
    to skip files whose mtime hasn't changed since the last scan."""
    return {
        row[0]: row[1]
        for row in conn.execute(
            "SELECT transcript_path, file_mtime FROM sessions WHERE transcript_path IS NOT NULL"
        )
    }


def _replace_session(conn: sqlite3.Connection, session: dict, prompts: list, tools: list) -> None:
    """UPSERT a session row and its child prompts + tool_calls. Children of the
    same session_id are deleted first (no FK cascade in SQLite without PRAGMA),
    so a re-parsed file cleanly replaces its prior rows."""
    sid = session["session_id"]
    conn.execute("DELETE FROM prompts WHERE session_id = ?", (sid,))
    conn.execute("DELETE FROM tool_calls WHERE session_id = ?", (sid,))
    conn.execute(
        """INSERT OR REPLACE INTO sessions
           (session_id, project_dir, transcript_path, file_mtime, started_at, ended_at, duration_seconds,
            is_subagent, parent_session_id, message_count, user_prompt_count,
            assistant_text_count, assistant_thinking_count, tool_use_count, tool_error_count, models,
            input_tokens, cache_creation_tokens, cache_read_tokens, output_tokens, model_dominant, cost_usd,
            agent)
           VALUES (:session_id, :project_dir, :transcript_path, :file_mtime, :started_at, :ended_at, :duration_seconds,
                   :is_subagent, :parent_session_id, :message_count, :user_prompt_count,
                   :assistant_text_count, :assistant_thinking_count, :tool_use_count, :tool_error_count, :models,
                   :input_tokens, :cache_creation_tokens, :cache_read_tokens, :output_tokens, :model_dominant, :cost_usd,
                   :agent)""",
        session,
    )
    if prompts:
        conn.executemany(
            """INSERT INTO prompts (session_id, timestamp, text, word_count, char_count, is_first_in_session)
               VALUES (:session_id, :timestamp, :text, :word_count, :char_count, :is_first_in_session)""",
            prompts,
        )
    if tools:
        conn.executemany(
            """INSERT INTO tool_calls (session_id, timestamp, tool_name, is_error, skill_name)
               VALUES (:session_id, :timestamp, :tool_name, :is_error, :skill_name)""",
            tools,
        )


def scan_all(*, full: bool = False) -> None:
    """Walk every adapter's transcripts. In incremental mode (default), skip
    files whose mtime matches the stored value. In --full mode, drop and
    rebuild from scratch (use when adapter logic changes)."""
    conn = init_db(full=full)
    known = {} if full else _known_mtimes(conn)

    per_adapter: list[tuple[str, list[dict]]] = []
    for adapter in ADAPTERS:
        files = list(adapter.discover())
        per_adapter.append((adapter.NAME, files))
        print(f"  {adapter.NAME}: {len(files)} transcripts", flush=True)
    total = sum(len(f) for _, f in per_adapter)
    mode = "full rebuild" if full else "incremental"
    print(f"Found {total} transcripts across {len(per_adapter)} adapter(s). Scanning ({mode})...", flush=True)

    failed = 0
    parsed = 0
    skipped = 0
    seen = 0
    for adapter, files in zip(ADAPTERS, [f for _, f in per_adapter]):
        for entry in files:
            seen += 1
            if seen % 500 == 0:
                print(f"  [{seen}/{total}] parsed={parsed} skipped={skipped}", flush=True)
            path = entry["path"]
            try:
                cur_mtime = path.stat().st_mtime
            except OSError:
                # File vanished between discover() and stat() — skip.
                continue
            prev_mtime = known.get(str(path))
            if prev_mtime is not None and abs(prev_mtime - cur_mtime) < 1e-6:
                skipped += 1
                continue

            try:
                session, prompts, tools = adapter.scan(entry)
            except Exception as ex:
                failed += 1
                if failed < 5:
                    print(f"  ! failed {path.name}: {ex}", flush=True)
                continue

            session["file_mtime"] = cur_mtime
            _replace_session(conn, session, prompts, tools)
            parsed += 1

    conn.commit()

    # Codex 0.133.0+ goals come from a separate SQLite store (not from the
    # rollout JSONLs the per-file loop processed above). Sync them now so a
    # single `watchmen ingest` keeps the goals lens fresh. No-op when codex
    # isn't installed or the thread_goals table is empty.
    try:
        from watchmen import goals as _wm_goals
        goal_rows = _wm_goals.sync_from_codex(conn)
        if goal_rows:
            print(f"  codex goals synced: {goal_rows} rows", flush=True)
    except Exception as ex:
        # Goal sync must never break corpus ingestion — surface the error
        # but let the main `parsed/skipped/failed` numbers stand.
        print(f"  ! goal sync failed: {ex}", flush=True)

    conn.close()
    print(f"Done. parsed={parsed} skipped={skipped} failed={failed}. DB at {DB_PATH}", flush=True)


def overview() -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    print("=== SESSION COUNTS ===")
    rows = conn.execute("SELECT is_subagent, COUNT(*) AS n FROM sessions GROUP BY is_subagent").fetchall()
    for r in rows:
        kind = "subagent" if r["is_subagent"] else "main"
        print(f"  {kind:<10} {r['n']}")

    print("\n=== BY AGENT (main sessions) ===")
    rows = conn.execute(
        """SELECT agent, COUNT(*) AS sessions,
                  COALESCE(SUM(user_prompt_count), 0) AS prompts,
                  ROUND(COALESCE(SUM(cost_usd), 0), 2) AS cost
           FROM sessions WHERE is_subagent = 0
           GROUP BY agent ORDER BY sessions DESC"""
    ).fetchall()
    for r in rows:
        print(f"  {r['agent']:<14} sessions={r['sessions']:>5}  prompts={r['prompts']:>6}  cost=${r['cost']}")

    print("\n=== TOP 20 PROJECTS BY MAIN SESSION COUNT ===")
    rows = conn.execute(
        """
        SELECT project_dir,
               COUNT(*) AS sessions,
               COALESCE(SUM(user_prompt_count), 0) AS prompts,
               COALESCE(SUM(tool_use_count), 0) AS tools,
               COALESCE(SUM(tool_error_count), 0) AS errors,
               ROUND(COALESCE(SUM(duration_seconds), 0)/3600.0, 1) AS hours
        FROM sessions WHERE is_subagent = 0
        GROUP BY project_dir
        ORDER BY sessions DESC, prompts DESC
        LIMIT 20
        """
    ).fetchall()
    print(f"  {'project':<70}  {'sess':>4} {'prompts':>8} {'tools':>7} {'errors':>7} {'hours':>6}")
    for r in rows:
        print(
            f"  {r['project_dir'][:70]:<70}  {r['sessions']:>4} {r['prompts']:>8} {r['tools']:>7} {r['errors']:>7} {r['hours']:>6}"
        )

    print("\n=== MAIN-SESSION SIZE DISTRIBUTION (prompts, tools, duration) ===")
    r = conn.execute(
        """
        SELECT COUNT(*) AS n,
               ROUND(AVG(user_prompt_count), 1) AS avg_prompts,
               MAX(user_prompt_count) AS max_prompts,
               ROUND(AVG(tool_use_count), 1) AS avg_tools,
               MAX(tool_use_count) AS max_tools,
               ROUND(AVG(duration_seconds)/60.0, 1) AS avg_min,
               ROUND(MAX(duration_seconds)/60.0, 1) AS max_min
        FROM sessions WHERE is_subagent = 0
        """
    ).fetchone()
    print(f"  sessions={r['n']}  avg_prompts={r['avg_prompts']}  max_prompts={r['max_prompts']}")
    print(f"  avg_tools={r['avg_tools']}  max_tools={r['max_tools']}")
    print(f"  avg_minutes={r['avg_min']}  max_minutes={r['max_min']}")

    print("\n=== PROMPT LENGTH (main sessions) ===")
    r = conn.execute(
        """
        SELECT COUNT(*) AS n,
               ROUND(AVG(word_count), 1) AS avg_words,
               MIN(word_count) AS min_words,
               MAX(word_count) AS max_words
        FROM prompts p JOIN sessions s ON p.session_id = s.session_id
        WHERE s.is_subagent = 0
        """
    ).fetchone()
    print(f"  total_prompts={r['n']}  avg={r['avg_words']}  min={r['min_words']}  max={r['max_words']}")

    print("\n=== TOOL USAGE (main sessions) ===")
    rows = conn.execute(
        """
        SELECT t.tool_name, COUNT(*) AS n,
               ROUND(100.0 * COUNT(*) / (SELECT COUNT(*) FROM tool_calls tc JOIN sessions s2 ON tc.session_id = s2.session_id WHERE s2.is_subagent = 0), 1) AS pct
        FROM tool_calls t JOIN sessions s ON t.session_id = s.session_id
        WHERE s.is_subagent = 0
        GROUP BY t.tool_name
        ORDER BY n DESC
        """
    ).fetchall()
    for r in rows:
        print(f"  {r['tool_name']:<25} {r['n']:>6}  ({r['pct']}%)")

    print("\n=== MODELS (main sessions) ===")
    rows = conn.execute(
        "SELECT models, COUNT(*) AS n FROM sessions WHERE is_subagent = 0 GROUP BY models ORDER BY n DESC"
    ).fetchall()
    for r in rows:
        print(f"  {r['models']:<70}  {r['n']}")

    conn.close()


def main() -> None:
    args = sys.argv[1:]
    cmd = args[0] if args else "overview"
    if cmd == "scan":
        full = "--full" in args[1:]
        scan_all(full=full)
    elif cmd == "overview":
        overview()
    else:
        print("usage: corpus.py [scan [--full] | overview]")


if __name__ == "__main__":
    main()
