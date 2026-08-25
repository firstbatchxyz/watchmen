"""The prompt-suggestion index must mirror skills agents can actually load."""

import sqlite3
from pathlib import Path

from watchmen import curate, skill_install as si, state


def _skill(root: Path, slug: str, trigger: str) -> None:
    skill_dir = root / "proj" / "skills" / slug
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        f"name: {slug}\n"
        f"description: {slug} helper\n"
        f"when_to_use: {trigger}\n"
        "when_not_to_use: unrelated requests\n"
        "---\nbody\n",
        encoding="utf-8",
    )


def test_index_contains_only_installed_skills(tmp_path, monkeypatch):
    bundles = tmp_path / "bundles"
    repo = tmp_path / "repo"
    repo.mkdir()
    _skill(bundles, "installed", "deploy railway service")
    _skill(bundles, "bundle-only", "review legal documents")

    monkeypatch.setattr(curate, "BUNDLES_DIR", bundles)
    monkeypatch.setattr(curate, "WATCHMEN_HOME", tmp_path)
    monkeypatch.setattr(si, "BUNDLES_DIR", bundles)
    monkeypatch.setattr(si, "MANIFEST_PATH", tmp_path / "install_manifest.json")
    monkeypatch.setattr(si, "HARNESS_SKILL_DIRS", {"claude_code": tmp_path / "claude-skills"})
    monkeypatch.setattr(state, "STATE_DB", tmp_path / "state.db")

    state.init_db()
    state.track_project("proj", str(repo))
    [bundle_skill] = [s for s in si.bundle_skills("proj") if s.slug == "installed"]
    si.install_skill(bundle_skill, "claude_code", project_key="proj")

    curate._build_skill_index()

    with sqlite3.connect(tmp_path / "skill_index.db") as conn:
        rows = conn.execute(
            "SELECT project_key, skill_slug FROM skill_match ORDER BY skill_slug"
        ).fetchall()
    assert rows == [("proj", "installed")]
