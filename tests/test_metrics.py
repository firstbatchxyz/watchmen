"""Tests for watchmen.metrics aggregation helpers.

Focused on `adapter_breakdown_all` — the cross-project per-agent rollup
that powers the viewer's "By coding agent" section and the CLI's per-agent
table. Builds a fixture corpus.db with sessions across three agents and
asserts the returned shape matches.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from watchmen import metrics as _metrics


@pytest.fixture
def fresh_metrics(tmp_path: Path, monkeypatch):
    """Redirect the metrics module's CORPUS_DB constant to a temp file
    using monkeypatch.setattr — and inside `compute_card_stats`, the
    BUNDLES_DIR import is re-resolved against an empty temp dir so the
    mastery axis doesn't read the developer's real bundles directory.
    monkeypatch restores both on teardown."""
    corpus_path = tmp_path / "corpus.db"
    bundles_path = tmp_path / "bundles"
    bundles_path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(_metrics, "CORPUS_DB", corpus_path)
    # `compute_card_stats` does `from watchmen.paths import BUNDLES_DIR`
    # at call time, so the patch has to target watchmen.paths rather than
    # the metrics module.
    from watchmen import paths as _paths
    monkeypatch.setattr(_paths, "BUNDLES_DIR", bundles_path)
    return _metrics


def _seed_corpus(corpus_path: Path, rows: list[dict]) -> None:
    """Write a minimal corpus.db sessions table with just the columns
    `adapter_breakdown_all` reads. Keeps the fixture tiny."""
    schema = """
    CREATE TABLE sessions (
        session_id TEXT PRIMARY KEY,
        project_dir TEXT NOT NULL,
        started_at TEXT NOT NULL,
        user_prompt_count INTEGER NOT NULL DEFAULT 0,
        tool_error_count INTEGER NOT NULL DEFAULT 0,
        input_tokens INTEGER NOT NULL DEFAULT 0,
        cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
        cache_read_tokens INTEGER NOT NULL DEFAULT 0,
        output_tokens INTEGER NOT NULL DEFAULT 0,
        cost_usd REAL NOT NULL DEFAULT 0,
        is_subagent INTEGER NOT NULL DEFAULT 0,
        agent TEXT NOT NULL DEFAULT 'claude_code'
    );
    """
    cols = (
        "session_id, project_dir, started_at, user_prompt_count, "
        "tool_error_count, input_tokens, cache_creation_tokens, "
        "cache_read_tokens, output_tokens, cost_usd, is_subagent, agent"
    )
    with sqlite3.connect(str(corpus_path)) as conn:
        conn.executescript(schema)
        for r in rows:
            conn.execute(
                f"INSERT INTO sessions ({cols}) VALUES "
                f"(:session_id, :project_dir, :started_at, "
                f":user_prompt_count, :tool_error_count, "
                f":input_tokens, :cache_creation_tokens, :cache_read_tokens, "
                f":output_tokens, :cost_usd, :is_subagent, :agent)",
                r,
            )


def _row(session_id, *, agent, project_dir, days_ago=0, prompts=0,
         tool_errors=0, cost=0.0, is_subagent=0):
    started = (datetime.now() - timedelta(days=days_ago)).isoformat()
    return {
        "session_id": session_id,
        "project_dir": project_dir,
        "started_at": started,
        "user_prompt_count": prompts,
        "tool_error_count": tool_errors,
        "input_tokens": 0,
        "cache_creation_tokens": 0,
        "cache_read_tokens": 0,
        "output_tokens": 0,
        "cost_usd": cost,
        "is_subagent": is_subagent,
        "agent": agent,
    }


def test_no_corpus_db_returns_empty(fresh_metrics):
    """No corpus.db on disk → empty list, not an exception."""
    assert fresh_metrics.adapter_breakdown_all() == []


def test_per_agent_rollup_orders_by_session_count(fresh_metrics, tmp_path):
    _seed_corpus(tmp_path / "corpus.db", [
        _row("s1", agent="claude_code", project_dir="/p1", prompts=10, tool_errors=2, cost=1.50),
        _row("s2", agent="claude_code", project_dir="/p2", prompts=4, cost=0.50),
        _row("s3", agent="codex", project_dir="/p1", prompts=3, tool_errors=1),
        _row("s4", agent="pi", project_dir="/p3", prompts=2),
        _row("s5", agent="pi", project_dir="/p3", prompts=1),
    ])
    rows = fresh_metrics.adapter_breakdown_all(days=30)

    assert [r["agent"] for r in rows] == ["claude_code", "pi", "codex"]
    cc = rows[0]
    assert cc["label"] == "Claude Code"
    assert cc["sessions"] == 2
    assert cc["projects"] == 2
    assert cc["prompts"] == 14
    assert cc["tool_errors"] == 2
    assert cc["cost_usd"] == pytest.approx(2.00)

    pi = rows[1]
    assert pi["label"] == "pi.dev"
    assert pi["sessions"] == 2
    assert pi["projects"] == 1


def test_subagent_sessions_excluded(fresh_metrics, tmp_path):
    """is_subagent=1 rows are noise from the curator's tool-calling agents,
    not user-driven sessions. They must not appear in the rollup."""
    _seed_corpus(tmp_path / "corpus.db", [
        _row("s1", agent="claude_code", project_dir="/p1"),
        _row("sub1", agent="claude_code", project_dir="/p1", is_subagent=1),
        _row("sub2", agent="claude_code", project_dir="/p1", is_subagent=1),
    ])
    rows = fresh_metrics.adapter_breakdown_all(days=30)
    assert len(rows) == 1
    assert rows[0]["sessions"] == 1


def test_window_cutoff_excludes_old_sessions(fresh_metrics, tmp_path):
    """Sessions older than `days` should not be counted. Default window is
    30 days; anything beyond drops out."""
    _seed_corpus(tmp_path / "corpus.db", [
        _row("recent", agent="claude_code", project_dir="/p1", days_ago=5),
        _row("ancient", agent="codex", project_dir="/p1", days_ago=120),
    ])
    rows = fresh_metrics.adapter_breakdown_all(days=30)
    assert [r["agent"] for r in rows] == ["claude_code"]


def test_unknown_agent_falls_through_to_raw_slug(fresh_metrics, tmp_path):
    """A future adapter slug not in ADAPTER_LABELS should still render — we
    just show the slug verbatim instead of crashing."""
    _seed_corpus(tmp_path / "corpus.db", [
        _row("s1", agent="cursor", project_dir="/p1"),
    ])
    rows = fresh_metrics.adapter_breakdown_all(days=30)
    assert rows[0]["label"] == "cursor"


# ─── compute_card_stats ──────────────────────────────────────────────────


def _seed_corpus_with_tools(corpus_path: Path, sessions: list[dict], tool_calls: list[dict]) -> None:
    """Extended fixture: sessions + tool_calls tables. The card metric
    pulls distinct tool names from tool_calls, so we need both."""
    schema = """
    CREATE TABLE sessions (
        session_id TEXT PRIMARY KEY,
        project_dir TEXT NOT NULL,
        started_at TEXT NOT NULL,
        user_prompt_count INTEGER NOT NULL DEFAULT 0,
        tool_use_count INTEGER NOT NULL DEFAULT 0,
        tool_error_count INTEGER NOT NULL DEFAULT 0,
        input_tokens INTEGER NOT NULL DEFAULT 0,
        cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
        cache_read_tokens INTEGER NOT NULL DEFAULT 0,
        output_tokens INTEGER NOT NULL DEFAULT 0,
        cost_usd REAL NOT NULL DEFAULT 0,
        is_subagent INTEGER NOT NULL DEFAULT 0,
        agent TEXT NOT NULL DEFAULT 'claude_code'
    );
    CREATE TABLE tool_calls (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT,
        timestamp TEXT,
        tool_name TEXT,
        is_error INTEGER NOT NULL DEFAULT 0
    );
    """
    with sqlite3.connect(str(corpus_path)) as conn:
        conn.executescript(schema)
        for s in sessions:
            conn.execute(
                "INSERT INTO sessions (session_id, project_dir, started_at, "
                "user_prompt_count, tool_use_count, tool_error_count, "
                "input_tokens, cache_creation_tokens, cache_read_tokens, "
                "output_tokens, cost_usd, is_subagent, agent) VALUES "
                "(:session_id, :project_dir, :started_at, "
                ":user_prompt_count, :tool_use_count, :tool_error_count, "
                ":input_tokens, :cache_creation_tokens, :cache_read_tokens, "
                ":output_tokens, :cost_usd, :is_subagent, :agent)",
                s,
            )
        for t in tool_calls:
            conn.execute(
                "INSERT INTO tool_calls (session_id, timestamp, tool_name, is_error) "
                "VALUES (:session_id, :timestamp, :tool_name, :is_error)",
                t,
            )


def _ses(session_id, *, agent="claude_code", project_dir="/p", days_ago=0,
         prompts=0, tool_uses=0, tool_errors=0, cost=0.0, is_subagent=0):
    return {
        "session_id": session_id,
        "project_dir": project_dir,
        "started_at": (datetime.now() - timedelta(days=days_ago)).isoformat(),
        "user_prompt_count": prompts,
        "tool_use_count": tool_uses,
        "tool_error_count": tool_errors,
        "input_tokens": 0, "cache_creation_tokens": 0,
        "cache_read_tokens": 0, "output_tokens": 0,
        "cost_usd": cost, "is_subagent": is_subagent, "agent": agent,
    }


def _tc(session_id, *, tool, days_ago=0, is_error=0):
    return {
        "session_id": session_id,
        "timestamp": (datetime.now() - timedelta(days=days_ago)).isoformat(),
        "tool_name": tool,
        "is_error": is_error,
    }


def test_card_stats_newcomer_when_corpus_empty(fresh_metrics):
    """No corpus.db / no sessions → Newcomer archetype, rating floor 40,
    every axis at zero. Card still renders so even a fresh install has
    something to look at."""
    s = fresh_metrics.compute_card_stats(days=90)
    assert s["rating"] == 40
    assert s["archetype"][0] == "Newcomer"
    assert all(v == 0.0 for v in s["axes"].values())
    assert s["sessions"] == 0
    assert s["top_agent"] is None and s["top_tool"] is None


def test_card_stats_rating_in_range_and_archetype_set(fresh_metrics, tmp_path):
    """A moderate corpus produces a non-floor rating and picks a real
    archetype (not Newcomer). Specific number is implementation-defined;
    just verify the contract: 40 ≤ rating ≤ 99 and archetype isn't None."""
    _seed_corpus_with_tools(
        tmp_path / "corpus.db",
        sessions=[
            _ses("s1", project_dir="/p1", days_ago=2, prompts=20, tool_uses=40, tool_errors=2, cost=0.50),
            _ses("s2", project_dir="/p2", days_ago=5, prompts=15, tool_uses=30, tool_errors=1, cost=0.30),
            _ses("s3", project_dir="/p3", days_ago=8, prompts=10, tool_uses=20, tool_errors=0, cost=0.20),
        ],
        tool_calls=[
            _tc("s1", tool="Read"), _tc("s1", tool="Edit"), _tc("s1", tool="Bash"),
            _tc("s2", tool="Read"), _tc("s2", tool="Grep"),
            _tc("s3", tool="Write"),
        ],
    )
    s = fresh_metrics.compute_card_stats(days=90)
    assert 40 <= s["rating"] <= 99
    assert s["archetype"][0] != "Newcomer"
    assert s["sessions"] == 3
    assert s["axes_raw"]["curiosity"] == 5  # Read, Edit, Bash, Grep, Write
    assert s["axes_raw"]["range"] == 3      # /p1, /p2, /p3
    assert s["top_tool"][0] == "Read" and s["top_tool"][1] == 2


