"""Tests for the prompt-intent map (watchmen.semantic, #111).

Two layers:

1. Pure-Python tests that run everywhere (no numpy/sklearn): the genuine-prompt
   filter, turn-level error classification, cache-key stability, and the
   MapDepsMissing hint when the optional `[map]` extra is absent.
2. A full `intent_map` build gated on `importorskip("sklearn")` — it injects a
   deterministic STUB embedder (so no model download, offline-safe) but uses
   the real PCA/t-SNE projection. Runs wherever the [map] extra is installed
   (locally); skipped in the core CI matrix, which doesn't ship the extra.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pytest

from watchmen import semantic


# ── Layer 1: pure-Python (CI-safe) ───────────────────────────────────────

def test_is_genuine_drops_synthetic_and_short():
    # synthetic / harness-injected
    assert not semantic._is_genuine("<task-notification> <task-id>b1</task-id>")
    assert not semantic._is_genuine("Pull request #42 got a `request_changes` review")
    assert not semantic._is_genuine("# AGENTS.md instructions for /Users/x")
    assert not semantic._is_genuine("<system-reminder>do the thing</system-reminder>")
    assert not semantic._is_genuine("[Request interrupted by user]")
    # too short to be intent
    assert not semantic._is_genuine("go ahead")
    assert not semantic._is_genuine("resume")
    # genuine task intents survive
    assert semantic._is_genuine("fix the failing auth test")
    assert semantic._is_genuine("can you refactor the parser to stream tokens?")


def test_classify_errors_turn_boundaries():
    # S1: p1 [t1 ok, t2 err] p2 [t3 ok] ; errored turn = p1, p2 clean-ish
    session_rows = {"S1": [(1, "2026-05-01T10:00:00"), (2, "2026-05-01T10:05:00")]}
    tool_rows = {"S1": [
        ("2026-05-01T10:00:30", False),
        ("2026-05-01T10:01:00", True),   # error inside p1's turn
        ("2026-05-01T10:06:00", False),  # inside p2's turn
    ]}
    errored, had_tools = semantic._classify_errors(session_rows, tool_rows)
    assert errored == {1}
    assert had_tools == {1: True, 2: True}


def test_classify_errors_no_tools_turn_is_candidate():
    session_rows = {"S": [(1, "2026-05-01T10:00:00"), (2, "2026-05-01T10:05:00")]}
    errored, had_tools = semantic._classify_errors(session_rows, {"S": []})
    assert errored == set()
    assert had_tools == {1: False, 2: False}


def test_cache_key_order_invariant_and_model_sensitive():
    a = semantic._cache_key("m1", [3, 1, 2])
    b = semantic._cache_key("m1", [1, 2, 3])
    c = semantic._cache_key("m2", [1, 2, 3])
    assert a == b           # set identity, not order
    assert a != c           # different model → different cache


def test_model2vec_embedder_raises_helpful_when_missing(monkeypatch):
    import builtins
    real_import = builtins.__import__

    def _block(name, *args, **kwargs):
        if name == "model2vec" or name.startswith("model2vec."):
            raise ImportError("no model2vec")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _block)
    with pytest.raises(semantic.MapDepsMissing) as ei:
        semantic.Model2VecEmbedder()._ensure()
    assert "watchmen[map]" in str(ei.value)


# ── Layer 2: full build with a stub embedder (needs sklearn) ──────────────

def _days_ago(n: int) -> str:
    return f"{(date.today() - timedelta(days=n)).isoformat()}T12:00:00"


class _StubEmbedder:
    """Deterministic offline embedder: identical text → identical vector (so a
    repeated prompt reads as a rephrase), different text → different vector."""

    def __init__(self):
        self.calls = 0

    def encode(self, texts):
        import hashlib

        import numpy as np
        self.calls += 1
        out = []
        for t in texts:
            h = hashlib.sha256(t.strip().lower().encode()).digest()
            v = np.frombuffer(h, dtype=np.uint8).astype(np.float32)  # 32 dims
            out.append(v)
        return np.vstack(out)


def _seed_corpus(path: Path, sessions, prompts, tool_calls):
    schema = """
    CREATE TABLE sessions (session_id TEXT PRIMARY KEY, project_dir TEXT,
        is_subagent INTEGER DEFAULT 0, agent TEXT DEFAULT 'claude_code');
    CREATE TABLE prompts (id INTEGER PRIMARY KEY, session_id TEXT, timestamp TEXT,
        text TEXT, word_count INTEGER, is_first_in_session INTEGER DEFAULT 0);
    CREATE TABLE tool_calls (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT,
        timestamp TEXT, tool_name TEXT, is_error INTEGER DEFAULT 0);
    """
    with sqlite3.connect(str(path)) as conn:
        conn.executescript(schema)
        for s in sessions:
            conn.execute("INSERT INTO sessions (session_id, project_dir, is_subagent) VALUES (?,?,?)",
                         (s["session_id"], s["project_dir"], s.get("is_subagent", 0)))
        for p in prompts:
            conn.execute("INSERT INTO prompts (id, session_id, timestamp, text) VALUES (?,?,?,?)",
                         (p["id"], p["session_id"], p["timestamp"], p["text"]))
        for t in tool_calls:
            conn.execute("INSERT INTO tool_calls (session_id, timestamp, tool_name, is_error) VALUES (?,?,?,?)",
                         (t["session_id"], t["timestamp"], t.get("tool_name", "Bash"), t.get("is_error", 0)))


@pytest.fixture
def map_env(tmp_path, monkeypatch):
    pytest.importorskip("numpy")
    pytest.importorskip("sklearn")
    corpus = tmp_path / "corpus.db"
    intent = tmp_path / "intent.db"
    monkeypatch.setattr(semantic, "CORPUS_DB", corpus)
    monkeypatch.setattr(semantic, "INTENT_DB", intent)
    stub = _StubEmbedder()
    monkeypatch.setattr(semantic, "EMBEDDER", stub)
    return semantic, corpus, stub


def test_intent_map_empty_without_corpus(map_env):
    sem, _corpus, _stub = map_env
    out = sem.intent_map(days=90)
    assert out["points"] == [] and out["total_prompts"] == 0


def test_intent_map_classifies_outcomes(map_env):
    sem, corpus, stub = map_env
    sessions = [{"session_id": "S1", "project_dir": "/r/app"},
                {"session_id": "S2", "project_dir": "/r/app"}]
    prompts = [
        # S1: errored turn (tool error between p1 and p2)
        {"id": 1, "session_id": "S1", "timestamp": _days_ago(3),
         "text": "build the user authentication feature"},
        {"id": 2, "session_id": "S1", "timestamp": _days_ago(3).replace("12:00", "12:10"),
         "text": "now wire the password reset flow end to end"},
        # S2: p3 then an identical restatement p4 with NO tools between → rephrase
        {"id": 3, "session_id": "S2", "timestamp": _days_ago(2),
         "text": "deploy the service to the staging cluster"},
        {"id": 4, "session_id": "S2", "timestamp": _days_ago(2).replace("12:00", "12:05"),
         "text": "deploy the service to the staging cluster"},
    ]
    tool_calls = [
        {"session_id": "S1", "timestamp": _days_ago(3).replace("12:00", "12:02"), "is_error": 1},
        {"session_id": "S1", "timestamp": _days_ago(3).replace("12:00", "12:12"), "is_error": 0},
    ]
    _seed_corpus(corpus, sessions, prompts, tool_calls)

    out = sem.intent_map(days=90)
    oc = {p["text"][:12]: p["outcome"] for p in out["points"]}
    assert out["shown"] == 4
    assert oc["build the us"] == "errored"      # tool error in turn
    # the first 'deploy' has an identical next prompt with no tools → rephrase
    assert out["outcomes"]["rephrase"] >= 1
    assert out["outcomes"]["errored"] == 1
    # every point carries a 2D coord + repo label
    assert all(isinstance(p["x"], float) and p["repo"] == "app" for p in out["points"])


def test_intent_map_caches_embeddings(map_env):
    sem, corpus, stub = map_env
    sessions = [{"session_id": "S", "project_dir": "/r/app"}]
    prompts = [{"id": i, "session_id": "S", "timestamp": _days_ago(2),
                "text": f"investigate the flaky integration test number {i}"} for i in range(5)]
    _seed_corpus(corpus, sessions, prompts, [])

    sem.intent_map(days=90)
    first_calls = stub.calls
    assert first_calls >= 1
    sem.intent_map(days=90)  # second build: vectors cached, no re-embed
    assert stub.calls == first_calls


def test_intent_map_samples_and_flags(map_env):
    sem, corpus, stub = map_env
    sessions = [{"session_id": "S", "project_dir": "/r/app"}]
    prompts = [{"id": i, "session_id": "S", "timestamp": _days_ago(2),
                "text": f"please refactor module number {i} for clarity and tests"}
               for i in range(30)]
    _seed_corpus(corpus, sessions, prompts, [])

    out = sem.intent_map(days=90, max_points=10)
    assert out["sampled"] is True
    assert out["total_prompts"] == 30
    assert out["shown"] == 10
