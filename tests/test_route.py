"""Tests for watchmen route."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path


# ─── Helpers ─────────────────────────────────────────────────────────


def _now_iso(offset_days: int = 0) -> str:
    return (
        datetime.now(timezone.utc).replace(microsecond=0)
        - timedelta(days=offset_days)
    ).isoformat().replace("+00:00", "Z")


def _seed_sessions(db_path: Path, project_dir: str, rows: list[dict]) -> None:
    """Minimal sessions table for detection tests."""
    db = sqlite3.connect(db_path)
    db.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT PRIMARY KEY,
            project_dir TEXT,
            started_at TEXT,
            is_subagent INTEGER NOT NULL DEFAULT 0,
            model_dominant TEXT,
            agent TEXT NOT NULL DEFAULT 'claude_code'
        )
    """)
    for i, row in enumerate(rows):
        db.execute(
            "INSERT INTO sessions(session_id, project_dir, started_at, "
            "is_subagent, model_dominant, agent) VALUES (?, ?, ?, ?, ?, ?)",
            (
                row.get("session_id", f"s{i}"),
                row.get("project_dir", project_dir),
                row.get("started_at", _now_iso(0)),
                int(row.get("is_subagent", 0)),
                row.get("model_dominant"),
                row.get("agent", "claude_code"),
            ),
        )
    db.commit()
    db.close()


def _setup_project(tmp_path: Path, monkeypatch, project_key: str, source_repo: str) -> Path:
    """Wire watchmen's corpus + state lookups into tmp_path.

    route.py and route_rewrite.py captured corpus_db_path, project_dir_predicate,
    bundle_dir, and get_project at import time via ``from ... import``, so the
    monkeypatch has to target those module-local names directly — patching
    ``watchmen.util`` is too late by then.
    """
    from watchmen import route as wm_route
    from watchmen import route_rewrite as wm_rewrite

    bundles_root = tmp_path / "bundles"
    bundles_root.mkdir()
    corpus_db = tmp_path / "corpus.db"

    monkeypatch.setattr(wm_route, "corpus_db_path", lambda: corpus_db)
    monkeypatch.setattr(
        wm_route, "project_dir_predicate",
        lambda key, alias="s": (f"({alias}.project_dir = ?)", (source_repo,)),
    )
    monkeypatch.setattr(wm_route, "bundle_dir", lambda key: bundles_root / key)
    monkeypatch.setattr(wm_rewrite, "bundle_dir", lambda key: bundles_root / key)
    monkeypatch.setattr(
        wm_rewrite, "_resolve_repo_root", lambda key: source_repo
    )
    return corpus_db


# ─── Detection ───────────────────────────────────────────────────────


def test_detect_harnesses_uses_modal_model_per_agent(tmp_path, monkeypatch):
    """Reference detection is modal-by-session-count (winning model in
    this project's session pool), not most-recent — most-recent is too
    fragile to single-hour smoke noise.  Subagent (is_subagent=1)
    sessions count too, since Task / thread_spawn delegations reflect
    real harness activity the user spawned and depends on."""
    from watchmen import route

    repo = "/tmp/proj-a"
    db = _setup_project(tmp_path, monkeypatch, "proj-a", repo)
    _seed_sessions(db, repo, [
        # codex: 1 old gpt-5-mini, 3 recent gpt-5.5 → gpt-5.5 wins by count
        {"agent": "codex", "model_dominant": "openai/gpt-5-mini", "started_at": _now_iso(7)},
        {"agent": "codex", "model_dominant": "openai/gpt-5.5", "started_at": _now_iso(2)},
        {"agent": "codex", "model_dominant": "openai/gpt-5.5", "started_at": _now_iso(1)},
        {"agent": "codex", "model_dominant": "openai/gpt-5.5", "started_at": _now_iso(0)},
        # claude_code: modal opus-4-7 should win (3 sessions including
        # subagents) over a single-hour spike of newer sonnet sessions.
        # This mirrors the real-world coder-pro corpus pattern.
        {"agent": "claude_code", "model_dominant": "anthropic/claude-opus-4-7",
         "started_at": _now_iso(10)},
        {"agent": "claude_code", "model_dominant": "anthropic/claude-opus-4-7",
         "started_at": _now_iso(8), "is_subagent": 1},
        {"agent": "claude_code", "model_dominant": "anthropic/claude-opus-4-7",
         "started_at": _now_iso(5)},
        # Two ad-hoc sonnet sessions yesterday — most-recent but not modal
        {"agent": "claude_code", "model_dominant": "anthropic/claude-sonnet-4-6",
         "started_at": _now_iso(1)},
        {"agent": "claude_code", "model_dominant": "anthropic/claude-sonnet-4-6",
         "started_at": _now_iso(0)},
        # outside lookback window — filtered
        {"agent": "pi", "model_dominant": "z-ai/glm-4.5",
         "started_at": _now_iso(120)},
    ])

    refs = route.detect_harnesses("proj-a", since_days=30)
    by_harness = {r.harness: r for r in refs}

    assert set(by_harness) == {"codex", "claude_code"}
    # Modal — winning model's session count is reported, not total
    assert by_harness["codex"].current_model == "openai/gpt-5.5"
    assert by_harness["codex"].session_count_window == 3
    assert by_harness["claude_code"].current_model == "anthropic/claude-opus-4-7"
    assert by_harness["claude_code"].session_count_window == 3


def test_detect_harnesses_skips_unsupported(tmp_path, monkeypatch):
    from watchmen import route

    repo = "/tmp/proj-b"
    db = _setup_project(tmp_path, monkeypatch, "proj-b", repo)
    _seed_sessions(db, repo, [
        {"agent": "claude_code", "model_dominant": "anthropic/claude-opus-4-7",
         "started_at": _now_iso(1)},
        {"agent": "some-future-harness", "model_dominant": "vendor/m",
         "started_at": _now_iso(1)},
    ])
    refs = route.detect_harnesses("proj-b", since_days=30)
    assert {r.harness for r in refs} == {"claude_code"}