def test_card_stats_dominant_axis_picks_specific_archetype(fresh_metrics, tmp_path):
    """When one axis dominates by ≥1.25× the runner-up AND ≥0.4 absolute,
    the corresponding archetype label is used. Seed a corpus with the
    range axis maxed (12 projects = cap) and zero tool activity so no
    other axis competes — should land cleanly on Polyglot."""
    sessions = [
        _ses(f"s{i}", project_dir=f"/p{i}", days_ago=i, prompts=1, tool_uses=0)
        for i in range(12)
    ]
    _seed_corpus_with_tools(tmp_path / "corpus.db", sessions=sessions, tool_calls=[])
    s = fresh_metrics.compute_card_stats(days=90)
    assert s["archetype"][0] == "Polyglot", f"got {s['archetype']!r}"
    assert s["axes_raw"]["range"] == 12


def test_card_stats_subagent_sessions_excluded(fresh_metrics, tmp_path):
    """Subagent sessions are curator-internal noise and must not inflate
    user-facing stats. Same exclusion rule as adapter_breakdown_all."""
    _seed_corpus_with_tools(
        tmp_path / "corpus.db",
        sessions=[
            _ses("real", project_dir="/p", days_ago=1, prompts=5),
            _ses("sub", project_dir="/p", days_ago=1, prompts=100, is_subagent=1),
        ],
        tool_calls=[],
    )
    s = fresh_metrics.compute_card_stats(days=90)
    assert s["sessions"] == 1
    assert s["prompts"] == 5


def test_card_svg_renders_axis_labels(fresh_metrics, tmp_path):
    """The SVG is now spider-chart-only (header + footer moved into
    HTML in metrics_all.html). Verify each axis label still appears
    in the rendered string; the polygon + tinted rings have no
    inspectable text but at least we catch the case where a label
    or spoke regresses out of the output."""
    _seed_corpus_with_tools(
        tmp_path / "corpus.db",
        sessions=[_ses("s1", project_dir="/p", days_ago=1, prompts=5, tool_uses=5)],
        tool_calls=[_tc("s1", tool="Read")],
    )
    s = fresh_metrics.compute_card_stats(days=90)
    svg = fresh_metrics.card_svg(s)
    assert svg.startswith("<svg") and svg.endswith("</svg>")
    for axis in ("THROUGHPUT", "FRUGALITY", "RELIABILITY", "CURIOSITY", "RANGE", "MASTERY"):
        assert axis in svg, f"axis label {axis} missing from card SVG"
    # The 4 tinted ring polygons (red → orange → yellow → green).
    assert svg.count("<polygon") >= 5  # 4 rings + 1 user polygon


def test_card_attribute_groups_and_traits_populate(fresh_metrics, tmp_path):
    """`compute_card_stats` returns the data the FM-style profile card
    needs: three column groups (Volume / Efficiency / Breadth) with
    color-coded stat items, and a non-empty traits list. This is the
    contract the metrics_all.html profile section depends on."""
    _seed_corpus_with_tools(
        tmp_path / "corpus.db",
        sessions=[
            _ses("s1", project_dir="/p1", days_ago=2, prompts=20, tool_uses=40, cost=1.0),
            _ses("s2", project_dir="/p2", days_ago=4, prompts=10, tool_uses=20, cost=0.5),
            _ses("s3", project_dir="/p3", days_ago=6, prompts=5, tool_uses=10),
        ],
        tool_calls=[_tc("s1", tool="Read"), _tc("s2", tool="Edit"), _tc("s3", tool="Bash")],
    )
    s = fresh_metrics.compute_card_stats(days=90)
    groups = s["attribute_groups"]
    assert [g["label"] for g in groups] == ["Volume", "Efficiency", "Breadth"]
    # Every stat item is a dict with the contract the template iterates.
    for g in groups:
        assert g["stats"], f"empty {g['label']!r} column"
        for item in g["stats"]:
            assert set(item.keys()) == {"label", "raw", "kind"}
            assert item["kind"] in {"elite", "mid", "low", "neutral"}
    # Traits is a list of strings, at least one trait present.
    assert isinstance(s["traits"], list) and s["traits"]
    assert all(isinstance(t, str) for t in s["traits"])


