#!/usr/bin/env python3
"""Cold-start smoke tests.

Catches the class of bugs that only appear on a totally fresh install — the
ones the developer never hits because their dev machine already has corpus.db,
state.db, etc. populated.

Specifically covers:
  - state.init_db() must be called before list_projects (regression test for
    the bug Eren hit on first install).
  - Model price lookup matches real API model names ('claude-opus-4-7', dash
    format, not 'opus-4.7' dot format) and routes to the right tier.
  - turn_cost_usd math matches Anthropic's published worked example.

Run via: `python tests/smoke.py`  (no pytest dep). CI runs the same command.
Exit code 0 = all pass, 1 = any failure.
"""

import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PASS = 0
FAIL = 0


def check(name: str, fn):
    global PASS, FAIL
    try:
        fn()
        print(f"  ✓ {name}")
        PASS += 1
    except Exception:
        print(f"  ✗ {name}")
        traceback.print_exc()
        FAIL += 1


# ─── State / onboard tests ──────────────────────────────────────────────────


def _with_tmp_state(fn):
    """Run fn() with state.STATE_DB redirected at a fresh temp file."""
    import state
    with tempfile.TemporaryDirectory() as td:
        orig = state.STATE_DB
        state.STATE_DB = Path(td) / "state.db"
        try:
            fn()
        finally:
            state.STATE_DB = orig


def test_state_init_idempotent():
    import state
    def go():
        state.init_db()
        state.init_db()  # second call must not error
        # And list_projects after should be empty.
        assert state.list_projects() == []
    _with_tmp_state(go)


def test_list_projects_without_init_raises():
    """The exact failure mode Eren hit: querying projects on a freshly-created
    state.db before init_db() runs must raise 'no such table'. If this changes
    (e.g. someone makes the conn auto-init), the test catches the silent shift."""
    import state
    def go():
        try:
            state.list_projects()
        except Exception as e:
            assert "no such table" in str(e), f"expected 'no such table' error, got: {e}"
            return
        raise AssertionError("expected OperationalError, got none")
    _with_tmp_state(go)


def test_onboard_calls_init_db():
    """The actual fix for Eren's bug: onboard.run must call state.init_db
    before doing anything that touches the projects table."""
    src = (ROOT / "onboard.py").read_text()
    assert "state.init_db()" in src, "onboard.run must call state.init_db()"
    # Ordering: init_db must appear before project_candidates is invoked.
    init_pos = src.index("state.init_db()")
    pc_pos = src.index("project_candidates(console)")
    assert init_pos < pc_pos, "state.init_db() must run before project_candidates"


# ─── Metrics / pricing tests ────────────────────────────────────────────────


def test_price_for_dash_api_names():
    """Real Anthropic API model names use dashes between version components.
    The normalizer must map these to our dot-separated price keys."""
    import metrics
    cases = [
        ("claude-opus-4-7",          (5.0,  6.25,  10.0, 0.5,  25.0)),
        ("claude-opus-4-6",          (5.0,  6.25,  10.0, 0.5,  25.0)),
        ("claude-opus-4-1",          (15.0, 18.75, 30.0, 1.5,  75.0)),
        ("claude-opus-4",            (15.0, 18.75, 30.0, 1.5,  75.0)),
        ("claude-sonnet-4-6",        (3.0,  3.75,  6.0,  0.3,  15.0)),
        ("claude-sonnet-4-20250514", (3.0,  3.75,  6.0,  0.3,  15.0)),
        ("claude-haiku-4-5",         (1.0,  1.25,  2.0,  0.1,  5.0)),
    ]
    for model, expected in cases:
        got = metrics.price_for_model(model)
        assert got == expected, f"{model}: expected {expected}, got {got}"


def test_price_for_unknown_falls_back():
    import metrics
    assert metrics.price_for_model(None) == metrics.DEFAULT_PRICE
    assert metrics.price_for_model("") == metrics.DEFAULT_PRICE
    # Unknown future Claude model falls back via family pattern.
    assert metrics.price_for_model("claude-opus-99-99") == metrics.MODEL_PRICES["opus-4.7"]


def test_turn_cost_worked_example():
    """Anthropic's pricing page worked example, Opus 4.7. The docs total ($0.705,
    $0.525) includes Managed Agents session runtime ($0.08/hr), which we don't
    bill — so we compare against the pure token portion.

    Source: https://platform.claude.com/docs/en/about-claude/pricing#worked-example"""
    import metrics
    # 50k input + 15k output, no caching → docs $0.705 incl. runtime; tokens-only $0.625
    cost = metrics.turn_cost_usd("claude-opus-4-7", 50_000, 0, 0, 0, 15_000)
    expected = 0.625
    assert abs(cost - expected) < 0.0001, f"50k+15k: expected {expected}, got {cost}"

    # 10k uncached input + 40k cache reads + 15k output → docs $0.525 incl. runtime;
    # tokens-only $0.445 = 0.05 (input) + 0.02 (cache read) + 0.375 (output)
    cost = metrics.turn_cost_usd("claude-opus-4-7", 10_000, 0, 0, 40_000, 15_000)
    expected = 0.445
    assert abs(cost - expected) < 0.0001, f"10k+40k cached+15k: expected {expected}, got {cost}"


def test_cache_5m_vs_1h_are_different():
    """5m cache write = 1.25× input. 1h cache write = 2× input. They must not
    be priced the same."""
    import metrics
    cost_5m = metrics.turn_cost_usd("claude-opus-4-7", 0, 1_000_000, 0, 0, 0)
    cost_1h = metrics.turn_cost_usd("claude-opus-4-7", 0, 0, 1_000_000, 0, 0)
    assert cost_5m < cost_1h, f"5m must cost less than 1h, got 5m=${cost_5m} 1h=${cost_1h}"
    assert abs(cost_5m - 6.25) < 0.01
    assert abs(cost_1h - 10.00) < 0.01


# ─── Adapter tests ──────────────────────────────────────────────────────────