# ─── Provider family + candidate curation ────────────────────────────


def test_provider_family_extraction():
    from watchmen.route import provider_family_for_model

    assert provider_family_for_model("anthropic/claude-opus-4-7") == "anthropic"
    assert provider_family_for_model("openai/gpt-5.5") == "openai"
    # Bare ids fall back to harness default
    assert provider_family_for_model("gpt-5.5", harness="codex") == "openai"
    assert provider_family_for_model("opus-4-7", harness="claude_code") == "anthropic"
    # Heuristic recovery on truly bare ids without harness hint
    assert provider_family_for_model("claude-haiku") == "anthropic"
    assert provider_family_for_model("gpt-4-turbo") == "openai"
    # Unknown family surfaces as unknown rather than guessing
    assert provider_family_for_model("z-ai/glm-4.5") == "z-ai"
    assert provider_family_for_model("mystery-model") == "unknown"


def test_normalize_model_id_namespaces_bare_codex_and_cc_models():
    """Codex writes `gpt-5.5` bare; CC writes `claude-opus-4-7` bare; both
    should be namespaced when the harness implies a default family, so
    reference vs candidate comparison doesn't trip on namespace mismatch."""
    from watchmen.route import _normalize_model_id

    assert _normalize_model_id("gpt-5.5", harness="codex") == "openai/gpt-5.5"
    assert _normalize_model_id("claude-opus-4-7", harness="claude_code") == "anthropic/claude-opus-4-7"
    # Already-namespaced ids are passthrough
    assert _normalize_model_id("openai/gpt-5.5", harness="codex") == "openai/gpt-5.5"
    # No harness hint, bare id → leave alone (don't guess)
    assert _normalize_model_id("mystery", harness=None) == "mystery"


def test_detect_harnesses_namespaces_bare_codex_model(tmp_path, monkeypatch):
    """Regression: codex sessions write `model_dominant=gpt-5.5` bare.
    Without normalization, candidates_for_harness keeps `openai/gpt-5.5`
    in the pool and route ends up recommending the user's current model."""
    from watchmen import route

    repo = "/tmp/proj-c"
    db = _setup_project(tmp_path, monkeypatch, "proj-c", repo)
    _seed_sessions(db, repo, [
        {"agent": "codex", "model_dominant": "gpt-5.5", "started_at": _now_iso(0)},
    ])
    refs = route.detect_harnesses("proj-c", since_days=30)
    assert refs[0].current_model == "openai/gpt-5.5"


def test_candidates_for_harness_discovers_from_corpus_with_bare_ids(tmp_path, monkeypatch):
    """Bare model ids stay bare (no namespace prefix added).  OAuth
    subs — which is what watchmen routes through by default for CC and
    codex — expect the bare format the user's corpus already records."""
    from watchmen import route

    repo = "/tmp/proj-d"
    db = _setup_project(tmp_path, monkeypatch, "proj-d", repo)
    _seed_sessions(db, repo, [
        {"agent": "claude_code", "model_dominant": "claude-sonnet-4-20250514",
         "started_at": _now_iso(2)},
        {"agent": "claude_code", "model_dominant": "claude-opus-4-7",
         "started_at": _now_iso(0)},
        {"agent": "claude_code", "model_dominant": "claude-haiku-4-5-20251001",
         "started_at": _now_iso(5)},
        {"agent": "claude_code", "model_dominant": "<synthetic>",
         "started_at": _now_iso(0)},
        {"agent": "codex", "model_dominant": "gpt-5.5",
         "started_at": _now_iso(0)},
    ])

    pool = route.candidates_for_harness(
        "claude_code",
        "claude-opus-4-7",  # bare reference (matches OAuth-sub format)
        project_key="proj-d",
        user_candidates=["claude-3-5-sonnet-20241022"],
        since_days=30,
    )

    assert "claude-opus-4-7" not in pool  # reference excluded
    assert pool[0] == "claude-sonnet-4-20250514"  # most recent corpus model first
    assert "claude-haiku-4-5-20251001" in pool
    assert "claude-3-5-sonnet-20241022" in pool  # explicit user-supplied candidate
    assert all("<synthetic>" not in m for m in pool)
    assert "gpt-5.5" not in pool  # codex models don't leak into CC pool


def test_candidates_for_harness_excludes_current_in_both_bare_and_namespaced_forms(
    tmp_path, monkeypatch,
):
    """Normalization regression: when the user's reference is namespaced
    (anthropic/claude-opus-4-7) but the corpus stored the bare form
    (claude-opus-4-7), we should still recognize them as the same model
    and not duplicate it in the candidate pool."""
    from watchmen import route

    repo = "/tmp/proj-norm"
    db = _setup_project(tmp_path, monkeypatch, "proj-norm", repo)
    _seed_sessions(db, repo, [
        {"agent": "claude_code", "model_dominant": "claude-opus-4-7",
         "started_at": _now_iso(0)},
        {"agent": "claude_code", "model_dominant": "claude-sonnet-4-5",
         "started_at": _now_iso(1)},
    ])
    pool = route.candidates_for_harness(
        "claude_code",
        "anthropic/claude-opus-4-7",  # namespaced reference
        project_key="proj-norm", since_days=30,
    )
    assert "claude-opus-4-7" not in pool  # bare form of ref excluded too
    assert "claude-sonnet-4-5" in pool


def test_native_provider_for_harness_returns_oauth_subs_for_cc_and_codex():
    """CC and codex are pinned to their OAuth subs in route's default
    routing — flat-rate generation cost, matches the harness's actual
    runtime behaviour."""
    from watchmen.route import native_provider_for_harness

    assert native_provider_for_harness("claude_code") == "claude-pro"
    assert native_provider_for_harness("codex") == "chatgpt"


def test_native_provider_for_multi_provider_harness_falls_back_to_active_provider(
    monkeypatch,
):
    """opencode and pi.dev don't have a single provider — fall back to
    watchmen's globally configured provider (config.active_provider)
    so routing matches the user's overall watchmen setup."""
    from watchmen import config, route

    monkeypatch.setattr(config, "active_provider", lambda: "openrouter")
    assert route.native_provider_for_harness("opencode") == "openrouter"
    assert route.native_provider_for_harness("pi") == "openrouter"