def test_card_tier_colors_match_rating_bands(fresh_metrics):
    """card_tier_colors picks gold/silver/bronze/indigo from rating.
    Smoke-tests the band edges (89 → silver, 90 → gold, etc.)."""
    assert fresh_metrics.card_tier_colors(95)["name"] == "gold"
    assert fresh_metrics.card_tier_colors(90)["name"] == "gold"
    assert fresh_metrics.card_tier_colors(89)["name"] == "silver"
    assert fresh_metrics.card_tier_colors(80)["name"] == "silver"
    assert fresh_metrics.card_tier_colors(79)["name"] == "bronze"
    assert fresh_metrics.card_tier_colors(70)["name"] == "bronze"
    assert fresh_metrics.card_tier_colors(40)["name"] == "indigo"


def test_agent_donut_svg_segments_and_empty(fresh_metrics):
    """agent_donut_svg renders one <path> per non-zero agent and a muted
    fallback when the input is empty. Center text shows the total +
    'sessions' label so the chart reads without a separate caption."""
    svg = fresh_metrics.agent_donut_svg({"claude_code": 6, "codex": 4, "pi": 0})
    assert svg.count("<path") == 2, "zero-count agents should be skipped"
    assert "10" in svg or "SESSIONS" in svg, "total + label missing from center"

    empty = fresh_metrics.agent_donut_svg({})
    assert "no data" in empty
    assert "<path" not in empty


def test_agent_donut_legend_orders_by_share_and_assigns_colors(fresh_metrics):
    """Legend rows come back sorted by count desc, with friendly labels
    and a stable color per known adapter. Unknown adapters cycle through
    a fallback palette so future agents render without a code change."""
    rows = fresh_metrics.agent_donut_legend({"claude_code": 5, "codex": 12, "pi": 3})
    assert [r["slug"] for r in rows] == ["codex", "claude_code", "pi"]
    assert rows[0]["label"] == "Codex"
    assert rows[0]["share"] == pytest.approx(12 / 20)
    # Known-agent colors are fixed.
    by_slug = {r["slug"]: r["color"] for r in rows}
    assert by_slug["claude_code"] == "#3b82f6"  # blue-500
    assert by_slug["codex"] == "#06b6d4"        # cyan-500
    assert by_slug["pi"] == "#14b8a6"           # teal-500


# ─── agent_comparison_facts ──────────────────────────────────────────────


def _seed_comparison_corpus(
    corpus_path: Path,
    sessions: list[dict],
    tool_calls: list[dict] | None = None,
) -> None:
    """Schema variant for cross-agent comparison tests: adds the columns
    `agent_comparison_facts` reads beyond `_seed_corpus_with_tools` —
    duration_seconds and model_dominant — plus the tool_calls table."""
    schema = """
    CREATE TABLE sessions (
        session_id TEXT PRIMARY KEY,
        project_dir TEXT NOT NULL,
        started_at TEXT NOT NULL,
        duration_seconds REAL,
        user_prompt_count INTEGER NOT NULL DEFAULT 0,
        tool_use_count INTEGER NOT NULL DEFAULT 0,
        tool_error_count INTEGER NOT NULL DEFAULT 0,
        input_tokens INTEGER NOT NULL DEFAULT 0,
        cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
        cache_read_tokens INTEGER NOT NULL DEFAULT 0,
        output_tokens INTEGER NOT NULL DEFAULT 0,
        cost_usd REAL NOT NULL DEFAULT 0,
        is_subagent INTEGER NOT NULL DEFAULT 0,
        agent TEXT NOT NULL DEFAULT 'claude_code',
        model_dominant TEXT
    );
    CREATE TABLE tool_calls (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT,
        timestamp TEXT,
        tool_name TEXT,
        is_error INTEGER NOT NULL DEFAULT 0
    );
    """
    with sqlite3.connect(str(corpus_path)) as conn:
        conn.executescript(schema)
        cols = (
            "session_id, project_dir, started_at, duration_seconds, "
            "user_prompt_count, tool_use_count, tool_error_count, "
            "input_tokens, cache_creation_tokens, cache_read_tokens, "
            "output_tokens, cost_usd, is_subagent, agent, model_dominant"
        )
        for s in sessions:
            row = {
                "duration_seconds": None,
                "tool_use_count": 0,
                "model_dominant": None,
                **s,
            }
            placeholders = ", ".join(f":{c.strip()}" for c in cols.split(","))
            conn.execute(
                f"INSERT INTO sessions ({cols}) VALUES ({placeholders})",
                row,
            )
        for t in tool_calls or []:
            conn.execute(
                "INSERT INTO tool_calls (session_id, tool_name) VALUES "
                "(:session_id, :tool_name)",
                t,
            )


def test_agent_comparison_facts_empty_when_no_corpus(fresh_metrics):
    """Missing corpus.db → empty shape, not an exception. The viewer
    template guards on `cmp_facts.adapters|length >= 2` so an empty
    dict here naturally hides the panel."""
    out = fresh_metrics.agent_comparison_facts(days=30)
    assert out["window_days"] == 30
    assert out["adapters"] == []


def test_agent_comparison_facts_derives_ratios_and_top_lists(fresh_metrics, tmp_path):
    """End-to-end: two adapters in window, each with sessions, tool
    calls, costs. Verify every derived ratio and that top_tools /
    top_projects are sorted desc within each adapter."""
    started_recent = (datetime.now() - timedelta(days=5)).isoformat()
    sessions = [
        # Claude Code: 2 sessions, 10 prompts, 100 tool calls, $5.
        {"session_id": "cc1", "project_dir": "/home/user/kai", "started_at": started_recent,
         "duration_seconds": 600.0, "user_prompt_count": 6, "tool_use_count": 70,
         "tool_error_count": 2, "input_tokens": 1000, "cache_creation_tokens": 0,
         "cache_read_tokens": 9000, "output_tokens": 500, "cost_usd": 3.0,
         "is_subagent": 0, "agent": "claude_code", "model_dominant": "claude-opus-4-7"},
        {"session_id": "cc2", "project_dir": "/home/user/kai", "started_at": started_recent,
         "duration_seconds": 1200.0, "user_prompt_count": 4, "tool_use_count": 30,
         "tool_error_count": 1, "input_tokens": 500, "cache_creation_tokens": 0,
         "cache_read_tokens": 4500, "output_tokens": 300, "cost_usd": 2.0,
         "is_subagent": 0, "agent": "claude_code", "model_dominant": "claude-opus-4-7"},
        # Codex: 1 session, 20 prompts, 50 tool calls, $0.40 — cheaper but more prompts.
        {"session_id": "cx1", "project_dir": "/home/user/other-repo", "started_at": started_recent,
         "duration_seconds": 1800.0, "user_prompt_count": 20, "tool_use_count": 50,
         "tool_error_count": 0, "input_tokens": 8000, "cache_creation_tokens": 0,
         "cache_read_tokens": 12000, "output_tokens": 2000, "cost_usd": 0.40,
         "is_subagent": 0, "agent": "codex", "model_dominant": "gpt-5.5"},
        # Subagent — must be excluded from the rollup.
        {"session_id": "sub1", "project_dir": "/home/user/kai", "started_at": started_recent,
         "duration_seconds": 60.0, "user_prompt_count": 99, "tool_use_count": 9999,
         "tool_error_count": 0, "input_tokens": 0, "cache_creation_tokens": 0,
         "cache_read_tokens": 0, "output_tokens": 0, "cost_usd": 99.0,
         "is_subagent": 1, "agent": "claude_code", "model_dominant": "claude-opus-4-7"},
    ]
    tool_calls = (
        [{"session_id": "cc1", "tool_name": "Bash"}] * 40 +
        [{"session_id": "cc1", "tool_name": "Read"}] * 20 +
        [{"session_id": "cc2", "tool_name": "Edit"}] * 25 +
        [{"session_id": "cx1", "tool_name": "exec_command"}] * 40 +
        [{"session_id": "cx1", "tool_name": "apply_patch"}] * 10
    )
    _seed_comparison_corpus(tmp_path / "corpus.db", sessions, tool_calls)

    out = fresh_metrics.agent_comparison_facts(days=30)
    adapters = {a["agent"]: a for a in out["adapters"]}
    assert set(adapters.keys()) == {"claude_code", "codex"}, \
        "subagent session leaked into the rollup or an adapter went missing"
    cc = adapters["claude_code"]
    cx = adapters["codex"]
    # Sessions / prompts / costs aggregate across both rows for cc.
    assert cc["sessions"] == 2
    assert cc["prompts"] == 10
    assert cc["tool_calls"] == 100
    assert cc["cost_usd"] == pytest.approx(5.0)
    # Derived: $/prompt = 5 / 10 = 0.5; tool_error_rate = 3 / 100 = 0.03;
    # cache hit = 13500 / (1500 + 13500) = 0.9.
    assert cc["cost_per_prompt"] == pytest.approx(0.5)
    assert cc["prompts_per_session"] == pytest.approx(5.0)
    assert cc["tool_calls_per_session"] == pytest.approx(50.0)
    assert cc["tool_error_rate"] == pytest.approx(0.03)
    assert cc["cache_hit_rate"] == pytest.approx(0.9)
    assert cc["dominant_model"] == "claude-opus-4-7"
    # Top tools ordered by count desc: Bash (40) > Edit (25) > Read (20).
    assert [t for t, _ in cc["top_tools"][:3]] == ["Bash", "Edit", "Read"]
    # Codex side: 20 prompts, 50 tool calls, cheap.
    assert cx["sessions"] == 1
    assert cx["cost_per_prompt"] == pytest.approx(0.40 / 20)
    assert cx["dominant_model"] == "gpt-5.5"
    assert cx["top_tools"][0] == ("exec_command", 40)


