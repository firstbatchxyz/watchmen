"""Tests for watchmen.skill_install — bundle enumeration + symlink install
with manifest-tracked conflict handling."""

from __future__ import annotations

from pathlib import Path

import pytest

from watchmen import skill_install as si


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    """Redirect BUNDLES_DIR, the manifest, and the two harness skill dirs into
    a temp tree so nothing touches the developer's real install."""
    bundles = tmp_path / "bundles"
    claude = tmp_path / "claude" / "skills"
    codex = tmp_path / "codex" / "skills"
    bundles.mkdir(parents=True)
    monkeypatch.setattr(si, "BUNDLES_DIR", bundles)
    monkeypatch.setattr(si, "MANIFEST_PATH", tmp_path / "install_manifest.json")
    monkeypatch.setattr(si, "HARNESS_SKILL_DIRS", {"claude_code": claude, "codex": codex})
    return tmp_path


def _make_bundle_skill(env: Path, project: str, slug: str, *, description: str = "does a thing") -> Path:
    skill_dir = env / "bundles" / project / "skills" / slug
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {slug}\ndescription: {description}\n---\n\nbody\n", encoding="utf-8"
    )
    (skill_dir / "scripts").mkdir()
    (skill_dir / "scripts" / "run.py").write_text("print('hi')\n", encoding="utf-8")
    return skill_dir


def test_bundle_skills_enumerates(env):
    _make_bundle_skill(env, "proj", "alpha", description="alpha skill")
    _make_bundle_skill(env, "proj", "beta")
    skills = si.bundle_skills("proj")
    assert [s.slug for s in skills] == ["alpha", "beta"]
    assert skills[0].description == "alpha skill"


def test_bundle_skills_empty_when_no_bundle(env):
    assert si.bundle_skills("nope") == []


def test_bundle_skills_skips_dirs_without_skill_md(env):
    stray = env / "bundles" / "proj" / "skills" / "no-md"
    stray.mkdir(parents=True)
    assert si.bundle_skills("proj") == []


def test_install_creates_symlink_to_bundle(env):
    src = _make_bundle_skill(env, "proj", "alpha")
    [skill] = si.bundle_skills("proj")
    res = si.install_skill(skill, "claude_code", project_key="proj")
    assert res.action == "installed"
    target = env / "claude" / "skills" / "alpha"
    assert target.is_symlink()
    assert target.resolve() == src.resolve()
    # the symlinked dir exposes SKILL.md + scripts to the agent
    assert (target / "SKILL.md").exists()
    assert (target / "scripts" / "run.py").exists()


def test_install_records_manifest(env):
    _make_bundle_skill(env, "proj", "alpha")
    [skill] = si.bundle_skills("proj")
    si.install_skill(skill, "claude_code", project_key="proj")
    entries = si.installed_targets("proj")
    assert len(entries) == 1
    assert entries[0]["slug"] == "alpha"
    assert entries[0]["harness"] == "claude_code"


def test_reinstall_replaces_watchmen_managed_target(env):
    _make_bundle_skill(env, "proj", "alpha")
    [skill] = si.bundle_skills("proj")
    si.install_skill(skill, "claude_code", project_key="proj")
    res = si.install_skill(skill, "claude_code", project_key="proj")
    assert res.action == "replaced"
    # still exactly one manifest entry for the target (no duplicate)
    assert len(si.installed_targets("proj")) == 1


def test_install_skips_user_made_target(env):
    _make_bundle_skill(env, "proj", "alpha")
    [skill] = si.bundle_skills("proj")
    # user already has a hand-made skill at the same slug
    user_dir = env / "claude" / "skills" / "alpha"
    user_dir.mkdir(parents=True)
    (user_dir / "SKILL.md").write_text("mine\n", encoding="utf-8")
    res = si.install_skill(skill, "claude_code", project_key="proj")
    assert res.action == "skipped_conflict"
    # the user's dir is untouched (still a real dir, not a symlink)
    assert user_dir.is_dir() and not user_dir.is_symlink()
    assert (user_dir / "SKILL.md").read_text() == "mine\n"


def test_force_overrides_user_made_target(env):
    _make_bundle_skill(env, "proj", "alpha")
    [skill] = si.bundle_skills("proj")
    user_dir = env / "claude" / "skills" / "alpha"
    user_dir.mkdir(parents=True)
    (user_dir / "SKILL.md").write_text("mine\n", encoding="utf-8")
    res = si.install_skill(skill, "claude_code", project_key="proj", force=True)
    assert res.action == "installed"
    assert (env / "claude" / "skills" / "alpha").is_symlink()


def test_unknown_harness_skipped(env):
    _make_bundle_skill(env, "proj", "alpha")
    [skill] = si.bundle_skills("proj")
    res = si.install_skill(skill, "opencode", project_key="proj")
    assert res.action == "skipped_no_dir"
    assert res.target is None