# ─── Decision classification ─────────────────────────────────────────


def _make_compare_result(
    ref_model: str, ref_score: float, candidates: list[dict],
):
    """Tiny stand-in for compare.run_compare's CompareResult — only the
    bits route.classify_route reads."""
    from watchmen.compare import (
        CompareConfig,
        CompareResult,
        ModelSummary,
    )

    summaries = [
        ModelSummary(
            model=ref_model,
            role="reference",
            avg_score=ref_score,
            worst_score=ref_score,
            wins_vs_reference=None,
            task_count=3,
            cost_usd=1.0,
            cost_vs_reference=None,
            produced_tokens=1000,
            produced_tokens_vs_reference=None,
            visible_chars=1000,
            empty_outputs=0,
            maxed_outputs=0,
            sample_count=3,
            latency_s=100.0,
            latency_vs_reference=None,
            decision="reference",
            decision_note="baseline",
        ),
    ]
    for c in candidates:
        summaries.append(
            ModelSummary(
                model=c["model"],
                role="candidate",
                avg_score=c["avg"],
                worst_score=c.get("worst", c["avg"]),
                wins_vs_reference=c.get("wins", 2),
                task_count=3,
                cost_usd=c.get("cost", 0.5),
                cost_vs_reference=c.get("cost_ratio"),
                produced_tokens=1000,
                produced_tokens_vs_reference=1.0,
                visible_chars=1000,
                empty_outputs=c.get("empty", 0),
                maxed_outputs=c.get("maxed", 0),
                sample_count=9,
                latency_s=100.0,
                latency_vs_reference=1.0,
                decision=c.get("decision", "pending"),
                decision_note=c.get("decision_note", ""),
            )
        )
    return CompareResult(
        run_id="test",
        run_dir="/tmp/test",
        config=CompareConfig(
            project_key="p", bucket="b", reference_model=ref_model,
            candidates=[c["model"] for c in candidates],
        ),
        tasks=[],
        generations=[],
        scores=[],
        task_results=[],
        summaries=summaries,
    )


def test_run_route_routes_cross_harness_candidate_to_its_own_backend(tmp_path, monkeypatch):
    """In a cross-harness run, the injected foreign model must be tagged with
    the backend that serves it (codex's gpt-5.5 -> chatgpt), not the source
    harness's provider. Own candidates carry no override (they use the run's
    provider)."""
    from watchmen import route as wm_route
    from watchmen.route import HarnessReference, RouteConfig, run_route

    _setup_project(tmp_path, monkeypatch, "p", str(tmp_path / "repo"))

    refs = [
        HarnessReference("claude_code", "anthropic/claude-opus-4-7",
                         _now_iso(0), 10),
        HarnessReference("codex", "openai/gpt-5.5", _now_iso(0), 5),
    ]
    monkeypatch.setattr(wm_route, "detect_harnesses", lambda *a, **k: refs)
    monkeypatch.setattr(wm_route, "load_skill_bucket_evidence", lambda *a, **k: None)
    # Own pools: one same-family candidate each, no foreign ids.
    own = {
        "claude_code": ["claude-sonnet-4-6"],
        "codex": ["gpt-5.4-mini"],
    }
    monkeypatch.setattr(
        wm_route, "candidates_for_harness",
        lambda harness, current, **k: list(own[harness]),
    )

    captured: dict[str, object] = {}

    def fake_run_compare(cfg, *, run_id, progress=None):
        captured[cfg.provider] = cfg
        return _make_compare_result(cfg.reference_model, 0.8, candidates=[])

    monkeypatch.setattr(wm_route, "run_compare", fake_run_compare)

    run_route(RouteConfig(project_key="p", bucket="b", cross_harness=True))

    cc_cfg = captured["claude-pro"]
    # gpt-5.5 (codex's model) injected into CC's pool, tagged to chatgpt;
    # the bare form is what compare will run, so that's the map key.
    assert cc_cfg.candidate_providers == {"gpt-5.5": "chatgpt"}
    assert "gpt-5.5" in cc_cfg.candidates
    assert "claude-sonnet-4-6" in cc_cfg.candidates

    codex_cfg = captured["chatgpt"]
    # claude-opus injected into codex's pool, tagged to claude-pro.
    assert codex_cfg.candidate_providers == {"claude-opus-4-7": "claude-pro"}


def test_classify_downshift_when_winner_is_cheaper_and_better():
    from watchmen.route import HarnessReference, classify_route

    ref = HarnessReference(
        harness="claude_code",
        current_model="anthropic/claude-opus-4-7",
        last_session_ts=_now_iso(0),
        session_count_window=10,
    )
    result = _make_compare_result(
        "anthropic/claude-opus-4-7", ref_score=0.85,
        candidates=[
            {"model": "anthropic/claude-sonnet-4-6", "avg": 0.92,
             "cost_ratio": 0.20, "wins": 3},
        ],
    )
    d = classify_route(ref, result, [ref])
    assert d.label == "downshift"
    assert d.recommended_model == "anthropic/claude-sonnet-4-6"
    assert d.cost_vs_current == 0.20


def test_classify_upshift_when_quality_wins_but_pricier():
    from watchmen.route import HarnessReference, classify_route

    ref = HarnessReference(
        harness="claude_code", current_model="anthropic/claude-haiku-4-5",
        last_session_ts=_now_iso(0), session_count_window=5,
    )
    result = _make_compare_result(
        "anthropic/claude-haiku-4-5", ref_score=0.70,
        candidates=[
            {"model": "anthropic/claude-opus-4-7", "avg": 0.90, "cost_ratio": 3.0},
        ],
    )
    d = classify_route(ref, result, [ref])
    assert d.label == "upshift"
    assert d.recommended_model == "anthropic/claude-opus-4-7"