def test_agent_comparison_facts_window_excludes_old(fresh_metrics, tmp_path):
    """Sessions outside the requested window must drop out of the rollup
    — the LLM narrative is window-scoped and shouldn't be influenced by
    sessions older than the user asked for."""
    recent = (datetime.now() - timedelta(days=10)).isoformat()
    ancient = (datetime.now() - timedelta(days=200)).isoformat()
    _seed_comparison_corpus(tmp_path / "corpus.db", [
        {"session_id": "r1", "project_dir": "/p", "started_at": recent,
         "duration_seconds": 60.0, "user_prompt_count": 5, "tool_use_count": 1,
         "tool_error_count": 0, "input_tokens": 0, "cache_creation_tokens": 0,
         "cache_read_tokens": 0, "output_tokens": 0, "cost_usd": 1.0,
         "is_subagent": 0, "agent": "claude_code", "model_dominant": None},
        {"session_id": "a1", "project_dir": "/p", "started_at": ancient,
         "duration_seconds": 60.0, "user_prompt_count": 999, "tool_use_count": 0,
         "tool_error_count": 0, "input_tokens": 0, "cache_creation_tokens": 0,
         "cache_read_tokens": 0, "output_tokens": 0, "cost_usd": 999.0,
         "is_subagent": 0, "agent": "claude_code", "model_dominant": None},
    ])
    out = fresh_metrics.agent_comparison_facts(days=30)
    assert len(out["adapters"]) == 1
    assert out["adapters"][0]["prompts"] == 5
    assert out["adapters"][0]["cost_usd"] == pytest.approx(1.0)


def test_cross_agent_narrative_skips_when_lt_two_active_adapters():
    """The LLM narrative is None when fewer than 2 adapters have at
    least 10 prompts each — comparing one agent in isolation is what
    the profile card already does, so the narrative would be redundant
    AND wastes an LLM call."""
    from watchmen.commands.insights import _cross_agent_narrative
    # Single adapter — skip.
    facts_one = {"window_days": 90, "adapters": [
        {"agent": "claude_code", "label": "Claude Code", "prompts": 500,
         "sessions": 10, "active_days": 5, "projects": 2, "tool_calls": 100,
         "tool_errors": 1, "cost_usd": 10.0, "cost_per_prompt": 0.02,
         "cost_per_session": 1.0, "prompts_per_session": 50.0,
         "tool_calls_per_session": 10.0, "tool_error_rate": 0.01,
         "cache_hit_rate": 0.5, "avg_session_seconds": 600.0,
         "top_tools": [("Bash", 50)], "top_projects": [("/p", 10)],
         "dominant_model": "claude-opus-4-7", "suggestions_fired": 0},
    ]}
    assert _cross_agent_narrative(facts_one, model="ignored") is None
    # Two adapters but one has <10 prompts — skip (not enough to compare).
    facts_thin = {"window_days": 90, "adapters": facts_one["adapters"] + [
        {"agent": "codex", "label": "Codex", "prompts": 3,
         "sessions": 1, "active_days": 1, "projects": 1, "tool_calls": 5,
         "tool_errors": 0, "cost_usd": 0.05, "cost_per_prompt": 0.017,
         "cost_per_session": 0.05, "prompts_per_session": 3.0,
         "tool_calls_per_session": 5.0, "tool_error_rate": 0.0,
         "cache_hit_rate": 0.0, "avg_session_seconds": 60.0,
         "top_tools": [("exec_command", 5)], "top_projects": [("/q", 1)],
         "dominant_model": "gpt-5.5", "suggestions_fired": 0},
    ]}
    assert _cross_agent_narrative(facts_thin, model="ignored") is None


# ── _count_uptake: slash-command + auto-fire channels ──────────────────────

def _seed_corpus_for_uptake(corpus_path: Path, prompts: list[dict], tool_calls: list[dict]) -> None:
    """Minimal corpus.db with the two tables `_count_uptake` reads: `prompts`
    (slash-command channel) and `tool_calls` carrying `skill_name` (auto-fire
    channel)."""
    schema = """
    CREATE TABLE prompts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT,
        timestamp TEXT,
        text TEXT
    );
    CREATE TABLE tool_calls (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT,
        timestamp TEXT,
        tool_name TEXT,
        is_error INTEGER NOT NULL DEFAULT 0,
        skill_name TEXT
    );
    """
    with sqlite3.connect(str(corpus_path)) as conn:
        conn.executescript(schema)
        for p in prompts:
            conn.execute(
                "INSERT INTO prompts (session_id, timestamp, text) VALUES (?, ?, ?)",
                (p["session_id"], p["timestamp"], p["text"]),
            )
        for t in tool_calls:
            conn.execute(
                "INSERT INTO tool_calls (session_id, timestamp, tool_name, skill_name) "
                "VALUES (?, ?, ?, ?)",
                (t["session_id"], t["timestamp"], t.get("tool_name", "Skill"), t.get("skill_name")),
            )


def _sugg(session_id: str, slug: str, ts: str) -> dict:
    return {"session_id": session_id, "skill_slug": slug, "ts": ts}


def test_uptake_slash_command_channel(fresh_metrics):
    """A later /<slug> prompt in the same session within an hour counts."""
    _seed_corpus_for_uptake(
        fresh_metrics.CORPUS_DB,
        prompts=[{"session_id": "s1", "timestamp": "2026-05-20T12:30:00.000Z",
                  "text": "let's run /portless-dev-server now"}],
        tool_calls=[],
    )
    n = fresh_metrics._count_uptake([_sugg("s1", "portless-dev-server", "2026-05-20T12:00:00")])
    assert n == 1


def test_uptake_autofire_channel(fresh_metrics):
    """A Skill tool call (auto-fire, no slash command) counts as uptake."""
    _seed_corpus_for_uptake(
        fresh_metrics.CORPUS_DB,
        prompts=[],
        tool_calls=[{"session_id": "s1", "timestamp": "2026-05-20T12:30:00.000Z",
                     "skill_name": "portless-dev-server"}],
    )
    n = fresh_metrics._count_uptake([_sugg("s1", "portless-dev-server", "2026-05-20T12:00:00")])
    assert n == 1


