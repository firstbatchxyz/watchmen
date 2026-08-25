"""Tests for opt-in auto-install: state schema flag + curate hook + settings parse."""

from __future__ import annotations

from pathlib import Path

import pytest

from watchmen import curate, skill_install as si, state


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    bundles = tmp_path / "bundles"
    claude = tmp_path / "claude" / "skills"
    codex = tmp_path / "codex" / "skills"
    bundles.mkdir(parents=True)
    monkeypatch.setattr(si, "BUNDLES_DIR", bundles)
    monkeypatch.setattr(si, "MANIFEST_PATH", tmp_path / "install_manifest.json")
    monkeypatch.setattr(si, "HARNESS_SKILL_DIRS", {"claude_code": claude, "codex": codex})
    monkeypatch.setattr(state, "STATE_DB", tmp_path / "state.db")
    state.init_db()
    # the project's repo must exist on disk for project-scoped install to resolve
    (tmp_path / "repo").mkdir()
    # one curated skill on disk
    sd = bundles / "proj" / "skills" / "alpha"
    sd.mkdir(parents=True)
    (sd / "SKILL.md").write_text("---\nname: alpha\ndescription: x\n---\nbody\n", encoding="utf-8")
    return tmp_path


def test_schema_has_auto_install_column(env):
    state.track_project("proj", str(env / "repo"))
    p = state.get_project("proj")
    assert p["auto_install"] == 0  # defaults off


def test_maybe_auto_install_noop_when_flag_off(env):
    state.track_project("proj", str(env / "repo"))
    curate._maybe_auto_install("proj")
    assert not (env / "repo" / ".claude" / "skills" / "alpha").exists()


def test_maybe_auto_install_installs_when_flag_on(env):
    state.track_project("proj", str(env / "repo"))
    state.update_project("proj", auto_install=1)
    curate._maybe_auto_install("proj")
    # project-scoped: links land in the repo, not the global dir
    assert (env / "repo" / ".claude" / "skills" / "alpha").is_symlink()
    assert (env / "repo" / ".codex" / "skills" / "alpha").is_symlink()
    assert not (env / "claude" / "skills" / "alpha").exists()


def test_maybe_auto_install_untracked_project_noop(env):
    # No project row at all → no crash, no install.
    curate._maybe_auto_install("ghost")
    assert not (env / "repo" / ".claude" / "skills" / "alpha").exists()


def test_settings_parse_auto_install_bool():
    from watchmen.cli import _parse_setting
    assert _parse_setting("auto_install", "true") == ("auto_install", 1)
    assert _parse_setting("auto_install", "off") == ("auto_install", 0)
    with pytest.raises(ValueError):
        _parse_setting("auto_install", "maybe")


# ─── Regression: write_changelog must thread `args` into the auto-install hook ──
# write_changelog() ends by calling _maybe_auto_install(..., force=args.auto_install).
# That call is wrapped in a bare `except Exception`, so when `args` was missing
# from write_changelog's signature the NameError was swallowed silently and
# auto-install never ran — the feature looked wired up but did nothing. These
# tests exercise the real write_changelog → _maybe_auto_install path so that
# breakage can't return unnoticed.


import argparse


def _neutralize_changelog_side_effects(monkeypatch):
    """Stub the heavy, environment-touching helpers write_changelog calls (git
    commit, ~/.watchmen state publish, FTS index) so the test stays hermetic and
    only the auto-install wiring is under test."""
    monkeypatch.setattr(curate, "_git_commit_artifacts", lambda **k: None)
    monkeypatch.setattr(curate, "_publish_watchmen_state", lambda **k: None)
    monkeypatch.setattr(curate, "_build_skill_index", lambda: None)


def test_write_changelog_force_installs_via_args(env, monkeypatch):
    """The --auto-install path: args.auto_install=True must reach the installer
    and symlink skills even when the project's opt-in flag is off. Under the old
    F821 bug this raised NameError (swallowed) and nothing installed."""
    _neutralize_changelog_side_effects(monkeypatch)
    state.track_project("proj", str(env / "repo"))  # auto_install defaults off

    out_dir = env / "bundles" / "proj"
    args = argparse.Namespace(auto_install=True)
    curate.write_changelog(out_dir, "full curator", args)

    # force=True bypasses the opt-in, so the skill must be linked into both
    # (project-scoped) repo dirs.
    assert (env / "repo" / ".claude" / "skills" / "alpha").is_symlink()
    assert (env / "repo" / ".codex" / "skills" / "alpha").is_symlink()


def test_write_changelog_propagates_args_auto_install_flag(env, monkeypatch):
    """The force value handed to _maybe_auto_install is read from args, not a
    constant — guards the exact `force=args.auto_install` wiring."""
    _neutralize_changelog_side_effects(monkeypatch)
    state.track_project("proj", str(env / "repo"))

    seen: dict[str, object] = {}

    def _spy(project_key, force=False):
        seen["project_key"] = project_key
        seen["force"] = force

    monkeypatch.setattr(curate, "_maybe_auto_install", _spy)
    out_dir = env / "bundles" / "proj"
    curate.write_changelog(out_dir, "full curator", argparse.Namespace(auto_install=True))

    # If `args` were undefined, evaluating args.auto_install would raise before
    # the call and the swallowing except would leave `seen` empty.
    assert seen == {"project_key": "proj", "force": True}


def test_write_changelog_installs_before_rebuilding_index(env, monkeypatch):
    """The index may only include installed skills, so ordering is functional."""
    monkeypatch.setattr(curate, "_git_commit_artifacts", lambda **k: None)
    monkeypatch.setattr(curate, "_publish_watchmen_state", lambda **k: None)
    events = []
    monkeypatch.setattr(curate, "_maybe_auto_install", lambda *a, **k: events.append("install"))
    monkeypatch.setattr(curate, "_build_skill_index", lambda: events.append("index"))

    out_dir = env / "bundles" / "proj"
    curate.write_changelog(out_dir, "full curator", argparse.Namespace(auto_install=True))

    assert events == ["install", "index"]


def test_write_changelog_no_install_when_flag_off(env, monkeypatch):
    """Without --auto-install and with the project opt-in off, write_changelog
    must NOT install anything (the force value is honestly False)."""
    _neutralize_changelog_side_effects(monkeypatch)
    state.track_project("proj", str(env / "repo"))

    out_dir = env / "bundles" / "proj"
    curate.write_changelog(out_dir, "full curator", argparse.Namespace(auto_install=False))

    assert not (env / "repo" / ".claude" / "skills" / "alpha").exists()
    assert not (env / "repo" / ".codex" / "skills" / "alpha").exists()
