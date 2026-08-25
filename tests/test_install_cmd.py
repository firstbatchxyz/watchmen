"""Tests for the `watchmen install` command dispatch (commands/install.py)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from watchmen import skill_install as si
from watchmen.commands import install as cmd


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    bundles = tmp_path / "bundles"
    claude = tmp_path / "claude" / "skills"
    codex = tmp_path / "codex" / "skills"
    bundles.mkdir(parents=True)
    monkeypatch.setattr(si, "BUNDLES_DIR", bundles)
    monkeypatch.setattr(si, "MANIFEST_PATH", tmp_path / "install_manifest.json")
    monkeypatch.setattr(si, "HARNESS_SKILL_DIRS", {"claude_code": claude, "codex": codex})
    monkeypatch.setattr(si, "refresh_skill_index", lambda: None)
    return tmp_path


def _bundle(env: Path, project: str, slug: str):
    d = env / "bundles" / project / "skills" / slug
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\nname: {slug}\ndescription: x\n---\nbody\n", encoding="utf-8")


def _args(project, **kw):
    # Default these dispatch tests to global scope: it exercises the command
    # plumbing without depending on `state` to resolve a repo. Project-scope
    # behaviour has its own test below and full coverage in test_skill_install.
    base = {"skill": [], "harness": [], "force": False, "uninstall": False,
            "list": False, "scope": "global", "migrate": False}
    base.update(kw)
    return SimpleNamespace(project=project, **base)


def test_install_no_skills_returns_1(env, capsys):
    rc = cmd.cmd_install(_args("ghost"))
    assert rc == 1
    assert "no curated skills" in capsys.readouterr().out


def test_install_all(env):
    _bundle(env, "proj", "alpha")
    _bundle(env, "proj", "beta")
    rc = cmd.cmd_install(_args("proj"))
    assert rc == 0
    assert (env / "claude" / "skills" / "alpha").is_symlink()
    assert (env / "codex" / "skills" / "beta").is_symlink()


def test_install_slug_and_harness_filter(env):
    _bundle(env, "proj", "alpha")
    _bundle(env, "proj", "beta")
    rc = cmd.cmd_install(_args("proj", skill=["alpha"], harness=["claude-code"]))
    assert rc == 0
    assert (env / "claude" / "skills" / "alpha").is_symlink()
    assert not (env / "codex" / "skills" / "alpha").exists()
    assert not (env / "claude" / "skills" / "beta").exists()


def test_install_and_uninstall_refresh_suggestion_index(env, monkeypatch):
    _bundle(env, "proj", "alpha")
    refreshes = []
    monkeypatch.setattr(si, "refresh_skill_index", lambda: refreshes.append("refresh"))

    assert cmd.cmd_install(_args("proj")) == 0
    assert refreshes == ["refresh"]
    assert cmd.cmd_install(_args("proj", uninstall=True)) == 0
    assert refreshes == ["refresh", "refresh"]


def test_install_conflict_skipped_message(env, capsys):
    _bundle(env, "proj", "alpha")
    user = env / "claude" / "skills" / "alpha"
    user.mkdir(parents=True)
    (user / "SKILL.md").write_text("mine\n", encoding="utf-8")
    cmd.cmd_install(_args("proj", harness=["claude-code"]))
    out = capsys.readouterr().out
    assert "skipped_conflict" in out
    assert user.is_dir() and not user.is_symlink()


def test_list_mode_changes_nothing(env, capsys):
    _bundle(env, "proj", "alpha")
    rc = cmd.cmd_install(_args("proj", list=True))
    assert rc == 0
    assert not (env / "claude" / "skills" / "alpha").exists()
    assert "1 curated skills" in capsys.readouterr().out


def test_uninstall_removes_links(env):
    _bundle(env, "proj", "alpha")
    cmd.cmd_install(_args("proj"))
    assert (env / "claude" / "skills" / "alpha").is_symlink()
    rc = cmd.cmd_install(_args("proj", uninstall=True))
    assert rc == 0
    assert not (env / "claude" / "skills" / "alpha").exists()
    assert not (env / "codex" / "skills" / "alpha").exists()


def test_install_project_scope_default_lands_in_repo(env, capsys, monkeypatch):
    """The default scope links into the project's own repo, and prints the
    (non-forcing) .gitignore heads-up."""
    _bundle(env, "proj", "alpha")
    repo = env / "repo"
    repo.mkdir()
    monkeypatch.setattr(si, "_project_repo", lambda pk: repo if pk == "proj" else None)
    rc = cmd.cmd_install(_args("proj", scope="project"))
    assert rc == 0
    assert (repo / ".claude" / "skills" / "alpha").is_symlink()
    assert not (env / "claude" / "skills" / "alpha").exists()
    out = capsys.readouterr().out
    assert "scope=project" in out
    assert ".gitignore" in out  # heads-up only; we never write it


def test_migrate_flag_invokes_migration(env, capsys, monkeypatch):
    called = {}

    def _fake_migrate():
        called["ran"] = True
        return []

    monkeypatch.setattr(si, "migrate_to_project_scope", _fake_migrate)
    rc = cmd.cmd_install(_args(None, migrate=True))
    assert rc == 0
    assert called.get("ran") is True