def test_uptake_autofire_namespace_stripped(fresh_metrics):
    """`watchmen:brief` auto-fire matches a `brief` suggestion."""
    _seed_corpus_for_uptake(
        fresh_metrics.CORPUS_DB,
        prompts=[],
        tool_calls=[{"session_id": "s1", "timestamp": "2026-05-20T12:10:00.000Z",
                     "skill_name": "watchmen:brief"}],
    )
    n = fresh_metrics._count_uptake([_sugg("s1", "brief", "2026-05-20T12:00:00")])
    assert n == 1


def test_uptake_autofire_outside_window_not_counted(fresh_metrics):
    """A Skill call more than an hour after the suggestion doesn't count."""
    _seed_corpus_for_uptake(
        fresh_metrics.CORPUS_DB,
        prompts=[],
        tool_calls=[{"session_id": "s1", "timestamp": "2026-05-20T13:30:00.000Z",
                     "skill_name": "portless-dev-server"}],
    )
    n = fresh_metrics._count_uptake([_sugg("s1", "portless-dev-server", "2026-05-20T12:00:00")])
    assert n == 0


def test_uptake_autofire_before_suggestion_not_counted(fresh_metrics):
    """A Skill call before the suggestion timestamp doesn't count."""
    _seed_corpus_for_uptake(
        fresh_metrics.CORPUS_DB,
        prompts=[],
        tool_calls=[{"session_id": "s1", "timestamp": "2026-05-20T11:30:00.000Z",
                     "skill_name": "portless-dev-server"}],
    )
    n = fresh_metrics._count_uptake([_sugg("s1", "portless-dev-server", "2026-05-20T12:00:00")])
    assert n == 0


def test_uptake_no_matching_channel_not_counted(fresh_metrics):
    """Neither a /<slug> prompt nor a matching Skill call → not taken."""
    _seed_corpus_for_uptake(
        fresh_metrics.CORPUS_DB,
        prompts=[{"session_id": "s1", "timestamp": "2026-05-20T12:30:00.000Z",
                  "text": "unrelated prompt"}],
        tool_calls=[{"session_id": "s1", "timestamp": "2026-05-20T12:30:00.000Z",
                     "skill_name": "some-other-skill"}],
    )
    n = fresh_metrics._count_uptake([_sugg("s1", "portless-dev-server", "2026-05-20T12:00:00")])
    assert n == 0


def test_uptake_both_channels_counts_suggestion_once(fresh_metrics):
    """When both channels fire for one suggestion, it still counts once."""
    _seed_corpus_for_uptake(
        fresh_metrics.CORPUS_DB,
        prompts=[{"session_id": "s1", "timestamp": "2026-05-20T12:20:00.000Z",
                  "text": "/portless-dev-server"}],
        tool_calls=[{"session_id": "s1", "timestamp": "2026-05-20T12:30:00.000Z",
                     "skill_name": "portless-dev-server"}],
    )
    n = fresh_metrics._count_uptake([_sugg("s1", "portless-dev-server", "2026-05-20T12:00:00")])
    assert n == 1


# ─── skill_effectiveness ───────────────────────────────────────────────────
#
# Builds a corpus with a sessions table (the columns the metric reads) and a
# tool_calls table carrying skill_name + cost_usd + timestamp — the #94
# accrual surface. Dates are anchored to today so the lookback window matches.

def _days_ago(n: int) -> str:
    from datetime import date as _date
    return f"{(_date.today() - timedelta(days=n)).isoformat()}T12:00:00"


def _np(posix_path: str) -> str:
    """POSIX-style test path → the running OS's native separators, so it
    round-trips through repo_dir_sql_predicate's Path() normalization on Windows
    too (project-scoped metrics normalize source_repo; raw POSIX fixtures would
    otherwise never match on Windows). Mirrors tests/test_subfolder_rollup.py."""
    import os
    return posix_path.replace("/", os.sep)


def _seed_corpus_skill_eff(corpus_path: Path, sessions: list[dict], tool_calls: list[dict]) -> None:
    """sessions + a tool_calls table with skill_name/cost_usd/timestamp, the
    exact surface skill_effectiveness joins over."""
    schema = """
    CREATE TABLE sessions (
        session_id TEXT PRIMARY KEY,
        project_dir TEXT NOT NULL,
        started_at TEXT NOT NULL,
        duration_seconds REAL,
        tool_use_count INTEGER NOT NULL DEFAULT 0,
        tool_error_count INTEGER NOT NULL DEFAULT 0,
        cost_usd REAL NOT NULL DEFAULT 0,
        input_tokens INTEGER NOT NULL DEFAULT 0,
        cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
        cache_read_tokens INTEGER NOT NULL DEFAULT 0,
        output_tokens INTEGER NOT NULL DEFAULT 0,
        is_subagent INTEGER NOT NULL DEFAULT 0,
        agent TEXT NOT NULL DEFAULT 'claude_code'
    );
    CREATE TABLE tool_calls (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT,
        timestamp TEXT,
        tool_name TEXT,
        is_error INTEGER NOT NULL DEFAULT 0,
        skill_name TEXT,
        cost_usd REAL,
        error_signature TEXT
    );
    """
    s_cols = ("session_id, project_dir, started_at, duration_seconds, "
              "tool_use_count, tool_error_count, cost_usd, is_subagent, agent")
    with sqlite3.connect(str(corpus_path)) as conn:
        conn.executescript(schema)
        for s in sessions:
            row = {"duration_seconds": None, "is_subagent": 0, "agent": "claude_code",
                   "cost_usd": 0.0, "tool_use_count": 0, "tool_error_count": 0, **s}
            ph = ", ".join(f":{c.strip()}" for c in s_cols.split(","))
            conn.execute(f"INSERT INTO sessions ({s_cols}) VALUES ({ph})", row)
        for t in tool_calls:
            row = {"skill_name": None, "cost_usd": None, "is_error": 0,
                   "error_signature": None, **t}
            conn.execute(
                "INSERT INTO tool_calls (session_id, timestamp, tool_name, is_error, skill_name, cost_usd, error_signature) "
                "VALUES (:session_id, :timestamp, :tool_name, :is_error, :skill_name, :cost_usd, :error_signature)",
                row,
            )


def test_skill_effectiveness_empty_when_no_corpus(fresh_metrics):
    assert fresh_metrics.skill_effectiveness(days=90) == []


def test_skill_effectiveness_usage_and_association(fresh_metrics):
    """A skill that fires in low-error/low-cost sessions vs same-project
    baseline: usage counts, decay split, #94 cost accrual, and an
    association (not causal) outcome delta all surface."""
    fired = ["f1", "f2", "f3"]
    base = ["b1", "b2", "b3", "b4"]
    sessions = []
    for sid in fired:  # error_rate 1/10 = 0.1, cost 0.5, dur 100
        sessions.append({"session_id": sid, "project_dir": "/pa",
                         "started_at": _days_ago(5 if sid != "f3" else 60),
                         "duration_seconds": 100.0, "tool_use_count": 10,
                         "tool_error_count": 1, "cost_usd": 0.5})
    for sid in base:  # error_rate 3/10 = 0.3, cost 1.0, dur 200
        sessions.append({"session_id": sid, "project_dir": "/pa",
                         "started_at": _days_ago(5), "duration_seconds": 200.0,
                         "tool_use_count": 10, "tool_error_count": 3, "cost_usd": 1.0})
    tool_calls = []
    for sid in fired:  # one skill activation row per fired session
        tool_calls.append({"session_id": sid, "timestamp": _days_ago(5 if sid != "f3" else 60),
                           "tool_name": "Skill", "skill_name": "deploy", "cost_usd": 0.05})
    # a non-skill tool row must be ignored by the per-skill rollup
    tool_calls.append({"session_id": "b1", "timestamp": _days_ago(5), "tool_name": "Bash"})
    _seed_corpus_skill_eff(fresh_metrics.CORPUS_DB, sessions, tool_calls)

    rows = fresh_metrics.skill_effectiveness(days=90)
    assert len(rows) == 1
    r = rows[0]
    # usage
    assert r["skill_name"] == "deploy"
    assert r["fires"] == 3
    assert r["sessions_fired"] == 3
    assert r["projects"] == 1
    assert r["cost_usd"] == pytest.approx(0.15)
    assert r["fires_recent"] == 2 and r["fires_prior"] == 1  # f1,f2 recent; f3 prior
    assert r["last_fired"] is not None and r["first_fired"] is not None
    # association
    assert r["fired"]["sessions_n"] == 3
    assert r["baseline"]["sessions_n"] == 4
    assert r["fired"]["median_error_rate"] == pytest.approx(0.1)
    assert r["baseline"]["median_error_rate"] == pytest.approx(0.3)
    assert r["delta_error_rate"] == pytest.approx(-0.2)
    assert r["delta_cost"] == pytest.approx(-0.5)
    assert "lower tool-error rate" in r["verdict"]
    assert "lower median cost" in r["verdict"]
    assert "association only" in r["verdict"]
    assert "n=3 fired vs 4 comparable" in r["verdict"]


