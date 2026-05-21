"""Tests for the skill mesh / distillation planner."""

from __future__ import annotations

import io
import json
from pathlib import Path


def _write_skill(base: Path, slug: str, description: str, when: list[str], body: str = "") -> None:
    skill_dir = base / "skills" / slug
    skill_dir.mkdir(parents=True, exist_ok=True)
    when_lines = "\n".join(f"  - {line}" for line in when)
    (skill_dir / "SKILL.md").write_text(
        f"""---
name: {slug}
description: {description}
when_to_use:
{when_lines}
---

# {slug}

## Procedure

{body}
""",
        encoding="utf-8",
    )


def test_skillmesh_clusters_similar_skills_and_keeps_standalone(tmp_path, monkeypatch):
    from watchmen import skillmesh

    bundles = tmp_path / "bundles"
    project = bundles / "demo"
    _write_skill(
        project,
        "pytest-failure-triage",
        "Debug failing pytest tests and isolate regressions",
        ["User asks to debug pytest failures", "A Python test suite fails after a change"],
        "- Run pytest.\n- Inspect failing assertions.\n- Patch the regression.",
    )
    _write_skill(
        project,
        "python-test-fixer",
        "Fix Python test regressions after pytest failures",
        ["User asks to fix broken Python tests", "Pytest output contains failing assertions"],
        "- Reproduce with pytest.\n- Compare expected and actual values.\n- Fix the tested code.",
    )
    _write_skill(
        project,
        "release-notes",
        "Write release notes from changelog entries",
        ["User asks for release notes or a changelog summary"],
        "- Read CHANGELOG.md.\n- Summarize user-visible changes.",
    )
    monkeypatch.setattr(skillmesh, "BUNDLES_DIR", bundles)

    plan = skillmesh.build_distill_plan("demo", min_similarity=0.20)

    assert plan.skill_count == 3
    assert plan.edge_count >= 1
    assert plan.cluster_count == 1
    assert set(plan.clusters[0].members) == {"pytest-failure-triage", "python-test-fixer"}
    assert plan.standalone == ["release-notes"]
    assert plan.context_rot_score > 0


def test_skillmesh_metadata_scope_ignores_skill_md_body_noise(tmp_path, monkeypatch):
    from watchmen import skillmesh

    bundles = tmp_path / "bundles"
    project = bundles / "demo"
    noisy_body = (
        "- rollout docker curriculum smoke pipeline rollout docker curriculum smoke pipeline.\n"
        "- rollout docker curriculum smoke pipeline rollout docker curriculum smoke pipeline."
    )
    _write_skill(
        project,
        "release-notes",
        "Write release notes from changelog entries",
        ["User asks for release notes"],
        noisy_body,
    )
    _write_skill(
        project,
        "meeting-prep",
        "Prepare an agenda from calendar notes",
        ["User asks for meeting preparation"],
        noisy_body,
    )
    monkeypatch.setattr(skillmesh, "BUNDLES_DIR", bundles)

    metadata_plan = skillmesh.build_distill_plan("demo", min_similarity=0.20)
    skill_md_plan = skillmesh.build_distill_plan("demo", min_similarity=0.20, source_scope="skill-md")

    assert metadata_plan.source_scope == "metadata"
    assert metadata_plan.edge_count == 0
    assert skill_md_plan.edge_count >= 1