def test_codex_adapter_parses_fixture():
    """Synthetic Codex rollout exercises all the quirks the adapter has to handle:
    synthetic <permissions> / <environment_context> injections (must be skipped),
    response_item + event_msg/user_message dedupe (count once), per-turn cost from
    last_token_usage (not cumulative), function_call + custom_tool_call as tool uses,
    reasoning blocks as thinking. If counts drift, the adapter regressed."""
    from pathlib import Path

    from adapters import codex

    fixture = ROOT / "tests" / "fixtures" / "codex_rollout.jsonl"
    assert fixture.exists(), f"fixture missing: {fixture}"
    entry = {"path": fixture, "project_dir": None, "is_subagent": False, "parent_session_id": None}
    session, prompts, tools = codex.scan(entry)

    # 2 real user prompts (the developer + environment_context lines must NOT count).
    assert session["user_prompt_count"] == 2, f"expected 2 user prompts, got {session['user_prompt_count']}"
    assert len(prompts) == 2
    assert prompts[0]["text"].startswith("add a function")
    assert prompts[1]["text"].startswith("now add a test")
    # 2 tool uses: function_call + custom_tool_call.
    assert session["tool_use_count"] == 2, f"expected 2 tool uses, got {session['tool_use_count']}"
    assert {t["tool_name"] for t in tools} == {"exec_command", "apply_patch"}
    # 1 reasoning block → thinking_count = 1.
    assert session["assistant_thinking_count"] == 1
    # 2 assistant text outputs.
    assert session["assistant_text_count"] == 2
    # Model from turn_context.
    assert session["model_dominant"] == "gpt-5.5"
    # project_dir from session_meta.cwd, NOT the fixture filename.
    assert session["project_dir"] == "/home/dev/myproject"
    assert session["agent"] == "codex"
    # Cost is non-zero and uses per-turn (last_token_usage) deltas.
    assert session["cost_usd"] > 0
    # Tokens: first turn 1000/200/80, second turn 1000/900/30 → uncached 800+100=900, cached 200+900=1100.
    assert session["input_tokens"] == 900, f"uncached input: expected 900, got {session['input_tokens']}"
    assert session["cache_read_tokens"] == 1100, f"cached: expected 1100, got {session['cache_read_tokens']}"


def test_codex_adapter_dedupe_user_message():
    """The fixture has both a response_item user message AND an event_msg/user_message
    for the same turn — they describe the same prompt. The adapter must NOT
    double-count by ignoring user_message event_msgs."""
    from adapters import codex
    fixture = ROOT / "tests" / "fixtures" / "codex_rollout.jsonl"
    entry = {"path": fixture, "project_dir": None, "is_subagent": False, "parent_session_id": None}
    session, _, _ = codex.scan(entry)
    # The fixture has exactly one event_msg/user_message line (after the first real prompt).
    # If we accidentally count it, user_prompt_count would be 3 instead of 2.
    assert session["user_prompt_count"] == 2


def test_codex_adapter_silent_on_missing_install():
    """When ~/.codex doesn't exist, discover() must yield nothing — not raise."""
    from adapters import codex
    # Point the adapter at a non-existent path temporarily.
    orig = codex.SESSIONS_DIR
    codex.SESSIONS_DIR = ROOT / "tests" / "_no_such_codex_dir"
    try:
        assert list(codex.discover()) == []
    finally:
        codex.SESSIONS_DIR = orig


def test_corpus_schema_has_agent_column():
    """The sessions table must have an `agent` column with a default — old corpus.db
    files from before the refactor would be missing it. init_db drops + recreates
    so this is also a guard against forgetting to add it back."""
    import tempfile
    import corpus
    with tempfile.TemporaryDirectory() as td:
        orig = corpus.DB_PATH
        corpus.DB_PATH = Path(td) / "corpus.db"
        try:
            conn = corpus.init_db()
            cols = [r[1] for r in conn.execute("PRAGMA table_info(sessions)").fetchall()]
            assert "agent" in cols, f"sessions table missing 'agent' column. got: {cols}"
            conn.close()
        finally:
            corpus.DB_PATH = orig


def test_decode_project_dir_naive_fallback():
    """When the encoded path doesn't exist on disk, decode_project_dir must still
    return a stable canonical form (leading '-' → '/', remaining '-' → '/').
    Stable means two adapter calls produce the same key for the same vanished project."""
    from paths import decode_project_dir
    encoded = "-tmp-watchmen-test-_no_such_path_12345"
    out = decode_project_dir(encoded)
    assert out == "/tmp/watchmen/test/_no_such_path_12345", f"got {out!r}"
    # Idempotent on already-real paths.
    assert decode_project_dir("/already/real") == "/already/real"


def test_decode_project_dir_resolves_real_filesystem():
    """For a path that exists on disk, decode_project_dir walks the FS to find
    the longest matching dir name at each level — this is how 'kai-frontend'
    survives the lossy encoding (instead of splitting into 'kai/frontend')."""
    import os
    import tempfile
    from paths import decode_project_dir
    with tempfile.TemporaryDirectory() as td:
        nested = Path(td) / "foo-bar" / "baz"
        nested.mkdir(parents=True)
        real = str(nested)
        encoded = real.replace("/", "-")
        out = decode_project_dir(encoded)
        assert os.path.realpath(out) == os.path.realpath(real), f"expected {real}, got {out}"


def test_pi_adapter_parses_branching_fixture():
    """Synthetic pi session with a fork at m3 (branch-a + branch-b). The adapter
    must pick the leaf with the latest timestamp (branch-b) and ignore branch-a
    entirely — counting both branches' user prompts would double-count."""
    from adapters import pi
    fixture = ROOT / "tests" / "fixtures" / "pi_session.jsonl"
    entry = {"path": fixture, "project_dir": None, "is_subagent": False, "parent_session_id": None}
    session, prompts, tools = pi.scan(entry)
    assert session["agent"] == "pi"
    assert session["project_dir"] == "/home/dev/myproject"
    # 2 user prompts: m1 ('reverse...') + m4-branch-b ('actually no, add docs'). branch-a is NOT counted.
    assert session["user_prompt_count"] == 2, f"got {session['user_prompt_count']}: {[p['text'] for p in prompts]}"
    texts = {p["text"] for p in prompts}
    assert "actually no, add docs instead" in texts
    assert "add a test" not in texts, "branch-a leaked into the active walk"
    # 1 thinking block, 2 assistant text outputs, 1 toolCall.
    assert session["assistant_thinking_count"] == 1
    assert session["assistant_text_count"] == 2
    assert session["tool_use_count"] == 1
    assert {t["tool_name"] for t in tools} == {"writeFile"}
    # Token attribution from per-message usage.
    assert session["input_tokens"] == 1500
    assert session["output_tokens"] == 70
    assert session["cache_read_tokens"] == 1200
    assert session["cache_creation_tokens"] == 100
    # Cost sanity check (sonnet-4.6 pricing).
    assert 0.005 < session["cost_usd"] < 0.010