def test_skill_effectiveness_too_few_fired_has_no_delta(fresh_metrics):
    """Under 3 fired sessions → no comparison, no causal-looking numbers."""
    sessions = [
        {"session_id": "f1", "project_dir": "/pa", "started_at": _days_ago(3),
         "tool_use_count": 5, "tool_error_count": 0, "cost_usd": 0.2},
        {"session_id": "f2", "project_dir": "/pa", "started_at": _days_ago(3),
         "tool_use_count": 5, "tool_error_count": 0, "cost_usd": 0.2},
        {"session_id": "b1", "project_dir": "/pa", "started_at": _days_ago(3),
         "tool_use_count": 5, "tool_error_count": 2, "cost_usd": 0.9},
    ]
    tool_calls = [
        {"session_id": "f1", "timestamp": _days_ago(3), "tool_name": "Skill", "skill_name": "deploy"},
        {"session_id": "f2", "timestamp": _days_ago(3), "tool_name": "Skill", "skill_name": "deploy"},
    ]
    _seed_corpus_skill_eff(fresh_metrics.CORPUS_DB, sessions, tool_calls)
    r = fresh_metrics.skill_effectiveness(days=90)[0]
    assert r["fires"] == 2
    assert r["delta_error_rate"] is None and r["delta_cost"] is None
    assert "Too few fired sessions" in r["verdict"]
    # cost_usd accrual is None on these rows → coalesces to 0.0, never errors
    assert r["cost_usd"] == pytest.approx(0.0)


def test_skill_effectiveness_project_filter_scopes_baseline(fresh_metrics, monkeypatch, tmp_path):
    """--project scopes both the skill set and its baseline to that project."""
    state_db = tmp_path / "state.db"
    with sqlite3.connect(str(state_db)) as conn:
        conn.execute("CREATE TABLE projects (project_key TEXT PRIMARY KEY, source_repo TEXT NOT NULL)")
        conn.execute("INSERT INTO projects VALUES ('pa', ?)", (_np("/pa"),))
    monkeypatch.setattr(fresh_metrics, "STATE_DB", state_db)

    sessions = []
    # project /pa: deploy fires in 3, 3 baseline
    for i in range(3):
        sessions.append({"session_id": f"af{i}", "project_dir": _np("/pa"), "started_at": _days_ago(4),
                         "tool_use_count": 10, "tool_error_count": 1, "cost_usd": 0.5})
    for i in range(3):
        sessions.append({"session_id": f"ab{i}", "project_dir": _np("/pa"), "started_at": _days_ago(4),
                         "tool_use_count": 10, "tool_error_count": 4, "cost_usd": 1.0})
    # project /pb: a different skill fires — must be excluded by the filter
    for i in range(3):
        sessions.append({"session_id": f"bf{i}", "project_dir": _np("/pb"), "started_at": _days_ago(4),
                         "tool_use_count": 10, "tool_error_count": 1, "cost_usd": 0.5})
    tool_calls = [{"session_id": f"af{i}", "timestamp": _days_ago(4), "tool_name": "Skill",
                   "skill_name": "deploy"} for i in range(3)]
    tool_calls += [{"session_id": f"bf{i}", "timestamp": _days_ago(4), "tool_name": "Skill",
                    "skill_name": "release"} for i in range(3)]
    _seed_corpus_skill_eff(fresh_metrics.CORPUS_DB, sessions, tool_calls)

    rows = fresh_metrics.skill_effectiveness(days=90, project_key="pa")
    assert [r["skill_name"] for r in rows] == ["deploy"]  # 'release' (in /pb) excluded
    r = rows[0]
    assert r["fired"]["sessions_n"] == 3 and r["baseline"]["sessions_n"] == 3
    assert r["delta_error_rate"] == pytest.approx(0.1 - 0.4)


# ─── work_matrix ────────────────────────────────────────────────────────────


def test_work_matrix_empty_when_no_corpus(fresh_metrics):
    out = fresh_metrics.work_matrix(days=90)
    assert out["rows"] == [] and out["agents"] == []


def test_work_matrix_folds_repo_x_agent_with_intensity_and_error_rate(fresh_metrics):
    rows = [
        # kai: heavy in claude_code, light in codex
        {"session_id": "k1", "project_dir": "/home/u/kai", "started_at": _days_ago(3),
         "tool_use_count": 10, "tool_error_count": 1, "cost_usd": 1.0, "agent": "claude_code"},
        {"session_id": "k2", "project_dir": "/home/u/kai", "started_at": _days_ago(3),
         "tool_use_count": 10, "tool_error_count": 1, "cost_usd": 1.0, "agent": "claude_code"},
        {"session_id": "k3", "project_dir": "/home/u/kai", "started_at": _days_ago(3),
         "tool_use_count": 10, "tool_error_count": 0, "cost_usd": 1.0, "agent": "claude_code"},
        {"session_id": "k4", "project_dir": "/home/u/kai", "started_at": _days_ago(3),
         "tool_use_count": 5, "tool_error_count": 5, "cost_usd": 0.5, "agent": "codex"},
        # watchmen: codex only
        {"session_id": "w1", "project_dir": "/home/u/watchmen", "started_at": _days_ago(3),
         "tool_use_count": 4, "tool_error_count": 0, "cost_usd": 0.2, "agent": "codex"},
        # a subagent session must be excluded
        {"session_id": "sub", "project_dir": "/home/u/kai", "started_at": _days_ago(3),
         "tool_use_count": 99, "tool_error_count": 0, "cost_usd": 9.0, "agent": "claude_code",
         "is_subagent": 1},
    ]
    _seed_corpus_skill_eff(fresh_metrics.CORPUS_DB, rows, [])
    out = fresh_metrics.work_matrix(days=90)

    # columns ordered by total sessions: claude_code (3) before codex (2)
    assert out["agents"] == ["claude_code", "codex"]
    # rows ordered by total sessions desc: kai (4) before watchmen (1)
    labels = [r["label"] for r in out["rows"]]
    assert labels == ["kai", "watchmen"]

    kai = out["rows"][0]
    assert kai["totals"]["sessions"] == 4  # subagent excluded
    cc = kai["cells"]["claude_code"]
    assert cc["sessions"] == 3 and cc["tool_errors"] == 2 and cc["tool_calls"] == 30
    assert cc["error_rate"] == pytest.approx(2 / 30, abs=1e-4)
    # busiest cell is kai/claude_code with 3 sessions → intensity 1.0
    assert out["scale"]["sessions_max"] == 3
    assert cc["intensity"] == pytest.approx(1.0)
    # kai/claude_code: 2 errors / 30 tools ≈ 7% → shown, but not hot (<10%)
    assert cc["secondary_label"] == "7%" and cc["secondary_hot"] is False
    # codex cell in kai: 5/5 errors → error_rate 1.0, intensity 1/3, flagged hot
    cx = kai["cells"]["codex"]
    assert cx["error_rate"] == pytest.approx(1.0)
    assert cx["intensity"] == pytest.approx(1 / 3, abs=1e-4)
    assert cx["secondary_label"] == "100%" and cx["secondary_hot"] is True
    # watchmen row: no claude_code cell; its codex cell is clean → blank secondary
    wm = out["rows"][1]
    assert wm["cells"].get("claude_code") is None
    assert wm["cells"]["codex"]["error_rate"] == 0.0
    assert wm["cells"]["codex"]["secondary_label"] == ""