def test_classify_stay_when_winner_only_marginally_better():
    from watchmen.route import HarnessReference, classify_route

    ref = HarnessReference(
        harness="codex", current_model="openai/gpt-5.5",
        last_session_ts=_now_iso(0), session_count_window=8,
    )
    # Candidate is 0.01 better and not cheaper — under our +0.02 threshold.
    result = _make_compare_result(
        "openai/gpt-5.5", ref_score=0.88,
        candidates=[
            {"model": "openai/gpt-5-mini", "avg": 0.89, "cost_ratio": 0.98},
        ],
    )
    d = classify_route(ref, result, [ref])
    assert d.label == "stay"
    assert d.recommended_model is None


def test_classify_inherits_compare_quality_guards():
    from watchmen.route import HarnessReference, classify_route

    ref = HarnessReference(
        harness="codex", current_model="openai/gpt-5.5",
        last_session_ts=_now_iso(0), session_count_window=8,
    )
    result = _make_compare_result(
        "openai/gpt-5.5", ref_score=0.85,
        candidates=[
            {"model": "openai/gpt-5-mini", "avg": 0.0,
             "decision": "invalid output", "decision_note": "all 9 samples empty",
             "empty": 9},
        ],
    )
    d = classify_route(ref, result, [ref])
    # No healthy candidate ⇒ bubble up the quality-guard label, not a downshift.
    assert d.label == "invalid"
    assert d.recommended_model is None


def test_classify_switch_harness_when_other_harness_model_wins():
    from watchmen.route import HarnessReference, classify_route

    ref_cc = HarnessReference(
        harness="claude_code", current_model="anthropic/claude-opus-4-7",
        last_session_ts=_now_iso(0), session_count_window=10,
    )
    ref_codex = HarnessReference(
        harness="codex", current_model="openai/gpt-5.5",
        last_session_ts=_now_iso(0), session_count_window=5,
    )
    result = _make_compare_result(
        "anthropic/claude-opus-4-7", ref_score=0.82,
        candidates=[
            # Codex's current model lands in CC's pool via --cross-harness
            {"model": "openai/gpt-5.5", "avg": 0.93, "cost_ratio": 0.50},
        ],
    )
    d = classify_route(ref_cc, result, [ref_cc, ref_codex])
    assert d.label == "switch-harness"
    assert d.recommended_model == "openai/gpt-5.5"


def test_classify_switch_harness_matches_across_namespace_forms():
    """Real runs hit a form mismatch: references are namespaced
    (openai/gpt-5.5) but compare returns the winner bare (gpt-5.5) after
    per-provider normalization. switch-harness must still fire, and name the
    harness that runs it, or the whole cross-harness path is dead."""
    from watchmen.route import HarnessReference, classify_route

    ref_cc = HarnessReference(
        harness="claude_code", current_model="anthropic/claude-opus-4-7",
        last_session_ts=_now_iso(0), session_count_window=10,
    )
    ref_codex = HarnessReference(
        harness="codex", current_model="openai/gpt-5.5",   # namespaced
        last_session_ts=_now_iso(0), session_count_window=5,
    )
    result = _make_compare_result(
        "anthropic/claude-opus-4-7", ref_score=0.82,
        candidates=[
            {"model": "gpt-5.5", "avg": 0.93, "cost_ratio": 0.50},  # bare winner
        ],
    )
    d = classify_route(ref_cc, result, [ref_cc, ref_codex])
    assert d.label == "switch-harness"
    assert d.recommended_model == "gpt-5.5"
    assert d.recommended_harness == "codex"


def test_same_model_id_ignores_namespace():
    from watchmen.route import _same_model_id

    assert _same_model_id("openai/gpt-5.5", "gpt-5.5")
    assert _same_model_id("gpt-5.5", "openai/gpt-5.5")
    assert _same_model_id("anthropic/claude-opus-4-7", "anthropic/claude-opus-4-7")
    assert not _same_model_id("openai/gpt-5.5", "openai/gpt-5-mini")
    assert not _same_model_id("", "gpt-5.5")


# ─── Rewriters ───────────────────────────────────────────────────────


def _decision(harness: str, current: str, recommended: str, label="downshift",
              cost=0.5, recommended_harness=None):
    from watchmen.route import RouteDecision
    return RouteDecision(
        harness=harness, current_model=current, recommended_model=recommended,
        label=label, note="winner cheaper at ratio", avg_score=0.92,
        cost_vs_current=cost, recommended_harness=recommended_harness,
    )


def _route_result(tmp_path, bucket: str, decisions: list):
    from watchmen.route import RouteConfig, RouteResult, HarnessReference

    run_dir = tmp_path / "bundles" / "p" / "_route" / "test-run"
    run_dir.mkdir(parents=True)
    return RouteResult(
        run_id="test-run", run_dir=str(run_dir),
        config=RouteConfig(project_key="p", bucket=bucket),
        references=[
            HarnessReference(harness=d.harness, current_model=d.current_model,
                             last_session_ts=_now_iso(0), session_count_window=10)
            for d in decisions
        ],
        compare_results={},
        decisions=decisions,
    )


def test_rewrite_claude_code_writes_subagent_md_in_repo(tmp_path, monkeypatch):
    from watchmen.route_rewrite import apply_route_rewrites

    repo = tmp_path / "src-repo"
    repo.mkdir()
    _setup_project(tmp_path, monkeypatch, "p", str(repo))

    skill_dir = tmp_path / "bundles" / "p" / "skills" / "demo-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: demo-skill\ndescription: x\n---\n\n# Demo\nbody\n",
        encoding="utf-8",
    )
    result = _route_result(tmp_path, "demo-skill", [
        _decision("claude_code", "anthropic/claude-opus-4-7",
                  "anthropic/claude-sonnet-4-6"),
    ])
    outcomes = apply_route_rewrites(result, repo_root=str(repo))

    router = repo / ".claude" / "agents" / "demo-skill-router.md"
    assert router.exists()
    body = router.read_text(encoding="utf-8")
    assert "model: anthropic/claude-sonnet-4-6" in body
    assert "name: demo-skill-router" in body

    skill_md = (tmp_path / "bundles" / "p" / "skills" / "demo-skill" / "SKILL.md").read_text()
    assert "<!-- watchmen-route:dispatch -->" in skill_md
    assert "Claude Code" in skill_md
    assert "subagent_type=\"demo-skill-router\"" in skill_md

    # outcomes: one router + one skill-body row
    kinds = [o.artifact_kind for o in outcomes]
    assert kinds.count("router") == 1
    assert kinds.count("skill-body") == 1