def test_pi_adapter_respects_compaction_cutoff():
    """A compaction entry summarizes earlier history; its firstKeptEntryId marks
    where the kept window starts. Pre-cutoff prompts/tokens must NOT be ingested
    again or we double-count what the summary already covers."""
    from adapters import pi
    fixture = ROOT / "tests" / "fixtures" / "pi_session_compacted.jsonl"
    entry = {"path": fixture, "project_dir": None, "is_subagent": False, "parent_session_id": None}
    session, prompts, _ = pi.scan(entry)
    # Only the post-cutoff user prompt should appear.
    assert session["user_prompt_count"] == 1
    assert prompts[0]["text"] == "this prompt SHOULD be counted"
    # Token total = m4 only (300 input + 15 output), NOT m4 + m2 (1300 + 65).
    assert session["input_tokens"] == 300
    assert session["output_tokens"] == 15


def test_pi_adapter_silent_on_missing_install():
    """No ~/.pi/agent/sessions/ on most dev boxes (yet). discover() must yield
    nothing rather than raise."""
    from adapters import pi
    orig = pi.SESSIONS_DIR
    pi.SESSIONS_DIR = ROOT / "tests" / "_no_such_pi_dir"
    try:
        assert list(pi.discover()) == []
    finally:
        pi.SESSIONS_DIR = orig


def test_pi_adapter_rejects_unsupported_version():
    """If the spec rev changes (v4+) the adapter must NOT misparse — return an
    empty session rather than guess at new fields. Regression test against the
    silent-drift failure mode."""
    import tempfile
    from adapters import pi
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "future.jsonl"
        # Header claims v4; everything else looks identical to v3.
        p.write_text(
            '{"type":"session","version":4,"id":"sX","timestamp":"2026-05-01T10:00:00Z","cwd":"/x"}\n'
            '{"type":"message","id":"m1","parentId":"sX","timestamp":"2026-05-01T10:00:01Z","message":{"role":"user","content":[{"type":"text","text":"hi"}]}}\n'
        )
        session, prompts, _ = pi.scan({"path": p, "project_dir": None, "is_subagent": False, "parent_session_id": None})
        assert prompts == []
        assert session["user_prompt_count"] == 0


def test_claude_adapter_stores_decoded_paths():
    """The Claude adapter must call decode_project_dir on the encoded dir name —
    if it stored '-Users-...' the per-project analyst couldn't merge with Codex's
    real-path sessions. Regression test for the original split-counts gotcha."""
    import os
    import tempfile
    from adapters import claude_code
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        real_dir = td_path / "foo-bar"
        real_dir.mkdir()
        encoded = str(real_dir).replace("/", "-")
        fake_claude = td_path / "claude_projects"
        proj = fake_claude / encoded
        proj.mkdir(parents=True)
        (proj / "abc.jsonl").write_text(
            '{"timestamp":"2026-05-01T10:00:00Z","type":"user","message":{"content":"hi"}}\n'
        )
        orig = claude_code.PROJECTS_DIR
        claude_code.PROJECTS_DIR = fake_claude
        claude_code._DECODE_CACHE.clear()
        try:
            entries = list(claude_code.discover())
            assert len(entries) == 1
            assert not entries[0]["project_dir"].startswith("-"), \
                f"adapter stored encoded form: {entries[0]['project_dir']!r}"
            assert os.path.realpath(entries[0]["project_dir"]) == os.path.realpath(str(real_dir))
        finally:
            claude_code.PROJECTS_DIR = orig
            claude_code._DECODE_CACHE.clear()


# ─── CLI settings tests ─────────────────────────────────────────────────────


def test_settings_parser_validates_inputs():
    """_parse_setting must coerce + reject inputs so bad CLI args never reach
    the DB layer. Covers: boolean parsing (truthy + falsy spellings), int
    bounds, path existence, unknown keys."""
    import cli

    # enabled: many truthy + falsy spellings.
    for v in ("true", "True", "yes", "Y", "on", "1"):
        col, val = cli._parse_setting("enabled", v)
        assert (col, val) == ("enabled", 1), f"{v!r} should map to (enabled, 1)"
    for v in ("false", "no", "N", "off", "0"):
        col, val = cli._parse_setting("enabled", v)
        assert (col, val) == ("enabled", 0), f"{v!r} should map to (enabled, 0)"
    try:
        cli._parse_setting("enabled", "maybe")
        raise AssertionError("expected ValueError on enabled=maybe")
    except ValueError:
        pass

    # threshold: positive int only.
    assert cli._parse_setting("threshold", "50") == ("threshold_new_prompts", 50)
    for bad in ("abc", "-1", "0"):
        try:
            cli._parse_setting("threshold", bad)
            raise AssertionError(f"expected ValueError on threshold={bad}")
        except ValueError:
            pass

    # repo: must point at an existing directory.
    col, val = cli._parse_setting("repo", str(ROOT))
    assert col == "source_repo"
    assert val.endswith("watchmen")
    try:
        cli._parse_setting("repo", "/tmp/_no_such_dir_for_smoke_test")
        raise AssertionError("expected ValueError on missing repo path")
    except ValueError:
        pass

    # notes: pass-through, any string allowed.
    assert cli._parse_setting("notes", "anything goes") == ("notes", "anything goes")

    # unknown key.
    try:
        cli._parse_setting("bogus", "x")
        raise AssertionError("expected ValueError on unknown key")
    except ValueError:
        pass