def test_install_project_all_harnesses(env):
    _make_bundle_skill(env, "proj", "alpha")
    _make_bundle_skill(env, "proj", "beta")
    results = si.install_project("proj", scope="global")
    actions = {(r.slug, r.harness): r.action for r in results}
    assert actions[("alpha", "claude_code")] == "installed"
    assert actions[("alpha", "codex")] == "installed"
    assert actions[("beta", "claude_code")] == "installed"
    assert (env / "claude" / "skills" / "alpha").is_symlink()
    assert (env / "codex" / "skills" / "beta").is_symlink()


def test_install_project_slug_filter(env):
    _make_bundle_skill(env, "proj", "alpha")
    _make_bundle_skill(env, "proj", "beta")
    results = si.install_project("proj", harnesses=["claude_code"], slugs=["alpha"], scope="global")
    assert {r.slug for r in results} == {"alpha"}
    assert (env / "claude" / "skills" / "alpha").is_symlink()
    assert not (env / "claude" / "skills" / "beta").exists()


def test_uninstall_removes_managed_link(env):
    _make_bundle_skill(env, "proj", "alpha")
    [skill] = si.bundle_skills("proj")
    si.install_skill(skill, "claude_code", project_key="proj")
    res = si.uninstall_skill("alpha", "claude_code")
    assert res.action == "uninstalled"
    assert not (env / "claude" / "skills" / "alpha").exists()
    assert si.installed_targets("proj") == []


def test_uninstall_refuses_user_made_target(env):
    user_dir = env / "claude" / "skills" / "alpha"
    user_dir.mkdir(parents=True)
    (user_dir / "SKILL.md").write_text("mine\n", encoding="utf-8")
    res = si.uninstall_skill("alpha", "claude_code")
    assert res.action == "skipped_conflict"
    assert user_dir.is_dir()


def test_uninstall_not_installed(env):
    res = si.uninstall_skill("ghost", "claude_code")
    assert res.action == "not_installed"


def test_project_scope_installs_into_repo(env):
    """scope='project' links into <repo>/.claude/skills, not the global dir."""
    _make_bundle_skill(env, "proj", "alpha")
    [skill] = si.bundle_skills("proj")
    repo = env / "repo"
    repo.mkdir()
    res = si.install_skill(skill, "claude_code", project_key="proj", scope="project", repo=repo)
    assert res.action == "installed"
    assert (repo / ".claude" / "skills" / "alpha").is_symlink()
    # nothing landed in the global dir
    assert not (env / "claude" / "skills" / "alpha").exists()
    entry = si.installed_targets("proj")[0]
    assert entry["scope"] == "project"


def test_project_scope_skips_when_no_repo(env):
    """Project scope with an unresolvable repo skips rather than silently
    falling back to global."""
    _make_bundle_skill(env, "proj", "alpha")
    [skill] = si.bundle_skills("proj")
    res = si.install_skill(skill, "claude_code", project_key="ghost", scope="project")
    assert res.action == "skipped_no_dir"
    assert "no resolvable repo" in res.reason


def test_two_projects_same_slug_no_collision_under_project_scope(env):
    """The global, bare-slug scheme let two projects' same-slug skills collide.
    Project scope puts each in its own repo, so both survive."""
    _make_bundle_skill(env, "proj_a", "shared")
    _make_bundle_skill(env, "proj_b", "shared")
    repo_a = env / "repo_a"; repo_a.mkdir()
    repo_b = env / "repo_b"; repo_b.mkdir()
    [a] = si.bundle_skills("proj_a")
    [b] = si.bundle_skills("proj_b")
    si.install_skill(a, "claude_code", project_key="proj_a", scope="project", repo=repo_a)
    si.install_skill(b, "claude_code", project_key="proj_b", scope="project", repo=repo_b)
    assert (repo_a / ".claude" / "skills" / "shared").resolve() == a.skill_dir.resolve()
    assert (repo_b / ".claude" / "skills" / "shared").resolve() == b.skill_dir.resolve()


def test_uninstall_finds_project_scoped_link(env):
    _make_bundle_skill(env, "proj", "alpha")
    [skill] = si.bundle_skills("proj")
    repo = env / "repo"; repo.mkdir()
    si.install_skill(skill, "claude_code", project_key="proj", scope="project", repo=repo)
    res = si.uninstall_skill("alpha", "claude_code", project_key="proj")
    assert res.action == "uninstalled"
    assert not (repo / ".claude" / "skills" / "alpha").exists()
    assert si.installed_targets("proj") == []


def test_migrate_moves_global_link_into_repo(env, monkeypatch):
    """migrate_to_project_scope() relocates an existing global link into the
    project's repo and drops the global one."""
    _make_bundle_skill(env, "proj", "alpha")
    [skill] = si.bundle_skills("proj")
    # start in the old global location
    si.install_skill(skill, "claude_code", project_key="proj", scope="global")
    assert (env / "claude" / "skills" / "alpha").is_symlink()
    # make the project's repo resolvable
    repo = env / "repo"; repo.mkdir()
    monkeypatch.setattr(si, "_project_repo", lambda pk: repo if pk == "proj" else None)

    results = si.migrate_to_project_scope()
    assert any(r.action == "migrated" for r in results)
    assert (repo / ".claude" / "skills" / "alpha").is_symlink()      # moved in
    assert not (env / "claude" / "skills" / "alpha").exists()        # old gone
    entries = si.installed_targets("proj")
    assert len(entries) == 1 and entries[0]["scope"] == "project"