def test_rewrite_codex_writes_profile_toml(tmp_path, monkeypatch):
    from watchmen import route_rewrite

    fake_home = tmp_path / "home"
    (fake_home / ".codex").mkdir(parents=True)
    monkeypatch.setattr(route_rewrite.Path, "home", staticmethod(lambda: fake_home))
    _setup_project(tmp_path, monkeypatch, "p", "/dev/null")

    skill_dir = tmp_path / "bundles" / "p" / "skills" / "demo-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: demo-skill\n---\nbody\n", encoding="utf-8",
    )
    result = _route_result(tmp_path, "demo-skill", [
        _decision("codex", "openai/gpt-5.5", "openai/gpt-5-mini"),
    ])
    route_rewrite.apply_route_rewrites(result, repo_root=None)

    profile = fake_home / ".codex" / "route-demo-skill.config.toml"
    assert profile.exists()
    text = profile.read_text(encoding="utf-8")
    assert 'model = "openai/gpt-5-mini"' in text
    skill_md = (tmp_path / "bundles" / "p" / "skills" / "demo-skill" / "SKILL.md").read_text()
    assert "codex exec --profile-v2 route-demo-skill" in skill_md


def test_rewrite_pi_falls_back_to_body_when_extension_missing(tmp_path, monkeypatch):
    from watchmen import route_rewrite

    fake_home = tmp_path / "home"
    (fake_home / ".pi" / "agent").mkdir(parents=True)
    monkeypatch.setattr(route_rewrite.Path, "home", staticmethod(lambda: fake_home))
    monkeypatch.setattr(
        route_rewrite, "PI_SUBAGENT_EXTENSION",
        fake_home / ".pi" / "agent" / "extensions" / "subagent",
    )
    _setup_project(tmp_path, monkeypatch, "p", "/dev/null")

    skill_dir = tmp_path / "bundles" / "p" / "skills" / "demo-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: demo-skill\n---\nbody\n", encoding="utf-8",
    )
    result = _route_result(tmp_path, "demo-skill", [
        _decision("pi", "z-ai/glm-4.5", "anthropic/claude-haiku-4-5"),
    ])
    outcomes = route_rewrite.apply_route_rewrites(result, repo_root=None)

    # No router file written
    by_kind = {(o.harness, o.artifact_kind): o for o in outcomes}
    assert by_kind[("pi", "router")].action == "skipped"
    assert "extension not installed" in by_kind[("pi", "router")].reason
    # But SKILL.md still gets the dispatch sentence
    skill_md = (tmp_path / "bundles" / "p" / "skills" / "demo-skill" / "SKILL.md").read_text()
    assert "pi --model anthropic/claude-haiku-4-5" in skill_md


def test_switch_harness_advises_instead_of_writing_unrunnable_file(tmp_path, monkeypatch):
    """A switch-harness winner is another runtime's model. Claude Code can't
    run it, so we must NOT write a router pinned to it — we advise running the
    skill on the harness that can, and the SKILL.md must not claim CC runs it."""
    from watchmen.route_rewrite import apply_route_rewrites

    repo = tmp_path / "src-repo"
    repo.mkdir()
    _setup_project(tmp_path, monkeypatch, "p", str(repo))

    skill_dir = tmp_path / "bundles" / "p" / "skills" / "demo-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: demo-skill\n---\nbody\n", encoding="utf-8",
    )
    result = _route_result(tmp_path, "demo-skill", [
        _decision("claude_code", "anthropic/claude-opus-4-7",
                  "openai/gpt-5-codex", label="switch-harness",
                  recommended_harness="codex"),
    ])
    outcomes = apply_route_rewrites(result, repo_root=str(repo))

    # No claude-code router file written for a model CC can't run.
    router = repo / ".claude" / "agents" / "demo-skill-router.md"
    assert not router.exists()

    by_kind = {(o.harness, o.artifact_kind): o for o in outcomes}
    assert by_kind[("claude_code", "advisory")].action == "skipped"
    assert "not runnable" in by_kind[("claude_code", "advisory")].reason

    skill_md = (skill_dir / "SKILL.md").read_text()
    assert "run it on Codex" in skill_md
    assert "openai/gpt-5-codex" in skill_md       # named in the advisory
    # ...but never as a recommended model CC would adopt.
    assert "Recommended model:" not in skill_md


def test_switch_harness_dispatches_via_ax_when_configured(tmp_path, monkeypatch):
    """With WATCHMEN_AX_SERVER set and an AX wrapper for the winning harness, a
    switch-harness decision emits a dispatcher subagent that calls `ax exec`
    instead of the advisory line — turning the recommendation into a real
    cross-runtime delegation. The frontmatter must NOT pin the foreign model."""
    from watchmen.route_rewrite import apply_route_rewrites

    monkeypatch.setenv("WATCHMEN_AX_SERVER", "localhost:8494")
    monkeypatch.setenv("WATCHMEN_AX_BIN", "ax")

    repo = tmp_path / "src-repo"
    repo.mkdir()
    _setup_project(tmp_path, monkeypatch, "p", str(repo))

    skill_dir = tmp_path / "bundles" / "p" / "skills" / "demo-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: demo-skill\n---\nbody\n", encoding="utf-8",
    )
    result = _route_result(tmp_path, "demo-skill", [
        _decision("claude_code", "anthropic/claude-opus-4-7",
                  "openai/gpt-5-codex", label="switch-harness",
                  recommended_harness="codex"),
    ])
    outcomes = apply_route_rewrites(result, repo_root=str(repo))

    # A dispatcher IS written this time (unlike the advisory path).
    router = repo / ".claude" / "agents" / "demo-skill-router.md"
    assert router.exists()
    body = router.read_text(encoding="utf-8")
    # It delegates through AX to the winning harness's AX agent...
    assert "ax exec --once --server localhost:8494 --agent codex" in body
    assert "[model] openai/gpt-5-codex" in body
    assert str(repo) in body                       # [workspace] header
    assert "<TASK>" in body                        # task placeholder
    # ...and never pins the foreign model in the Claude subagent frontmatter.
    assert "model: openai/gpt-5-codex" not in body

    by_kind = {(o.harness, o.artifact_kind): o for o in outcomes}
    assert ("claude_code", "ax-router") in by_kind
    assert ("claude_code", "advisory") not in by_kind

    skill_md = (skill_dir / "SKILL.md").read_text()
    assert "via AX" in skill_md
    assert "subagent_type=\"demo-skill-router\"" in skill_md
    # The independent foreign-model line is still suppressed.
    assert "Recommended model:" not in skill_md