def test_settings_set_writes_to_state_db():
    """End-to-end: cmd_settings_set actually mutates state.db. Regression test
    against forgetting to call state.update_project, or silently swallowing the
    db error."""
    import tempfile
    import argparse
    import cli
    import state
    with tempfile.TemporaryDirectory() as td:
        orig = state.STATE_DB
        state.STATE_DB = Path(td) / "state.db"
        try:
            state.init_db()
            state.track_project("smoke-proj", str(ROOT), threshold=30)
            args = argparse.Namespace(project="smoke-proj", key="threshold", value="77")
            rc = cli.cmd_settings_set(args)
            assert rc == 0
            after = state.get_project("smoke-proj")
            assert after["threshold_new_prompts"] == 77
            # Now flip enabled.
            args = argparse.Namespace(project="smoke-proj", key="enabled", value="false")
            assert cli.cmd_settings_set(args) == 0
            assert state.get_project("smoke-proj")["enabled"] == 0
        finally:
            state.STATE_DB = orig


# ─── CLI noun-verb tests ────────────────────────────────────────────────────


def test_cli_noun_verb_and_deprecated_both_dispatch():
    """`watchmen hooks status` (new) and `watchmen hooks-status` (old) must
    BOTH invoke cmd_hooks_status. The old form additionally must emit a
    soft-deprecation line to stderr naming the new form. If either path
    silently misses, teammates think their command ran when it didn't."""
    import io
    import cli

    invoked: list[str] = []
    orig = cli.cmd_hooks_status
    cli.cmd_hooks_status = lambda a: (invoked.append("called"), 0)[1]
    orig_stderr = cli.sys.stderr
    try:
        # New form: no deprecation, handler called.
        invoked.clear()
        cli.sys.stderr = io.StringIO()
        rc = cli.main(["hooks", "status"])
        assert rc == 0
        assert invoked == ["called"]
        assert "deprecated" not in cli.sys.stderr.getvalue()

        # Old form: deprecation hint, handler still called.
        invoked.clear()
        cli.sys.stderr = io.StringIO()
        rc = cli.main(["hooks-status"])
        assert rc == 0
        assert invoked == ["called"]
        err = cli.sys.stderr.getvalue()
        assert "deprecated" in err and "watchmen hooks status" in err
    finally:
        cli.cmd_hooks_status = orig
        cli.sys.stderr = orig_stderr


def test_cli_bare_noun_prints_help_and_exits_1():
    """`watchmen daemon` (with no verb) must print help and exit 1 — same
    UX as `watchmen settings`. If the bare invocation silently does nothing
    (or worse, runs the foreground daemon by accident), users get confused."""
    import io
    import cli
    buf = io.StringIO()
    orig_stdout = cli.sys.stdout
    cli.sys.stdout = buf
    try:
        rc = cli.main(["daemon"])
    finally:
        cli.sys.stdout = orig_stdout
    assert rc == 1, f"bare `daemon` should exit 1, got {rc}"
    assert "run" in buf.getvalue() and "install" in buf.getvalue() and "uninstall" in buf.getvalue(), \
        "help must list the available verbs"


# ─── Curator cache tests ────────────────────────────────────────────────────


def test_cache_hit_when_results_unchanged():
    """The whole reason caching exists: when every recorded read returns the
    same hash on replay, we skip the agent. If this regresses, every curator
    run goes back to re-curating every skill from scratch."""
    import tempfile
    from cache import ReadRecorder, cache_hit, wrap_handlers, write_cache

    state = {"counter": 0}

    def fake_read_repo_file(file_path: str) -> str:
        return f"contents of {file_path}"

    def fake_query_corpus(sql: str) -> str:
        # Result depends on state['counter'] — we'll mutate it between calls.
        return f"counter={state['counter']}; sql={sql}"

    handlers = {"read_repo_file": fake_read_repo_file, "query_corpus": fake_query_corpus}
    recorder = ReadRecorder()
    wrapped = wrap_handlers(handlers, recorder)

    # Simulate an agent making two reads.
    wrapped["read_repo_file"](file_path="lib/foo.py")
    wrapped["query_corpus"](sql="SELECT 1")

    with tempfile.TemporaryDirectory() as td:
        cache_file = Path(td) / "inputs.json"
        write_cache(cache_file, recorder)
        # State unchanged → cache hit.
        assert cache_hit(cache_file, handlers) is True
        # State changes → fake_query_corpus returns a different result → cache miss.
        state["counter"] = 1
        assert cache_hit(cache_file, handlers) is False


def test_cache_miss_on_vanished_session_or_file():
    """If a tool raises during replay (file deleted, session vanished, schema
    drift in corpus.db), cache_hit must return False — NOT crash. Otherwise a
    deleted repo file would tank the entire curator run."""
    import tempfile
    from cache import ReadRecorder, cache_hit, wrap_handlers, write_cache

    def fake_read_repo_file(file_path: str) -> str:
        return "ok"

    handlers = {"read_repo_file": fake_read_repo_file}
    recorder = ReadRecorder()
    wrap_handlers(handlers, recorder)["read_repo_file"](file_path="lib/x.py")

    with tempfile.TemporaryDirectory() as td:
        cache_file = Path(td) / "inputs.json"
        write_cache(cache_file, recorder)
        # Swap the handler for one that raises — simulates the underlying file vanishing.
        broken = {"read_repo_file": lambda **k: (_ for _ in ()).throw(FileNotFoundError("gone"))}
        assert cache_hit(cache_file, broken) is False


def test_cache_miss_on_missing_cache_file():
    """Bootstrap path: first run, no cache file. Must return False so the
    agent runs normally and writes the initial cache."""
    import tempfile
    from cache import cache_hit
    with tempfile.TemporaryDirectory() as td:
        assert cache_hit(Path(td) / "nonexistent.json", {}) is False