def test_skillmesh_folder_scope_can_use_extra_script_digest(tmp_path, monkeypatch):
    from watchmen import skillmesh

    bundles = tmp_path / "bundles"
    project = bundles / "demo"
    _write_skill(
        project,
        "release-notes",
        "Write release notes from changelog entries",
        ["User asks for release notes"],
    )
    _write_skill(
        project,
        "meeting-prep",
        "Prepare an agenda from calendar notes",
        ["User asks for meeting preparation"],
    )
    scripts_a = project / "skills" / "release-notes" / "scripts"
    scripts_b = project / "skills" / "meeting-prep" / "scripts"
    scripts_a.mkdir()
    scripts_b.mkdir()
    (scripts_a / "collect.py").write_text(
        "# rollout docker curriculum smoke pipeline collector\n"
        "def collect_rollout_curriculum_smoke_pipeline():\n"
        "    pass\n",
        encoding="utf-8",
    )
    (scripts_b / "probe.py").write_text(
        "# rollout docker curriculum smoke pipeline collector\n"
        "def probe_rollout_curriculum_smoke_pipeline():\n"
        "    pass\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(skillmesh, "BUNDLES_DIR", bundles)

    metadata_plan = skillmesh.build_distill_plan("demo", min_similarity=0.20)
    folder_plan = skillmesh.build_distill_plan("demo", min_similarity=0.20, source_scope="folder")

    assert metadata_plan.edge_count == 0
    assert folder_plan.source_scope == "folder"
    assert folder_plan.edge_count >= 1


def test_skillmesh_writes_plan_and_stages_pending_candidate(tmp_path, monkeypatch):
    from watchmen import skillmesh

    bundles = tmp_path / "bundles"
    project = bundles / "demo"
    _write_skill(
        project,
        "docker-log-debug",
        "Debug Docker service logs and failing containers",
        ["Docker container exits or logs show an error"],
        "- Inspect docker logs.\n- Check compose service names.",
    )
    _write_skill(
        project,
        "container-failure-triage",
        "Triage Docker container startup failures",
        ["Container startup fails or Docker logs show errors"],
        "- Inspect docker logs.\n- Check exposed ports and env vars.",
    )
    monkeypatch.setattr(skillmesh, "BUNDLES_DIR", bundles)

    plan = skillmesh.build_distill_plan("demo", min_similarity=0.20, source_scope="skill-md")
    plan_path = skillmesh.write_distill_plan(plan)
    staged = skillmesh.stage_distilled_candidates(plan)

    assert plan_path.exists()
    assert staged
    staged_text = staged[0].read_text()
    assert "staged distillation draft" in staged_text
    assert "docker-log-debug" in staged_text
    assert "container-failure-triage" in staged_text
    assert (staged[0].parent / "references" / "source-skills.md").exists()


def test_skillmesh_parses_llm_rubric_json():
    from watchmen import skillmesh

    raw = skillmesh._extract_json_object(
        """```json
        {
          "similarity": 0.92,
          "relationship": "duplicate",
          "merge_decision": "merge",
          "trigger_overlap": 5,
          "procedure_overlap": 4,
          "boundary_compatibility": 5,
          "context_rot_reduction": 5,
          "risk": "low",
          "rationale": "They describe the same workflow.",
          "preserve": ["keep the smoke-test branch"]
        }
        ```"""
    )
    judgment = skillmesh._coerce_judgment(raw)

    assert judgment.similarity == 0.92
    assert judgment.relationship == "duplicate"
    assert judgment.merge_decision == "merge"
    assert judgment.trigger_overlap == 5
    assert judgment.preserve == ["keep the smoke-test branch"]


def test_skillmesh_llm_rubric_retries_invalid_json_then_falls_back(tmp_path, monkeypatch):
    from watchmen import agent, config, skillmesh

    bundles = tmp_path / "bundles"
    project = bundles / "demo"
    _write_skill(project, "alpha-build", "Build alpha workflows", ["User asks for alpha build"])
    _write_skill(project, "beta-build", "Build beta workflows", ["User asks for beta build"])
    monkeypatch.setattr(skillmesh, "BUNDLES_DIR", bundles)
    monkeypatch.setattr(config, "active_provider", lambda: "openrouter")

    calls = 0

    def fake_chat_call(*args, **kwargs):
        nonlocal calls
        calls += 1
        assert kwargs["response_format"] == {"type": "json_object"}
        return {"choices": [{"message": {"content": "not json"}}]}

    monkeypatch.setattr(agent, "chat_call", fake_chat_call)
    nodes = skillmesh.load_skill_nodes("demo")
    by_slug = {node.slug: node for node in nodes}
    cluster = skillmesh._build_cluster(1, ["alpha-build", "beta-build"], by_slug, [])
    plan = skillmesh.DistillPlan(
        project_key="demo",
        created_at="now",
        skill_count=2,
        edge_count=0,
        cluster_count=0,
        context_rot_score=0,
        total_skill_bytes=0,
        min_similarity=0.8,
        source_scope="metadata",
        nodes=nodes,
        edges=[],
        clusters=[],
        standalone=[],
        semantic_model="fake-model",
    )

    judgment = skillmesh._judge_cluster_semantically(
        None,
        plan,
        cluster,
        idx=1,
        total=1,
        model="fake-model",
        show_visual=False,
    )

    assert calls == 2
    assert judgment.merge_decision == "keep_separate"
    assert judgment.risk == "high"
    assert "parseable" in judgment.rationale


def test_distill_default_model_respects_env_override_then_global_default(monkeypatch):
    from watchmen import config

    monkeypatch.setattr(config, "default_model", lambda: "global-default-model")

    monkeypatch.setattr(config, "read_env_var", lambda key, default=None: None)
    assert config.distill_default_model() == "global-default-model"

    monkeypatch.setattr(
        config,
        "read_env_var",
        lambda key, default=None: "override-model" if key == "WATCHMEN_DISTILL_MODEL" else None,
    )
    assert config.distill_default_model() == "override-model"


def test_skillmesh_stage_skips_llm_keep_separate(tmp_path, monkeypatch):
    from watchmen import skillmesh

    bundles = tmp_path / "bundles"
    project = bundles / "demo"
    _write_skill(
        project,
        "docker-log-debug",
        "Debug Docker service logs and failing containers",
        ["Docker container exits or logs show an error"],
        "- Inspect docker logs.\n- Check compose service names.",
    )
    _write_skill(
        project,
        "container-failure-triage",
        "Triage Docker container startup failures",
        ["Container startup fails or Docker logs show errors"],
        "- Inspect docker logs.\n- Check exposed ports and env vars.",
    )
    monkeypatch.setattr(skillmesh, "BUNDLES_DIR", bundles)

    plan = skillmesh.build_distill_plan("demo", min_similarity=0.20, source_scope="skill-md")
    plan.clusters[0].semantic_judgment = skillmesh.SkillSimilarityJudgment(
        similarity=0.41,
        relationship="adjacent",
        merge_decision="keep_separate",
        trigger_overlap=2,
        procedure_overlap=2,
        boundary_compatibility=3,
        context_rot_reduction=1,
        risk="high",
        rationale="Related but not replaceable.",
        preserve=[],
    )

    assert skillmesh.stage_distilled_candidates(plan) == []


def test_skillmesh_apply_promotes_distilled_and_archives_sources(tmp_path, monkeypatch):
    from watchmen import skillmesh

    bundles = tmp_path / "bundles"
    project = bundles / "demo"
    _write_skill(
        project,
        "docker-log-debug",
        "Debug Docker service logs and failing containers",
        ["Docker container exits or logs show an error"],
        "- Inspect docker logs.\n- Check compose service names.",
    )
    _write_skill(
        project,
        "container-failure-triage",
        "Triage Docker container startup failures",
        ["Container startup fails or Docker logs show errors"],
        "- Inspect docker logs.\n- Check exposed ports and env vars.",
    )
    monkeypatch.setattr(skillmesh, "BUNDLES_DIR", bundles)

    plan = skillmesh.build_distill_plan("demo", min_similarity=0.20, source_scope="skill-md")
    selected = [plan.clusters[0].proposed_slug]

    result = skillmesh.apply_distilled_candidates(plan, selected)

    skills_dir = project / "skills"
    assert result.promoted == selected
    assert (skills_dir / selected[0] / "SKILL.md").exists()
    assert not (skills_dir / "docker-log-debug").exists()
    assert not (skills_dir / "container-failure-triage").exists()
    assert result.archive_dir is not None
    archive = Path(result.archive_dir)
    assert (archive / "sources" / "docker-log-debug" / "SKILL.md").exists()
    assert (archive / "sources" / "container-failure-triage" / "SKILL.md").exists()
    blocklist = json.loads((project / "_blocklist.json").read_text())
    assert blocklist == ["container-failure-triage", "docker-log-debug"]
    assert result.audit_path is not None
    assert "distill merge" in Path(result.audit_path).read_text()


def test_skillmesh_semantic_plan_compares_all_pairs_without_local_filter(tmp_path, monkeypatch):
    from watchmen import skillmesh

    bundles = tmp_path / "bundles"
    project = bundles / "demo"
    _write_skill(project, "alpha-build", "Build alpha workflows", ["User asks for alpha build"])
    _write_skill(project, "beta-build", "Build beta workflows", ["User asks for beta build"])
    _write_skill(project, "release-notes", "Write release notes", ["User asks for release notes"])
    monkeypatch.setattr(skillmesh, "BUNDLES_DIR", bundles)

    seen: list[tuple[str, ...]] = []

    def fake_judge(*args, **kwargs):
        cluster = args[2]
        pair = tuple(cluster.members)
        seen.append(pair)
        if pair == ("alpha-build", "beta-build"):
            return skillmesh.SkillSimilarityJudgment(
                similarity=0.91,
                relationship="duplicate",
                merge_decision="merge",
                trigger_overlap=5,
                procedure_overlap=4,
                boundary_compatibility=5,
                context_rot_reduction=5,
                risk="low",
                rationale="same build workflow",
                preserve=[],
            )
        return skillmesh.SkillSimilarityJudgment(
            similarity=0.18,
            relationship="unrelated",
            merge_decision="keep_separate",
            trigger_overlap=1,
            procedure_overlap=0,
            boundary_compatibility=3,
            context_rot_reduction=0,
            risk="high",
            rationale="not replaceable",
            preserve=[],
        )

    monkeypatch.setattr(skillmesh, "_judge_cluster_semantically", fake_judge)

    plan = skillmesh.build_semantic_distill_plan(
        "demo",
        model="fake-model",
        min_similarity=0.80,
        show_visual=False,
    )

    assert len(seen) == 3
    assert plan.semantic_model == "fake-model"
    assert len(plan.semantic_judgments) == 3
    assert plan.edge_count == 1
    assert plan.cluster_count == 1
    rejected = [
        edge
        for edge in plan.semantic_judgments
        if edge.semantic_judgment and edge.semantic_judgment.merge_decision == "keep_separate"
    ]
    assert len(rejected) == 2
    assert plan.clusters[0].semantic_judgment is not None
    assert plan.clusters[0].semantic_judgment.similarity == 0.91
    assert set(plan.clusters[0].members) == {"alpha-build", "beta-build"}


def test_skillmesh_semantic_summary_shows_rejected_judgments(tmp_path, monkeypatch):
    from rich.console import Console

    from watchmen import skillmesh

    bundles = tmp_path / "bundles"
    project = bundles / "demo"
    _write_skill(project, "alpha-build", "Build alpha workflows", ["User asks for alpha build"])
    _write_skill(project, "beta-build", "Build beta workflows", ["User asks for beta build"])
    _write_skill(project, "release-notes", "Write release notes", ["User asks for release notes"])
    monkeypatch.setattr(skillmesh, "BUNDLES_DIR", bundles)

    def fake_judge(*args, **kwargs):
        cluster = args[2]
        scores = {
            ("alpha-build", "beta-build"): 0.79,
            ("alpha-build", "release-notes"): 0.12,
            ("beta-build", "release-notes"): 0.11,
        }
        return skillmesh.SkillSimilarityJudgment(
            similarity=scores[tuple(cluster.members)],
            relationship="overlap",
            merge_decision="keep_separate",
            trigger_overlap=3,
            procedure_overlap=2,
            boundary_compatibility=4,
            context_rot_reduction=2,
            risk="medium",
            rationale="Close, but below merge threshold.",
            preserve=[],
        )

    monkeypatch.setattr(skillmesh, "_judge_cluster_semantically", fake_judge)

    plan = skillmesh.build_semantic_distill_plan(
        "demo",
        model="fake-model",
        min_similarity=0.80,
        show_visual=False,
    )
    output = io.StringIO()
    console = Console(file=output, force_terminal=False, width=140)

    skillmesh.render_plan_summary(plan, console=console)

    rendered = output.getvalue()
    assert "judged_pairs=3" in rendered
    assert "candidate_edges=0" in rendered
    assert "alpha-build" in rendered
    assert "beta-build" in rendered
    assert "showing strongest rejected judgments" in rendered


def test_skillmesh_semantic_progress_is_minimal_text():
    from watchmen import skillmesh

    node_a = skillmesh.SkillNode(
        slug="model-repl-compliance-test",
        name="",
        description="",
        when_to_use=[],
        when_not_to_use=[],
        token_count=1,
        byte_size=1,
        keywords=[],
    )
    node_b = skillmesh.SkillNode(
        slug="model-repl-compliance-tester",
        name="",
        description="",
        when_to_use=[],
        when_not_to_use=[],
        token_count=1,
        byte_size=1,
        keywords=[],
    )
    plan = skillmesh.DistillPlan(
        project_key="demo",
        created_at="now",
        skill_count=2,
        edge_count=0,
        cluster_count=0,
        context_rot_score=0,
        total_skill_bytes=0,
        min_similarity=0.8,
        source_scope="metadata",
        nodes=[node_a, node_b],
        edges=[],
        clusters=[],
        standalone=[],
        semantic_model="fake-model",
    )
    cluster = skillmesh._build_cluster(1, [node_a.slug, node_b.slug], {node_a.slug: node_a, node_b.slug: node_b}, [])
    progress = skillmesh._SemanticProgressState(
        accepted_edges=1,
        strongest_pair=(node_a.slug, node_b.slug),
        strongest_score=0.84,
    )

    rendered = skillmesh._semantic_progress_frame(plan, cluster, 21, 28, 0, progress=progress).plain

    assert "semantic distill 21/28" in rendered
    assert "judging: model-repl-compliance-test <-> model-repl-compliance-tester" in rendered
    assert "merge hits: 1" in rendered
    assert "0.84" in rendered
    assert "/\\" not in rendered
    assert "semantic forge" not in rendered


def test_skillmesh_does_not_merge_through_transitive_bridge():
    from watchmen import skillmesh

    nodes = [
        skillmesh.SkillNode(
            slug="skill-a",
            name="skill-a",
            description="",
            when_to_use=[],
            when_not_to_use=[],
            token_count=1,
            byte_size=1,
            keywords=[],
        ),
        skillmesh.SkillNode(
            slug="skill-b",
            name="skill-b",
            description="",
            when_to_use=[],
            when_not_to_use=[],
            token_count=1,
            byte_size=1,
            keywords=[],
        ),
        skillmesh.SkillNode(
            slug="skill-c",
            name="skill-c",
            description="",
            when_to_use=[],
            when_not_to_use=[],
            token_count=1,
            byte_size=1,
            keywords=[],
        ),
    ]
    edges = [
        skillmesh.SkillEdge(
            a="skill-a",
            b="skill-b",
            similarity=0.42,
            shared_keywords=[],
            a_only=[],
            b_only=[],
        ),
        skillmesh.SkillEdge(
            a="skill-b",
            b="skill-c",
            similarity=0.40,
            shared_keywords=[],
            a_only=[],
            b_only=[],
        ),
    ]

    clusters = skillmesh._find_tight_clusters(nodes, edges, min_similarity=0.28)

    assert clusters == [["skill-a", "skill-b"]]


def test_skillmesh_proposed_slug_dedupes_fallback_words():
    from watchmen import skillmesh

    nodes = [
        skillmesh.SkillNode(
            slug="batch-rollout-collection",
            name="",
            description="",
            when_to_use=[],
            when_not_to_use=[],
            token_count=1,
            byte_size=1,
            keywords=[],
        ),
        skillmesh.SkillNode(
            slug="batch-rollout-collector",
            name="",
            description="",
            when_to_use=[],
            when_not_to_use=[],
            token_count=1,
            byte_size=1,
            keywords=[],
        ),
        skillmesh.SkillNode(
            slug="build-ctf-curriculum",
            name="",
            description="",
            when_to_use=[],
            when_not_to_use=[],
            token_count=1,
            byte_size=1,
            keywords=[],
        ),
    ]

    assert skillmesh._proposed_slug(nodes, []) == "distilled-batch-rollout-collection"


def test_skillmesh_proposed_slug_prefers_source_slug_center():
    from watchmen import skillmesh

    batch_nodes = [
        skillmesh.SkillNode(
            slug="batch-rollout-collection",
            name="",
            description="",
            when_to_use=[],
            when_not_to_use=[],
            token_count=1,
            byte_size=1,
            keywords=[],
        ),
        skillmesh.SkillNode(
            slug="batch-rollout-collector",
            name="",
            description="",
            when_to_use=[],
            when_not_to_use=[],
            token_count=1,
            byte_size=1,
            keywords=[],
        ),
    ]
    smoke_nodes = [
        skillmesh.SkillNode(
            slug="rlm-harness-smoke",
            name="",
            description="",
            when_to_use=[],
            when_not_to_use=[],
            token_count=1,
            byte_size=1,
            keywords=[],
        ),
        skillmesh.SkillNode(
            slug="rlm-smoke-test",
            name="",
            description="",
            when_to_use=[],
            when_not_to_use=[],
            token_count=1,
            byte_size=1,
            keywords=[],
        ),
    ]

    assert (
        skillmesh._proposed_slug(batch_nodes, ["batch", "challenges", "ctf", "curriculum", "data", "rollouts"])
        == "distilled-batch-rollout-collection"
    )
    assert (
        skillmesh._proposed_slug(smoke_nodes, ["challenge", "harness", "rlm", "rollout", "smoke", "test"])
        == "distilled-rlm-harness-smoke"
    )