def test_switch_harness_falls_back_to_advisory_when_ax_unset(tmp_path, monkeypatch):
    """No WATCHMEN_AX_SERVER => default behavior: advisory, no dispatcher."""
    from watchmen.route_rewrite import apply_route_rewrites

    monkeypatch.delenv("WATCHMEN_AX_SERVER", raising=False)

    repo = tmp_path / "src-repo"
    repo.mkdir()
    _setup_project(tmp_path, monkeypatch, "p", str(repo))

    skill_dir = tmp_path / "bundles" / "p" / "skills" / "demo-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: demo-skill\n---\nbody\n", encoding="utf-8",
    )
    result = _route_result(tmp_path, "demo-skill", [
        _decision("claude_code", "anthropic/claude-opus-4-7",
                  "openai/gpt-5-codex", label="switch-harness",
                  recommended_harness="codex"),
    ])
    outcomes = apply_route_rewrites(result, repo_root=str(repo))

    assert not (repo / ".claude" / "agents" / "demo-skill-router.md").exists()
    by_kind = {(o.harness, o.artifact_kind): o for o in outcomes}
    assert ("claude_code", "advisory") in by_kind
    assert ("claude_code", "ax-router") not in by_kind


def test_codex_source_dispatches_via_ax_inline(tmp_path, monkeypatch):
    """Codex has no subagent file — its native artifact is a model profile that
    can't invoke another runtime. So a Codex-source switch-harness route inlines
    the `ax exec` call into the SKILL.md dispatch block; no profile file is
    written, and the delegation targets the winning harness's AX agent."""
    from watchmen import route_rewrite
    from watchmen.route_rewrite import apply_route_rewrites

    monkeypatch.setenv("WATCHMEN_AX_SERVER", "localhost:8494")
    fake_home = tmp_path / "home"
    (fake_home / ".codex").mkdir(parents=True)
    monkeypatch.setattr(route_rewrite.Path, "home", staticmethod(lambda: fake_home))

    repo = tmp_path / "src-repo"
    repo.mkdir()
    _setup_project(tmp_path, monkeypatch, "p", str(repo))

    skill_dir = tmp_path / "bundles" / "p" / "skills" / "demo-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: demo-skill\n---\nbody\n", encoding="utf-8",
    )
    # Codex's work wins on Claude Code for this skill -> delegate Codex -> CC.
    result = _route_result(tmp_path, "demo-skill", [
        _decision("codex", "openai/gpt-5.5",
                  "anthropic/claude-opus-4-7", label="switch-harness",
                  recommended_harness="claude_code"),
    ])
    outcomes = apply_route_rewrites(result, repo_root=str(repo))

    # No codex profile file is written — dispatch lives in SKILL.md.
    assert not (fake_home / ".codex" / "route-demo-skill.config.toml").exists()

    by_kind = {(o.harness, o.artifact_kind): o for o in outcomes}
    assert ("codex", "ax-dispatch") in by_kind
    assert by_kind[("codex", "ax-dispatch")].action == "inline"
    assert by_kind[("codex", "ax-dispatch")].path == ""

    skill_md = (skill_dir / "SKILL.md").read_text()
    assert "ax exec --once --server localhost:8494 --agent claude-code" in skill_md
    assert "[model] anthropic/claude-opus-4-7" in skill_md
    assert "<TASK>" in skill_md
    assert "via AX" in skill_md
    assert "Recommended model:" not in skill_md     # suppressed for ax-routed


def test_foreign_candidate_winner_is_advised_not_emitted(tmp_path, monkeypatch):
    """A user-injected --candidate from another provider can win as a
    downshift (it's never labeled switch-harness because no current harness
    runs it). The provider_supports_model clause must still catch it so we
    don't stamp a GPT model into Claude Code's subagent file."""
    from watchmen.route_rewrite import apply_route_rewrites

    repo = tmp_path / "src-repo"
    repo.mkdir()
    _setup_project(tmp_path, monkeypatch, "p", str(repo))

    skill_dir = tmp_path / "bundles" / "p" / "skills" / "demo-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: demo-skill\n---\nbody\n", encoding="utf-8",
    )
    result = _route_result(tmp_path, "demo-skill", [
        _decision("claude_code", "anthropic/claude-opus-4-7",
                  "openai/gpt-5-mini", label="downshift"),
    ])
    outcomes = apply_route_rewrites(result, repo_root=str(repo))

    router = repo / ".claude" / "agents" / "demo-skill-router.md"
    assert not router.exists()

    by_kind = {(o.harness, o.artifact_kind): o for o in outcomes}
    assert by_kind[("claude_code", "advisory")].action == "skipped"

    skill_md = (skill_dir / "SKILL.md").read_text()
    assert "isn't run by any current harness" in skill_md
    assert "Recommended model:" not in skill_md