def test_work_matrix_truncates_to_top_repos_and_reports_total(fresh_metrics):
    rows = []
    for i in range(25):  # 25 repos, 1 session each
        rows.append({"session_id": f"s{i}", "project_dir": f"/r/proj{i:02d}",
                     "started_at": _days_ago(2), "tool_use_count": 1,
                     "tool_error_count": 0, "cost_usd": 0.1, "agent": "claude_code"})
    _seed_corpus_skill_eff(fresh_metrics.CORPUS_DB, rows, [])
    out = fresh_metrics.work_matrix(days=90, top_repos=20)
    assert out["repos_total"] == 25
    assert out["repos_shown"] == 20
    assert len(out["rows"]) == 20


def test_work_matrix_metric_cost_shades_and_ranks_by_cost(fresh_metrics):
    """metric='cost' switches the colour ramp + headline + row order to cost.
    cheaprepo has more sessions but billrepo has far more cost → billrepo
    must outrank it and own the brightest cell."""
    rows = [
        {"session_id": "c1", "project_dir": "/r/cheaprepo", "started_at": _days_ago(2),
         "tool_use_count": 5, "tool_error_count": 0, "cost_usd": 0.10, "agent": "claude_code"},
        {"session_id": "c2", "project_dir": "/r/cheaprepo", "started_at": _days_ago(2),
         "tool_use_count": 5, "tool_error_count": 0, "cost_usd": 0.10, "agent": "claude_code"},
        {"session_id": "b1", "project_dir": "/r/billrepo", "started_at": _days_ago(2),
         "tool_use_count": 5, "tool_error_count": 0, "cost_usd": 9.00, "agent": "codex"},
    ]
    _seed_corpus_skill_eff(fresh_metrics.CORPUS_DB, rows, [])
    out = fresh_metrics.work_matrix(days=90, metric="cost")
    assert out["metric"] == "cost"
    assert [r["label"] for r in out["rows"]] == ["billrepo", "cheaprepo"]  # by cost, not sessions
    bill = out["rows"][0]["cells"]["codex"]
    assert bill["intensity"] == pytest.approx(1.0)          # priciest cell
    assert bill["primary_label"] == "$9.00"
    assert out["scale"]["value_max"] == pytest.approx(9.0)
    cheap = out["rows"][1]["cells"]["claude_code"]  # two $0.10 sessions → $0.20
    assert cheap["primary_label"] == "$0.20"
    assert cheap["intensity"] == pytest.approx(0.20 / 9.0, abs=1e-4)


def test_work_matrix_unknown_metric_falls_back_to_sessions(fresh_metrics):
    rows = [{"session_id": "s1", "project_dir": "/r/a", "started_at": _days_ago(1),
             "tool_use_count": 1, "tool_error_count": 0, "cost_usd": 0.1, "agent": "claude_code"}]
    _seed_corpus_skill_eff(fresh_metrics.CORPUS_DB, rows, [])
    out = fresh_metrics.work_matrix(days=90, metric="bogus")
    assert out["metric"] == "sessions"


# ─── repo_swimlane ──────────────────────────────────────────────────────────


def test_repo_swimlane_empty_when_no_corpus(fresh_metrics):
    out = fresh_metrics.repo_swimlane("proj", weeks=4, source_repo="/r/proj")
    assert out["total_sessions"] == 0 and out["agents"] == []
    assert fresh_metrics.repo_swimlane_svg(out) == ""


def test_repo_swimlane_buckets_by_day_and_agent(fresh_metrics):
    rows = [
        {"session_id": "a1", "project_dir": _np("/r/kai"), "started_at": _days_ago(2),
         "tool_use_count": 5, "tool_error_count": 0, "cost_usd": 0.5, "agent": "claude_code"},
        {"session_id": "a2", "project_dir": _np("/r/kai"), "started_at": _days_ago(2),
         "tool_use_count": 5, "tool_error_count": 1, "cost_usd": 0.5, "agent": "claude_code"},
        {"session_id": "c1", "project_dir": _np("/r/kai"), "started_at": _days_ago(1),
         "tool_use_count": 5, "tool_error_count": 0, "cost_usd": 0.3, "agent": "codex"},
        # other repo must be excluded; subagent must be excluded
        {"session_id": "x1", "project_dir": "/r/other", "started_at": _days_ago(1),
         "tool_use_count": 5, "tool_error_count": 0, "cost_usd": 0.3, "agent": "codex"},
        {"session_id": "sub", "project_dir": _np("/r/kai"), "started_at": _days_ago(1),
         "tool_use_count": 5, "tool_error_count": 0, "cost_usd": 9.0, "agent": "claude_code",
         "is_subagent": 1},
    ]
    _seed_corpus_skill_eff(fresh_metrics.CORPUS_DB, rows, [])
    out = fresh_metrics.repo_swimlane("kai", weeks=4, source_repo=_np("/r/kai"))

    assert out["total_sessions"] == 3          # subagent + other repo excluded
    assert out["agents"] == ["claude_code", "codex"]  # busiest lane first
    assert out["max_day"] == 2                 # two claude_code sessions same day
    assert len(out["days"]) == 28              # 4 weeks
    d2 = _days_ago(2)[:10]
    assert out["lanes"]["claude_code"][d2]["sessions"] == 2
    assert out["lanes"]["claude_code"][d2]["errors"] == 1
    # svg renders one <title> tooltip per populated (agent, day) cell (2 here)
    svg = fresh_metrics.repo_swimlane_svg(out)
    assert svg.startswith("<svg") and svg.count("<title>") == 2


def test_repo_swimlane_skill_fire_overlay(fresh_metrics):
    """Days a curated skill fired get a per-(agent, day) count + an amber dot
    overlay, keyed by the session's start day so it aligns with the lane."""
    rows = [
        {"session_id": "a1", "project_dir": _np("/r/kai"), "started_at": _days_ago(2),
         "tool_use_count": 5, "tool_error_count": 0, "cost_usd": 0.5, "agent": "claude_code"},
        {"session_id": "a2", "project_dir": _np("/r/kai"), "started_at": _days_ago(1),
         "tool_use_count": 5, "tool_error_count": 0, "cost_usd": 0.5, "agent": "claude_code"},
    ]
    tool_calls = [
        # a1 fired a skill twice; a2 fired none
        {"session_id": "a1", "timestamp": _days_ago(2), "tool_name": "Skill", "skill_name": "deploy"},
        {"session_id": "a1", "timestamp": _days_ago(2), "tool_name": "Skill", "skill_name": "deploy"},
        {"session_id": "a2", "timestamp": _days_ago(1), "tool_name": "Bash"},  # not a skill
    ]
    _seed_corpus_skill_eff(fresh_metrics.CORPUS_DB, rows, tool_calls)
    out = fresh_metrics.repo_swimlane("kai", weeks=4, source_repo=_np("/r/kai"))
    assert out["total_skill_fires"] == 2
    assert out["lanes"]["claude_code"][_days_ago(2)[:10]]["skill_fires"] == 2
    assert out["lanes"]["claude_code"][_days_ago(1)[:10]]["skill_fires"] == 0
    svg = fresh_metrics.repo_swimlane_svg(out)
    assert "#f59e0b" in svg and "curated skill fired" in svg  # dot + legend