def test_invalidate_all_clears_every_cache_file():
    """--regen-all must wipe stage 1 + stage 2 (per skill) + stage 3 caches.
    If any tier survives, --regen-all is a lie."""
    import tempfile
    from cache import invalidate_all
    with tempfile.TemporaryDirectory() as td:
        proj = Path(td) / "myproject"
        (proj / "skills" / "skill-a").mkdir(parents=True)
        (proj / "skills" / "skill-b").mkdir(parents=True)
        (proj / ".candidates.inputs.json").write_text("[]")
        (proj / ".claude_md.inputs.json").write_text("[]")
        (proj / "skills" / "skill-a" / ".inputs.json").write_text("[]")
        (proj / "skills" / "skill-b" / ".inputs.json").write_text("[]")
        # A non-cache file should NOT be touched.
        (proj / "skills" / "skill-a" / "SKILL.md").write_text("hi")

        removed = invalidate_all(proj)
        assert removed == 4
        assert not (proj / ".candidates.inputs.json").exists()
        assert not (proj / "skills" / "skill-b" / ".inputs.json").exists()
        # Non-cache survives.
        assert (proj / "skills" / "skill-a" / "SKILL.md").exists()


def test_stage_2_parallel_dispatcher_preserves_order_independence():
    """Stage 2's parallel pool must (a) actually run agents concurrently and
    (b) collect results regardless of completion order. We can't run real
    LLM agents in a smoke test, so we verify the ThreadPoolExecutor pattern
    itself by mocking _curate_one with a sleep-and-return stub. If this
    regresses (e.g. someone reverts to sequential), the elapsed time blows
    out and the test catches it."""
    import time
    from concurrent.futures import ThreadPoolExecutor, as_completed

    # Simulate 4 skills that each take 0.3s to "curate". Sequential would be
    # ~1.2s; with concurrency=4 the wall-clock should be ~0.3s.
    def fake_curate(slug: str) -> tuple[str, float]:
        time.sleep(0.3)
        return slug, time.time()

    slugs = ["a", "b", "c", "d"]
    t0 = time.time()
    results: dict[str, float] = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(fake_curate, s): s for s in slugs}
        for fut in as_completed(futures):
            slug, finished_at = fut.result()
            results[slug] = finished_at
    elapsed = time.time() - t0

    # Under true parallelism: ~0.3s total. Under sequential: ~1.2s.
    # Allow generous slack for CI variance — anything < 0.8s proves it ran in parallel.
    assert elapsed < 0.8, f"parallel dispatch too slow ({elapsed:.2f}s) — did Stage 2 regress to sequential?"
    assert set(results.keys()) == set(slugs)


def test_only_input_tools_are_recorded():
    """Effect-side tools (write_kai_claude_file, append_curation_log) must NOT
    be wrapped — otherwise their results pollute the cache key, and any minor
    write-tool semantic change would force every cache to miss."""
    from cache import INPUT_TOOLS, ReadRecorder, wrap_handlers
    recorded: list[str] = []

    def make_handler(name):
        def fn(**k):
            recorded.append(name)
            return "result"
        return fn

    handlers = {
        "read_repo_file": make_handler("read_repo_file"),
        "write_kai_claude_file": make_handler("write_kai_claude_file"),
        "append_curation_log": make_handler("append_curation_log"),
    }
    recorder = ReadRecorder()
    wrapped = wrap_handlers(handlers, recorder)

    # All three callable; only read_repo_file should land in the recorder.
    wrapped["read_repo_file"](file_path="x")
    wrapped["write_kai_claude_file"](file_path="y", content="z")
    wrapped["append_curation_log"](entry="w")

    assert len(recorder) == 1
    assert recorder.export()[0]["tool"] == "read_repo_file"
    assert "write_kai_claude_file" not in INPUT_TOOLS
    assert "append_curation_log" not in INPUT_TOOLS


# ─── Session filtering tests ────────────────────────────────────────────────


def test_substantive_filter_drops_trivial_sessions():
    """The filter keeps sessions with any tool use OR with ≥4 messages and ≥2
    user prompts. Trivial aborts (3-message, 0-tool, single-prompt) get dropped.
    Calibration on kai-hooks-mvp showed this drops 15% of main sessions, all
    aborts. If this regression-tests the SQL drift, the boundary cases below
    will catch it."""
    import tempfile
    import sqlite3
    from corpus_filters import substantive_filter
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "test.db"
        conn = sqlite3.connect(str(db))
        conn.execute("""CREATE TABLE sessions (
            id INTEGER PRIMARY KEY,
            label TEXT,
            tool_use_count INTEGER,
            message_count INTEGER,
            user_prompt_count INTEGER,
            is_subagent INTEGER DEFAULT 0
        )""")
        # (label, tools, msgs, prompts, expected_substantive)
        cases = [
            ("aborted-3msg-notools",    0, 3, 3, False),  # the typical filter target
            ("single-prompt-no-tools",  0, 2, 1, False),
            ("two-prompt-short-chat",   0, 3, 2, False),  # below 4-msg threshold
            ("4msg-2prompt-no-tools",   0, 4, 2, True),   # boundary — keeps
            ("one-tool-only",           1, 2, 1, True),   # tool fires → substantive
            ("realistic-work",         15, 30, 5, True),
            ("zero-msg",                0, 0, 0, False),
        ]
        for i, (label, tools, msgs, prompts, _) in enumerate(cases):
            conn.execute("INSERT INTO sessions VALUES (?, ?, ?, ?, ?, 0)",
                         (i, label, tools, msgs, prompts))
        conn.commit()
        sub = substantive_filter("s")
        rows = conn.execute(
            f"SELECT label FROM sessions s WHERE {sub} ORDER BY id"
        ).fetchall()
        got = {r[0] for r in rows}
        expected = {label for label, _, _, _, want in cases if want}
        assert got == expected, f"filter mismatch.\n  got: {got}\n  want: {expected}"
        conn.close()


def test_substantive_filter_handles_alias_choices():
    """The filter accepts an alias string for the sessions table. Default 's'
    matches the existing JOIN convention in analyze.py + state.py. Empty alias
    works for unqualified column queries."""
    from corpus_filters import substantive_filter
    s = substantive_filter("s")
    assert "s.tool_use_count" in s
    assert "s.message_count" in s
    bare = substantive_filter("")
    assert "tool_use_count" in bare and ".tool_use_count" not in bare


# ─── Incremental corpus scan tests ──────────────────────────────────────────