def test_rewrite_skill_body_is_idempotent(tmp_path, monkeypatch):
    """Running route twice replaces the block, doesn't append a duplicate."""
    from watchmen.route_rewrite import apply_route_rewrites

    repo = tmp_path / "src-repo"
    repo.mkdir()
    _setup_project(tmp_path, monkeypatch, "p", str(repo))

    skill_dir = tmp_path / "bundles" / "p" / "skills" / "demo-skill"
    skill_dir.mkdir(parents=True)
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(
        "---\nname: demo-skill\n---\nbody\n", encoding="utf-8",
    )
    result = _route_result(tmp_path, "demo-skill", [
        _decision("claude_code", "anthropic/claude-opus-4-7",
                  "anthropic/claude-sonnet-4-6"),
    ])
    apply_route_rewrites(result, repo_root=str(repo))
    apply_route_rewrites(result, repo_root=str(repo))

    text = skill_md.read_text()
    assert text.count("<!-- watchmen-route:dispatch -->") == 1
    assert text.count("<!-- /watchmen-route:dispatch -->") == 1


# ─── CLI candidate parsing ───────────────────────────────────────────


def test_parse_harnesses_normalizes_dashes():
    from watchmen.commands.route import _parse_harnesses

    assert _parse_harnesses("claude-code,codex") == ["claude_code", "codex"]
    assert _parse_harnesses("auto") == []
    assert _parse_harnesses("none") == []
    assert _parse_harnesses("claude-code", ["codex", "pi"]) == [
        "claude_code", "codex", "pi"
    ]
    # Deduplication
    assert _parse_harnesses("codex,codex,claude-code") == ["codex", "claude_code"]



def test_provider_supports_model_filters_cross_provider_correctly():
    """The compat filter keeps the candidate pool runnable: claude-pro
    only sees Anthropic models even if the user's CC corpus historically
    ran Gemini via a multi-provider proxy.  Regression for a real-world
    case caught during route smoke against the user's actual corpus."""
    from watchmen.route import provider_supports_model

    # claude-pro / anthropic accept Anthropic models only
    assert provider_supports_model("claude-opus-4-7", "claude-pro") is True
    assert provider_supports_model("claude-sonnet-4-20250514", "claude-pro") is True
    assert provider_supports_model("anthropic/claude-haiku-4-5", "claude-pro") is True
    assert provider_supports_model("google/gemini-3-flash-preview", "claude-pro") is False
    assert provider_supports_model("openai/gpt-5.2-codex", "claude-pro") is False
    assert provider_supports_model("z-ai/glm-4.7", "claude-pro") is False

    # chatgpt / openai accept OpenAI models only
    assert provider_supports_model("gpt-5.5", "chatgpt") is True
    assert provider_supports_model("openai/gpt-5-codex", "chatgpt") is True
    assert provider_supports_model("o5-mini", "chatgpt") is True
    assert provider_supports_model("anthropic/claude-opus-4-7", "chatgpt") is False
    assert provider_supports_model("claude-opus-4-7", "chatgpt") is False

    # openrouter needs namespaced ids
    assert provider_supports_model("anthropic/claude-opus-4-7", "openrouter") is True
    assert provider_supports_model("claude-opus-4-7", "openrouter") is False

    # Unknown / multi-provider — let everything through
    assert provider_supports_model("any-model", "opencode") is True


def test_candidates_for_harness_filters_cross_provider_models_from_corpus(
    tmp_path, monkeypatch,
):
    """End-to-end check on the filter wired into candidates_for_harness:
    seed a corpus with mixed cross-provider CC sessions (Gemini, GLM,
    GPT-codex via proxy); pool should only contain Anthropic candidates.
    """
    from watchmen import route

    repo = "/tmp/proj-mix"
    db = _setup_project(tmp_path, monkeypatch, "proj-mix", repo)
    _seed_sessions(db, repo, [
        # Mixed CC sessions (some claude, some via multi-provider proxy)
        {"agent": "claude_code", "model_dominant": "claude-opus-4-7",
         "started_at": _now_iso(1)},
        {"agent": "claude_code", "model_dominant": "claude-haiku-4-5",
         "started_at": _now_iso(2)},
        {"agent": "claude_code", "model_dominant": "google/gemini-3-flash-preview",
         "started_at": _now_iso(3)},  # cross-provider — must be filtered
        {"agent": "claude_code", "model_dominant": "openai/gpt-5.2-codex",
         "started_at": _now_iso(4)},  # cross-provider — must be filtered
        {"agent": "claude_code", "model_dominant": "z-ai/glm-4.7",
         "started_at": _now_iso(5)},  # cross-provider — must be filtered
    ])

    pool = route.candidates_for_harness(
        "claude_code", "claude-sonnet-4-6",  # not in corpus, won't be filtered
        project_key="proj-mix",
    )

    assert "claude-opus-4-7" in pool
    assert "claude-haiku-4-5" in pool
    assert "google/gemini-3-flash-preview" not in pool
    assert "openai/gpt-5.2-codex" not in pool
    assert "z-ai/glm-4.7" not in pool


def test_candidates_for_harness_discovers_globally_not_just_project(
    tmp_path, monkeypatch,
):
    """A model the user ran on project-A but not project-B should still
    appear in the candidate pool when routing project-B — they have
    access to it through the same harness/sub regardless of which
    project we're routing."""
    from watchmen import route

    repo_a = "/tmp/proj-a"
    db = _setup_project(tmp_path, monkeypatch, "proj-a", repo_a)
    _seed_sessions(db, repo_a, [
        # Project A: opus
        {"agent": "claude_code", "model_dominant": "claude-opus-4-7",
         "started_at": _now_iso(1), "project_dir": repo_a},
        # Project B: sonnet + haiku (different repo)
        {"agent": "claude_code", "model_dominant": "claude-sonnet-4-6",
         "started_at": _now_iso(2), "project_dir": "/tmp/proj-b"},
        {"agent": "claude_code", "model_dominant": "claude-haiku-4-5",
         "started_at": _now_iso(3), "project_dir": "/tmp/proj-b"},
    ])

    # Route on proj-a; project_dir_predicate scopes to proj-a; reference
    # detection picks opus.  But candidates should still pull from B too
    # since proj-b's sonnet + haiku are valid swap targets for proj-a.
    pool = route.candidates_for_harness(
        "claude_code", "claude-opus-4-7",
        project_key="proj-a",
    )
    assert "claude-sonnet-4-6" in pool
    assert "claude-haiku-4-5" in pool


