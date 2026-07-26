"""Release preparation helper tests."""

from __future__ import annotations

import json
from pathlib import Path

from scripts import prepare_release


def _write_release_tree(root: Path) -> None:
    (root / "console-ui").mkdir()
    (root / "pyproject.toml").write_text(
        '[project]\nname = "data2agent"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    (root / "console-ui" / "package.json").write_text(
        json.dumps({"name": "ui", "version": "0.1.0"}, indent=2) + "\n",
        encoding="utf-8",
    )
    (root / "console-ui" / "package-lock.json").write_text(
        json.dumps({
            "name": "ui",
            "version": "0.1.0",
            "packages": {"": {"name": "ui", "version": "0.1.0"}},
        }, indent=2) + "\n",
        encoding="utf-8",
    )


def test_prepare_release_updates_all_version_files(tmp_path, monkeypatch):
    _write_release_tree(tmp_path)
    monkeypatch.setattr(prepare_release, "ROOT", tmp_path)

    changed = prepare_release.update_versions("1.2.3")

    assert changed == [
        "pyproject.toml",
        "console-ui/package.json",
        "console-ui/package-lock.json",
    ]
    assert 'version = "1.2.3"' in (tmp_path / "pyproject.toml").read_text(
        encoding="utf-8")
    package = json.loads((tmp_path / "console-ui" / "package.json").read_text(
        encoding="utf-8"))
    lock = json.loads((tmp_path / "console-ui" / "package-lock.json").read_text(
        encoding="utf-8"))
    assert package["version"] == "1.2.3"
    assert lock["version"] == "1.2.3"
    assert lock["packages"][""]["version"] == "1.2.3"


def test_prepare_release_main_runs_checks_without_git_by_default(tmp_path, monkeypatch):
    _write_release_tree(tmp_path)
    monkeypatch.setattr(prepare_release, "ROOT", tmp_path)
    monkeypatch.setattr(
        prepare_release, "_version_test_paths",
        lambda: ["tests/test_version_fake.py"],
    )
    calls: list[list[str]] = []

    def fake_run(cmd, *, check=True):
        calls.append(cmd)
        return None

    monkeypatch.setattr(prepare_release, "_run", fake_run)

    assert prepare_release.main(["1.2.3"]) == 0

    assert any("scripts/check_release_version.py" in cmd for cmd in calls)
    assert any("pytest" in cmd and "tests/test_version_fake.py" in cmd for cmd in calls)
    assert not any(cmd[:2] == ["git", "commit"] for cmd in calls)
    assert not any(cmd[:2] == ["git", "tag"] for cmd in calls)