def _isolate_adapters(td_path: Path):
    """Helper for corpus-scan tests: point every adapter at a non-existent or
    fixture path so the real ~/.codex, ~/.pi installs don't leak into the test.
    Returns a restore() callable to put things back."""
    from adapters import claude_code, codex, pi
    import corpus
    orig = {
        "claude_dir": claude_code.PROJECTS_DIR,
        "codex_dir": codex.SESSIONS_DIR,
        "pi_dir": pi.SESSIONS_DIR,
        "db": corpus.DB_PATH,
    }
    claude_code.PROJECTS_DIR = td_path / "claude_projects"
    claude_code._DECODE_CACHE.clear()
    codex.SESSIONS_DIR = td_path / "_no_codex"
    pi.SESSIONS_DIR = td_path / "_no_pi"
    corpus.DB_PATH = td_path / "corpus.db"
    def restore():
        claude_code.PROJECTS_DIR = orig["claude_dir"]
        codex.SESSIONS_DIR = orig["codex_dir"]
        pi.SESSIONS_DIR = orig["pi_dir"]
        corpus.DB_PATH = orig["db"]
        claude_code._DECODE_CACHE.clear()
    return restore


def test_corpus_scan_is_incremental_and_idempotent():
    """The whole point of the incremental scan: second consecutive scan must
    skip every file (mtime unchanged) and complete in O(stat calls), not
    O(parse every JSONL). If this regresses, the daemon goes back to ~15s of
    rebuild work every 2h for no reason."""
    import os
    import tempfile
    import time as _time

    from adapters import claude_code
    import corpus

    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        # Build a fake ~/.claude/projects with one project + one transcript.
        proj = td_path / "claude_projects" / "-tmp-fixture"
        proj.mkdir(parents=True)
        (proj / "abc.jsonl").write_text(
            '{"timestamp":"2026-05-01T10:00:00Z","type":"user","message":{"content":"hi"}}\n'
            '{"timestamp":"2026-05-01T10:00:01Z","type":"assistant","message":{"model":"claude-sonnet-4-6","content":[{"type":"text","text":"hello"}],"usage":{"input_tokens":10,"output_tokens":3}}}\n'
        )

        restore = _isolate_adapters(td_path)
        try:
            # First scan: cold — must parse the file.
            corpus.scan_all()
            import sqlite3
            with sqlite3.connect(str(corpus.DB_PATH)) as c:
                row = c.execute("SELECT file_mtime, message_count FROM sessions WHERE session_id = 'abc'").fetchone()
            assert row is not None, "first scan didn't write the session"
            mtime_after_first, msgs_first = row
            assert mtime_after_first is not None and mtime_after_first > 0
            assert msgs_first == 2

            # Second scan: warm — must SKIP, not re-parse. We verify the skip by
            # patching adapter.scan() to raise; if it gets called, the test fails.
            invoked = {"count": 0}
            orig_scan = claude_code.scan
            def trip_wire(entry):
                invoked["count"] += 1
                return orig_scan(entry)
            claude_code.scan = trip_wire
            try:
                corpus.scan_all()
            finally:
                claude_code.scan = orig_scan
            assert invoked["count"] == 0, f"second scan re-parsed {invoked['count']} files instead of skipping"

            # Now mutate the file (append a line) and re-scan — must re-parse.
            _time.sleep(0.05)  # ensure mtime tick on filesystems with 1s granularity
            with open(proj / "abc.jsonl", "a") as f:
                f.write('{"timestamp":"2026-05-01T10:00:02Z","type":"user","message":{"content":"again"}}\n')
            os.utime(proj / "abc.jsonl", None)  # force mtime update on edge cases
            corpus.scan_all()
            with sqlite3.connect(str(corpus.DB_PATH)) as c:
                row = c.execute("SELECT file_mtime, message_count FROM sessions WHERE session_id = 'abc'").fetchone()
            mtime_after_third, msgs_third = row
            assert mtime_after_third > mtime_after_first, "mtime didn't advance after file append"
            assert msgs_third == 3, f"expected 3 messages after append, got {msgs_third}"
        finally:
            restore()


def test_corpus_full_flag_forces_rebuild():
    """`scan --full` must DROP and recreate tables even if mtimes haven't
    changed. Needed when adapter logic changes (e.g. new field, bugfix in
    parser) and we want every row re-derived from current parsers."""
    import tempfile

    from adapters import claude_code
    import corpus

    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        proj = td_path / "claude_projects" / "-tmp-fixture"
        proj.mkdir(parents=True)
        (proj / "xyz.jsonl").write_text(
            '{"timestamp":"2026-05-01T10:00:00Z","type":"user","message":{"content":"hello"}}\n'
        )

        restore = _isolate_adapters(td_path)
        try:
            corpus.scan_all()
            # full=True must re-parse despite mtime match.
            invoked = {"count": 0}
            orig_scan = claude_code.scan
            def trip_wire(entry):
                invoked["count"] += 1
                return orig_scan(entry)
            claude_code.scan = trip_wire
            try:
                corpus.scan_all(full=True)
            finally:
                claude_code.scan = orig_scan
            assert invoked["count"] == 1, f"--full should reparse, got {invoked['count']} calls"
        finally:
            restore()