def test_migrate_sweeps_unmanifested_global_link(env, monkeypatch):
    """Migration is robust to manifest loss: a global symlink into BUNDLES_DIR
    with no manifest entry is still discovered (project/slug inferred from the
    target path) and relocated."""
    _make_bundle_skill(env, "proj", "alpha")
    [skill] = si.bundle_skills("proj")
    si.install_skill(skill, "claude_code", project_key="proj", scope="global")
    si.MANIFEST_PATH.unlink()  # wipe the manifest — link is now unrecorded
    repo = env / "repo"; repo.mkdir()
    monkeypatch.setattr(si, "_project_repo", lambda pk: repo if pk == "proj" else None)

    results = si.migrate_to_project_scope()
    assert any(r.action == "migrated" for r in results)
    assert (repo / ".claude" / "skills" / "alpha").is_symlink()
    assert not (env / "claude" / "skills" / "alpha").exists()


def test_migrate_leaves_global_when_repo_unresolvable(env, monkeypatch):
    _make_bundle_skill(env, "proj", "alpha")
    [skill] = si.bundle_skills("proj")
    si.install_skill(skill, "claude_code", project_key="proj", scope="global")
    monkeypatch.setattr(si, "_project_repo", lambda pk: None)
    results = si.migrate_to_project_scope()
    assert all(r.action != "migrated" for r in results)
    assert (env / "claude" / "skills" / "alpha").is_symlink()  # untouched


def test_uninstall_preserves_user_content_at_stale_manifest_path(env):
    """Data-safety: a stale manifest row must NOT let uninstall delete user
    content that replaced watchmen's old symlink at the same path."""
    _make_bundle_skill(env, "proj", "alpha")
    [skill] = si.bundle_skills("proj")
    si.install_skill(skill, "claude_code", project_key="proj", scope="global")
    target = env / "claude" / "skills" / "alpha"
    assert target.is_symlink()
    # user swaps in their own real dir but the manifest row lingers
    target.unlink()
    target.mkdir()
    (target / "SKILL.md").write_text("mine\n", encoding="utf-8")
    res = si.uninstall_skill("alpha", "claude_code")
    assert res.action == "skipped_conflict"
    assert target.is_dir() and not target.is_symlink()
    assert (target / "SKILL.md").read_text() == "mine\n"


def test_migrate_preserves_user_content_at_stale_manifest_path(env, monkeypatch):
    """Same guarantee under migration: user content at a manifested global path
    is never removed."""
    _make_bundle_skill(env, "proj", "alpha")
    [skill] = si.bundle_skills("proj")
    si.install_skill(skill, "claude_code", project_key="proj", scope="global")
    target = env / "claude" / "skills" / "alpha"
    target.unlink()
    target.mkdir()
    (target / "SKILL.md").write_text("mine\n", encoding="utf-8")
    repo = env / "repo"; repo.mkdir()
    monkeypatch.setattr(si, "_project_repo", lambda pk: repo if pk == "proj" else None)
    si.migrate_to_project_scope()
    # user content untouched
    assert target.is_dir() and (target / "SKILL.md").read_text() == "mine\n"


def test_migrate_ignores_symlink_to_skill_subpath(env, monkeypatch):
    """The filesystem sweep only treats <key>/skills/<slug> as a candidate, not
    a symlink pointing at a sub-path inside a skill."""
    src = _make_bundle_skill(env, "proj", "alpha")
    claude = env / "claude" / "skills"
    claude.mkdir(parents=True)
    # a stray symlink to the skill's scripts/ subdir
    (claude / "scripts").symlink_to((src / "scripts").resolve(), target_is_directory=True)
    repo = env / "repo"; repo.mkdir()
    monkeypatch.setattr(si, "_project_repo", lambda pk: repo if pk == "proj" else None)
    results = si.migrate_to_project_scope()
    assert all(r.slug != "scripts" for r in results)
    assert not (repo / ".claude" / "skills" / "scripts").exists()
    assert (claude / "scripts").is_symlink()  # left alone


def test_managed_detection_survives_manifest_loss(env):
    """A symlink pointing into BUNDLES_DIR is treated as watchmen-managed even
    if the manifest is gone — so a reinstall replaces rather than skips."""
    _make_bundle_skill(env, "proj", "alpha")
    [skill] = si.bundle_skills("proj")
    si.install_skill(skill, "claude_code", project_key="proj")
    # simulate manifest loss
    si.MANIFEST_PATH.unlink()
    res = si.install_skill(skill, "claude_code", project_key="proj")
    assert res.action == "replaced"