# ─── Model id canonicalization ───────────────────────────────────────


def test_canonicalize_model_id_strips_provider_namespace():
    from watchmen.route import _canonicalize_corpus_model_id
    assert _canonicalize_corpus_model_id("anthropic/claude-opus-4-7") == "claude-opus-4-7"
    assert _canonicalize_corpus_model_id("openai/gpt-5.5") == "gpt-5.5"


def test_canonicalize_model_id_converts_dot_versions_for_claude():
    """CC display strings like `claude-opus-4.7` are not valid API ids;
    the canonical form uses dashes between version digits."""
    from watchmen.route import _canonicalize_corpus_model_id
    assert _canonicalize_corpus_model_id("claude-opus-4.7") == "claude-opus-4-7"
    assert _canonicalize_corpus_model_id("anthropic/claude-opus-4.7") == "claude-opus-4-7"
    assert _canonicalize_corpus_model_id("claude-sonnet-4.6") == "claude-sonnet-4-6"


def test_canonicalize_model_id_reorders_v4plus_family_after_version():
    """Anthropic flipped naming at v4: family now precedes version digits.
    Pre-v4 ids (3, 3.5) keep family-at-end and should stay canonical."""
    from watchmen.route import _canonicalize_corpus_model_id
    # v4.5 written wrong way → should swap
    assert _canonicalize_corpus_model_id("claude-4-5-haiku-20251001") == "claude-haiku-4-5-20251001"
    # v3.5 is genuinely family-at-end canonical → should NOT swap
    assert _canonicalize_corpus_model_id("claude-3-5-sonnet-20241022") == "claude-3-5-sonnet-20241022"


def test_canonicalize_model_id_drops_sentinels():
    from watchmen.route import _canonicalize_corpus_model_id
    assert _canonicalize_corpus_model_id("") is None
    assert _canonicalize_corpus_model_id("<synthetic>") is None
    assert _canonicalize_corpus_model_id("null") is None
    assert _canonicalize_corpus_model_id("   ") is None


def test_canonicalize_model_id_preserves_valid_ids():
    from watchmen.route import _canonicalize_corpus_model_id
    # Dated canonical
    assert _canonicalize_corpus_model_id("claude-haiku-4-5-20251001") == "claude-haiku-4-5-20251001"
    assert _canonicalize_corpus_model_id("claude-sonnet-4-20250514") == "claude-sonnet-4-20250514"
    # Dateless canonical
    assert _canonicalize_corpus_model_id("claude-opus-4-7") == "claude-opus-4-7"
    # OpenAI
    assert _canonicalize_corpus_model_id("gpt-5.5") == "gpt-5.5"


# ─── Provider catalog discovery integration ──────────────────────────


def test_candidates_for_harness_filters_to_provider_catalog_when_available(
    tmp_path, monkeypatch,
):
    """When the OAuth provider's /v1/models returns an authoritative list,
    corpus ids get intersected with it: a model in corpus history but
    absent from the live catalog (retired, renamed, or display-string
    artifact) gets dropped."""
    from watchmen import route

    repo = "/tmp/proj-cat"
    db = _setup_project(tmp_path, monkeypatch, "proj-cat", repo)
    _seed_sessions(db, repo, [
        {"agent": "claude_code", "model_dominant": "claude-opus-4-7",
         "started_at": _now_iso(1)},
        # Stale corpus id: model retired since the session was recorded.
        {"agent": "claude_code", "model_dominant": "claude-opus-3-5-20240620",
         "started_at": _now_iso(60)},
        # Display-string artifact (not API-valid)
        {"agent": "claude_code", "model_dominant": "claude-opus-4.7",
         "started_at": _now_iso(2)},
    ])

    # Stub the catalog to return ONLY canonical current ids.
    route._provider_available_models_cached.cache_clear()
    monkeypatch.setattr(
        route, "provider_available_models",
        lambda provider: ["claude-opus-4-7", "claude-sonnet-4-6", "claude-haiku-4-5-20251001"],
    )

    pool = route.candidates_for_harness(
        "claude_code", "claude-sonnet-4-6",
        project_key="proj-cat",
    )

    # Retired stale id dropped; display-string artifact canonicalized then matched.
    assert "claude-opus-3-5-20240620" not in pool
    # The dot-form gets canonicalized to `claude-opus-4-7` which IS in catalog.
    assert "claude-opus-4-7" in pool
    # Backfill from catalog: haiku appears even though no corpus session ran it.
    assert "claude-haiku-4-5-20251001" in pool


def test_candidates_for_harness_falls_back_to_heuristic_when_catalog_empty(
    tmp_path, monkeypatch,
):
    """When provider catalog discovery returns [] (transient failure),
    candidates_for_harness falls back to the prefix heuristic — so route
    still runs against corpus history even if the network is flaky."""
    from watchmen import route

    repo = "/tmp/proj-nocat"
    db = _setup_project(tmp_path, monkeypatch, "proj-nocat", repo)
    _seed_sessions(db, repo, [
        {"agent": "claude_code", "model_dominant": "claude-opus-4-7",
         "started_at": _now_iso(1)},
        {"agent": "claude_code", "model_dominant": "google/gemini-3-flash",
         "started_at": _now_iso(2)},  # cross-provider — still filtered by heuristic
    ])

    route._provider_available_models_cached.cache_clear()
    monkeypatch.setattr(route, "provider_available_models", lambda provider: [])

    pool = route.candidates_for_harness(
        "claude_code", "claude-sonnet-4-6",
        project_key="proj-nocat",
    )

    assert "claude-opus-4-7" in pool  # heuristic accepts claude- prefix
    assert "google/gemini-3-flash" not in pool  # heuristic rejects non-anthropic