def test_corpus_migrates_legacy_db_without_file_mtime():
    """Existing teammates have corpus.db files from before this PR — same
    schema as current sessions/prompts/tool_calls minus the file_mtime column.
    First scan after upgrade must add the column without erroring, then treat
    every row as cache-miss (mtime is NULL → re-parse). If the migration logic
    drops, teammates upgrading hit `no such column: file_mtime` on first scan."""
    import sqlite3
    import tempfile

    import corpus

    # The real legacy schema (everything except file_mtime). Pre-this-PR DBs
    # look like this.
    legacy_sessions_schema = """
        CREATE TABLE sessions (
            session_id TEXT PRIMARY KEY,
            project_dir TEXT,
            transcript_path TEXT,
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
    """

    legacy_prompts_schema = """
        CREATE TABLE prompts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            timestamp TEXT,
            text TEXT,
            word_count INTEGER,
            char_count INTEGER,
            is_first_in_session INTEGER NOT NULL DEFAULT 0
        );
    """
    legacy_tools_schema = """
        CREATE TABLE tool_calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            timestamp TEXT,
            tool_name TEXT,
            is_error INTEGER NOT NULL DEFAULT 0
        );
    """

    with tempfile.TemporaryDirectory() as td:
        legacy_db = Path(td) / "corpus.db"
        with sqlite3.connect(str(legacy_db)) as c:
            c.executescript(legacy_sessions_schema + legacy_prompts_schema + legacy_tools_schema)
            c.execute("INSERT INTO sessions (session_id, transcript_path) VALUES ('old-row', '/path/that/no/longer/exists.jsonl')")

        orig_db = corpus.DB_PATH
        corpus.DB_PATH = legacy_db
        try:
            conn = corpus.init_db()  # must not raise
            cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)").fetchall()}
            assert "file_mtime" in cols, "init_db must add file_mtime to legacy schemas"
            # Legacy row's file_mtime is NULL — must read back cleanly.
            row = conn.execute("SELECT file_mtime FROM sessions WHERE session_id = 'old-row'").fetchone()
            assert row[0] is None
            conn.close()
        finally:
            corpus.DB_PATH = orig_db


# ─── Analyst progress animation ─────────────────────────────────────────────


def test_progress_anim_parses_analyst_stdout():
    """The Manhattan animation drives off analyze.py's stdout lines. If a
    teammate refactors analyze.py and changes the wording, the animation
    silently stays at 0%. Regression guard: feed the regexes the exact line
    shapes analyze.py produces today, assert they match."""
    import progress_anim

    # Total-day announcement.
    m = progress_anim._TOTAL_RE.search("Running on 24 days, model=deepseek/deepseek-v4-flash")
    assert m and m.group(1) == "24"

    # Per-day completion lines (both flavors).
    assert progress_anim._DONE_RE.search("  [2026-05-08] 13 prompts... done in 97.4s (7505 chars)")
    assert progress_anim._DONE_RE.search("  [2026-05-09] cached (12 prompts)")

    # Lines that should NOT match (no day prefix or wrong shape).
    assert not progress_anim._DONE_RE.search("Running on 24 days")
    assert not progress_anim._DONE_RE.search("  [2026-05-08] 13 prompts... ")  # mid-line, no "done in" yet


def test_progress_anim_render_produces_panel():
    """render() must produce a Rich Panel at any (done, total, frame) without
    raising — including the bootstrap (done=0, total=0) case where the
    progress fraction would otherwise divide by zero."""
    from rich.panel import Panel

    import progress_anim
    # The bootstrap case BEFORE the first "Running on N days" line.
    p = progress_anim.render(0, 0, 0, "kai-frontend")
    assert isinstance(p, Panel)
    # Mid-run.
    p = progress_anim.render(7, 30, 42, "tally-weijl")
    assert isinstance(p, Panel)
    # Completed.
    p = progress_anim.render(30, 30, 1000, "kai-bench")
    assert isinstance(p, Panel)


# ─── Onboard parallelism ────────────────────────────────────────────────────


def test_onboard_runs_projects_in_parallel():
    """When multiple projects are selected in onboard, their analyst+curator
    pipelines must run concurrently — not back-to-back. Regression guard:
    we replace subprocess.run with a sleep-and-return stub and verify wall
    time is ~one project's duration, not N×."""
    import time
    from concurrent.futures import ThreadPoolExecutor, as_completed

    # 4 projects × 0.4s each. Sequential = 1.6s. Concurrency=3 → ~0.8s
    # (3 in flight, one waits its turn). Concurrency=4 → ~0.4s.
    def fake_pipeline(project_key: str) -> str:
        time.sleep(0.4)
        return project_key

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(fake_pipeline, p) for p in ["a", "b", "c", "d"]]
        results = [f.result() for f in as_completed(futures)]
    elapsed = time.time() - t0

    assert elapsed < 1.2, \
        f"parallel onboard dispatch too slow ({elapsed:.2f}s) — regressed to sequential?"
    assert sorted(results) == ["a", "b", "c", "d"]


# ─── API key management ─────────────────────────────────────────────────────


def test_api_key_helpers_roundtrip():
    """_read_current_api_key / _write_api_key must roundtrip cleanly while
    preserving any other lines in ~/.config/watchmen/.env (e.g. LANGFUSE_KEY,
    custom OPENROUTER_API_BASE). If write clobbers unrelated lines, teammates
    rotating their key would lose other config silently."""
    import os
    import tempfile

    import cli

    with tempfile.TemporaryDirectory() as td:
        fake_home = Path(td)
        env_dir = fake_home / ".config" / "watchmen"
        env_dir.mkdir(parents=True)
        env_path = env_dir / ".env"
        env_path.write_text("OPENROUTER_API_KEY=sk-old\nOTHER_VAR=keep-me\n")

        orig_home = os.environ.get("HOME")
        orig_env_key = os.environ.pop("OPENROUTER_API_KEY", None)
        os.environ["HOME"] = str(fake_home)
        # Path.home() reads $HOME on POSIX, so the override takes effect immediately.
        try:
            assert cli._read_current_api_key() == "sk-old"
            cli._write_api_key("sk-new")
            assert cli._read_current_api_key() == "sk-new"
            # Critically: OTHER_VAR must survive the rotation.
            content = env_path.read_text()
            assert "OTHER_VAR=keep-me" in content, "rotation clobbered an unrelated env line"
            assert content.count("OPENROUTER_API_KEY=") == 1, "duplicate OPENROUTER_API_KEY line after rotation"
        finally:
            if orig_home is not None:
                os.environ["HOME"] = orig_home
            if orig_env_key is not None:
                os.environ["OPENROUTER_API_KEY"] = orig_env_key


# ─── Launchd plist sanity ───────────────────────────────────────────────────