def test_repo_swimlane_landing_marker_only_within_window(fresh_metrics, monkeypatch, tmp_path):
    state_db = tmp_path / "state.db"
    with sqlite3.connect(str(state_db)) as conn:
        conn.execute("CREATE TABLE runs (project_key TEXT, kind TEXT, status TEXT, started_at TEXT)")
        # a curator run 1 day ago → inside a 4-week window
        conn.execute("INSERT INTO runs VALUES ('kai','curator','ok',?)", (_days_ago(1),))
    monkeypatch.setattr(fresh_metrics, "STATE_DB", state_db)
    rows = [{"session_id": "a1", "project_dir": _np("/r/kai"), "started_at": _days_ago(2),
             "tool_use_count": 1, "tool_error_count": 0, "cost_usd": 0.1, "agent": "claude_code"}]
    _seed_corpus_skill_eff(fresh_metrics.CORPUS_DB, rows, [])
    out = fresh_metrics.repo_swimlane("kai", weeks=4, source_repo=_np("/r/kai"))
    assert out["landing"] == _days_ago(1)[:10]
    assert "skills landed" in fresh_metrics.repo_swimlane_svg(out)


# ─── friction_ledger (#110) ──────────────────────────────────────────────
#
# Recurring tool failures grouped by error_signature, ranked recurrence ×
# recency. Reuses _seed_corpus_skill_eff (its tool_calls table now carries
# error_signature). Cross-project (project_key=None) so no STATE_DB needed.

def _err(session_id, sig, days_ago, tool="Edit"):
    return {"session_id": session_id, "timestamp": _days_ago(days_ago),
            "tool_name": tool, "is_error": 1, "error_signature": sig}


def test_friction_ledger_empty_when_no_corpus(fresh_metrics):
    out = fresh_metrics.friction_ledger(days=90)
    assert out["entries"] == [] and out["total_errors"] == 0


def test_friction_ledger_groups_and_ranks_by_recurrence_recency(fresh_metrics):
    sessions = [{"session_id": f"s{i}", "project_dir": "/pa", "started_at": _days_ago(2),
                 "tool_use_count": 20, "tool_error_count": 5} for i in range(4)]
    tools = []
    # "stale read" — frequent AND recent → should rank first.
    for i in range(6):
        tools.append(_err(f"s{i % 4}", "file modified since read", 0))
    # "not found" — same count but older → lower score.
    for i in range(6):
        tools.append(_err(f"s{i % 4}", "string not found", 40))
    # a singleton — must be excluded from entries but counted.
    tools.append(_err("s0", "one off blip", 1))
    _seed_corpus_skill_eff(fresh_metrics.CORPUS_DB, sessions, tools)

    out = fresh_metrics.friction_ledger(days=90)
    assert out["total_errors"] == 13
    assert out["signatured"] == 13
    assert out["singletons"] == 1
    assert out["distinct"] == 2  # singleton excluded
    sigs = [e["signature"] for e in out["entries"]]
    assert sigs == ["file modified since read", "string not found"]  # recent wins
    top = out["entries"][0]
    assert top["occurrences"] == 6
    assert top["sessions"] == 4
    assert top["days_since_last"] == 0


def test_friction_ledger_excludes_subagents_and_unsignatured(fresh_metrics):
    sessions = [
        {"session_id": "main", "project_dir": "/pa", "started_at": _days_ago(1),
         "tool_use_count": 10, "tool_error_count": 3, "is_subagent": 0},
        {"session_id": "sub", "project_dir": "/pa", "started_at": _days_ago(1),
         "tool_use_count": 10, "tool_error_count": 3, "is_subagent": 1},
    ]
    tools = [
        _err("main", "real mistake", 0), _err("main", "real mistake", 0),
        # subagent error — excluded entirely.
        _err("sub", "real mistake", 0),
        # an errored row with NO signature (a user cancel) — counts toward
        # total_errors but never appears as a signature.
        {"session_id": "main", "timestamp": _days_ago(0), "tool_name": "Bash",
         "is_error": 1, "error_signature": None},
    ]
    _seed_corpus_skill_eff(fresh_metrics.CORPUS_DB, sessions, tools)

    out = fresh_metrics.friction_ledger(days=90)
    assert out["total_errors"] == 3  # 2 main signatured + 1 unsignatured; sub excluded
    assert out["signatured"] == 2
    assert len(out["entries"]) == 1
    assert out["entries"][0]["occurrences"] == 2


def test_friction_ledger_window_cutoff(fresh_metrics):
    sessions = [{"session_id": "s", "project_dir": "/pa", "started_at": _days_ago(1),
                 "tool_use_count": 10, "tool_error_count": 4}]
    tools = [
        _err("s", "recent mistake", 2), _err("s", "recent mistake", 3),
        _err("s", "ancient mistake", 200), _err("s", "ancient mistake", 201),
    ]
    _seed_corpus_skill_eff(fresh_metrics.CORPUS_DB, sessions, tools)

    out = fresh_metrics.friction_ledger(days=30)
    assert [e["signature"] for e in out["entries"]] == ["recent mistake"]


def test_friction_ledger_weekly_spark_buckets(fresh_metrics):
    sessions = [{"session_id": "s", "project_dir": "/pa", "started_at": _days_ago(1),
                 "tool_use_count": 10, "tool_error_count": 5}]
    # Two this week, one ~3 weeks ago.
    tools = [_err("s", "m", 0), _err("s", "m", 1), _err("s", "m", 21)]
    _seed_corpus_skill_eff(fresh_metrics.CORPUS_DB, sessions, tools)

    out = fresh_metrics.friction_ledger(days=90, weeks=8)
    spark = out["entries"][0]["spark"]
    assert len(spark) == 8
    assert spark[-1] == 2          # current week
    assert sum(spark) == 3


def test_project_scoped_metrics_roll_up_subfolder_sessions(fresh_metrics, monkeypatch, tmp_path):
    """A session opened from a SUBFOLDER of the tracked repo must count toward
    the project's metrics — consistent with the shared repo_dir_sql_predicate
    (#93) the rest of the app uses — while a prefix-sibling dir must NOT match.
    Guards the rollup-consistency fix so project pages stop undercounting."""
    state_db = tmp_path / "state.db"
    with sqlite3.connect(str(state_db)) as conn:
        conn.execute("CREATE TABLE projects (project_key TEXT PRIMARY KEY, source_repo TEXT NOT NULL)")
        conn.execute("INSERT INTO projects VALUES ('app', ?)", (_np("/r/app"),))
    monkeypatch.setattr(fresh_metrics, "STATE_DB", state_db)

    sessions = [
        {"session_id": "root", "project_dir": _np("/r/app"), "started_at": _days_ago(2),
         "tool_use_count": 5, "tool_error_count": 1},
        {"session_id": "sub", "project_dir": _np("/r/app/packages/api"), "started_at": _days_ago(2),
         "tool_use_count": 5, "tool_error_count": 1},
        {"session_id": "sibling", "project_dir": _np("/r/app-other"), "started_at": _days_ago(2),
         "tool_use_count": 5, "tool_error_count": 1},
    ]
    tools = [
        _err("root", "same mistake", 1),
        _err("sub", "same mistake", 1),      # subfolder → rolls up
        _err("sibling", "same mistake", 1),  # prefix sibling → excluded
    ]
    _seed_corpus_skill_eff(fresh_metrics.CORPUS_DB, sessions, tools)

    fr = fresh_metrics.friction_ledger(days=90, project_key="app")
    assert fr["entries"][0]["occurrences"] == 2   # root + sub, not the sibling
    assert fr["entries"][0]["sessions"] == 2

    # skill_effectiveness is project-scoped through the same helper — it must
    # roll up the subfolder session's data without error.
    eff = fresh_metrics.skill_effectiveness(days=90, project_key="app")
    assert isinstance(eff, list)
