"""HTTP-level tests for the viewer's install/uninstall skill endpoints."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from watchmen import skill_install as si
from watchmen.viewer import server


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    bundles = tmp_path / "bundles"
    claude = tmp_path / "claude" / "skills"
    codex = tmp_path / "codex" / "skills"
    bundles.mkdir(parents=True)
    # Skill-install module dirs
    monkeypatch.setattr(si, "BUNDLES_DIR", bundles)
    monkeypatch.setattr(si, "MANIFEST_PATH", tmp_path / "install_manifest.json")
    monkeypatch.setattr(si, "HARNESS_SKILL_DIRS", {"claude_code": claude, "codex": codex})
    # The server resolves bundle dirs via its own BUNDLES constant
    monkeypatch.setattr(server, "BUNDLES", bundles)
    # Install defaults to project scope; pin the project's repo deterministically
    # so the test doesn't depend on the real state.db.
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(si, "_project_repo", lambda pk: repo if pk == "proj" else None)
    # Seed one curated skill
    skill_dir = bundles / "proj" / "skills" / "alpha"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: alpha\ndescription: x\n---\nbody\n", encoding="utf-8")
    return TestClient(server.app), tmp_path


def test_install_endpoint_creates_symlink_and_redirects(client):
    tc, tmp_path = client
    resp = tc.post("/p/proj/skills/alpha/install", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/p/proj/skills/alpha"
    assert (tmp_path / "repo" / ".claude" / "skills" / "alpha").is_symlink()
    assert (tmp_path / "repo" / ".codex" / "skills" / "alpha").is_symlink()


def test_uninstall_endpoint_removes_symlink(client):
    tc, tmp_path = client
    tc.post("/p/proj/skills/alpha/install", follow_redirects=False)
    resp = tc.post("/p/proj/skills/alpha/uninstall", follow_redirects=False)
    assert resp.status_code == 303
    assert not (tmp_path / "repo" / ".claude" / "skills" / "alpha").exists()
    assert not (tmp_path / "repo" / ".codex" / "skills" / "alpha").exists()


def test_install_unknown_skill_404(client):
    tc, _ = client
    resp = tc.post("/p/proj/skills/ghost/install", follow_redirects=False)
    assert resp.status_code == 404


def test_install_status_reflected_in_get_skill_status(client):
    tc, _ = client
    before = server.get_skill_status("proj", "alpha")
    assert before["is_installed"] is False
    tc.post("/p/proj/skills/alpha/install", follow_redirects=False)
    after = server.get_skill_status("proj", "alpha")
    assert after["is_installed"] is True
    assert set(after["installed_harnesses"]) == {"claude_code", "codex"}