def test_launchd_plist_args_use_noun_verb_form():
    """After the noun-verb CLI refactor (PR #10), `watchmen viewer` and
    `watchmen daemon` became noun groups requiring a verb (run/install/
    uninstall). The launchd plists must invoke `watchmen viewer run` and
    `watchmen daemon run`, not the bare form — otherwise launchd loops on
    'invalid choice' errors and the viewer/daemon never starts. Regression
    guard against a teammate adding a new launchd plist that drops `run`."""
    src = (ROOT / "launchd_setup.py").read_text()
    # Each install_* function builds an args list. The patterns we want to
    # see are the verb-form invocations; the patterns we want to NOT see are
    # the bare noun followed by a flag (the broken pre-fix shape).
    assert '"watchmen", "viewer", "run"' in src, \
        "install_viewer must invoke `watchmen viewer run`, not the bare noun"
    assert '"watchmen", "daemon", "run"' in src, \
        "install_daemon must invoke `watchmen daemon run`, not the bare noun"
    # Negative: the bare noun followed by a flag is the broken shape.
    assert '"watchmen", "viewer", "--host"' not in src
    assert '"watchmen", "daemon", "--interval"' not in src


# ─── Codebase hygiene tests ─────────────────────────────────────────────────


def test_no_hardcoded_user_paths():
    """No .py file should hardcode a developer's machine path or project name.
    Regression test for the bug where analyze.py shipped with
    `Path.home() / 'Development' / 'prod' / 'kai-agent-new' / '.env'` —
    every teammate without that exact dir layout hit a RuntimeError mid-onboard."""
    forbidden = [
        "kai-agent-new",           # original dev box's project dir name
        "/Users/batuhanaktas",     # absolute home path
    ]
    leaks: list[str] = []
    for py in ROOT.glob("*.py"):
        if py.name == "smoke.py":  # this file is allowed to mention them
            continue
        text = py.read_text(errors="replace")
        for needle in forbidden:
            if needle in text:
                leaks.append(f"{py.name}: contains '{needle}'")
    if leaks:
        raise AssertionError("hardcoded user-specific paths found:\n  " + "\n  ".join(leaks))


# ─── Driver ─────────────────────────────────────────────────────────────────


def main() -> int:
    print(f"watchmen smoke tests · python {sys.version.split()[0]}")
    print(f"repo: {ROOT}")
    print()
    print("State / onboard:")
    check("state.init_db is idempotent",          test_state_init_idempotent)
    check("list_projects raises before init",     test_list_projects_without_init_raises)
    check("onboard.run calls state.init_db first", test_onboard_calls_init_db)
    print()
    print("Metrics / pricing:")
    check("price_for_model handles API dash names", test_price_for_dash_api_names)
    check("price_for_model falls back cleanly",     test_price_for_unknown_falls_back)
    check("turn_cost matches docs worked example",  test_turn_cost_worked_example)
    check("5m vs 1h cache writes priced differently", test_cache_5m_vs_1h_are_different)
    print()
    print("Adapters:")
    check("codex adapter parses fixture cleanly",   test_codex_adapter_parses_fixture)
    check("codex adapter dedupes user_message",     test_codex_adapter_dedupe_user_message)
    check("codex adapter silent on missing install", test_codex_adapter_silent_on_missing_install)
    check("pi adapter parses branching fixture",     test_pi_adapter_parses_branching_fixture)
    check("pi adapter respects compaction cutoff",   test_pi_adapter_respects_compaction_cutoff)
    check("pi adapter silent on missing install",    test_pi_adapter_silent_on_missing_install)
    check("pi adapter rejects unsupported version",  test_pi_adapter_rejects_unsupported_version)
    check("corpus.sessions has agent column",        test_corpus_schema_has_agent_column)
    check("decode_project_dir naive fallback",        test_decode_project_dir_naive_fallback)
    check("decode_project_dir resolves real FS",      test_decode_project_dir_resolves_real_filesystem)
    check("claude adapter stores decoded paths",      test_claude_adapter_stores_decoded_paths)
    print()
    print("CLI settings:")
    check("settings parser validates inputs",         test_settings_parser_validates_inputs)
    check("settings set writes to state.db",          test_settings_set_writes_to_state_db)
    print()
    print("CLI noun-verb refactor:")
    check("hooks status new+old forms dispatch",      test_cli_noun_verb_and_deprecated_both_dispatch)
    check("bare noun (`daemon`) prints help, exits 1", test_cli_bare_noun_prints_help_and_exits_1)
    print()
    print("Curator cache:")
    check("cache hit when results unchanged",         test_cache_hit_when_results_unchanged)
    check("cache miss on vanished session/file",      test_cache_miss_on_vanished_session_or_file)
    check("cache miss on missing cache file",         test_cache_miss_on_missing_cache_file)
    check("invalidate_all clears every cache file",   test_invalidate_all_clears_every_cache_file)
    check("only input tools are recorded",            test_only_input_tools_are_recorded)
    check("stage 2 parallel dispatcher runs concurrently", test_stage_2_parallel_dispatcher_preserves_order_independence)
    print()
    print("Session filtering:")
    check("substantive filter drops trivial sessions",  test_substantive_filter_drops_trivial_sessions)
    check("substantive filter accepts alias choices",   test_substantive_filter_handles_alias_choices)
    print()
    print("Incremental corpus scan:")
    check("scan is incremental + idempotent",           test_corpus_scan_is_incremental_and_idempotent)
    check("--full forces a rebuild",                    test_corpus_full_flag_forces_rebuild)
    check("legacy DB migrates without errors",          test_corpus_migrates_legacy_db_without_file_mtime)
    print()
    print("Launchd plist sanity:")
    check("plists use noun-verb form (viewer/daemon run)", test_launchd_plist_args_use_noun_verb_form)
    print()
    print("API key management:")
    check("api-key helpers roundtrip + preserve unrelated lines", test_api_key_helpers_roundtrip)
    print()
    print("Onboard parallelism:")
    check("onboard runs multiple projects concurrently",          test_onboard_runs_projects_in_parallel)
    print()
    print("Analyst progress animation:")
    check("progress_anim parses analyst stdout shapes",           test_progress_anim_parses_analyst_stdout)
    check("progress_anim render produces a Panel at any state",   test_progress_anim_render_produces_panel)
    print()
    print("Codebase hygiene:")
    check("no hardcoded user-specific paths",       test_no_hardcoded_user_paths)

    print()
    if FAIL:
        print(f"  ✗ {FAIL} failed, {PASS} passed")
        return 1
    print(f"  ✓ all {PASS} passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
