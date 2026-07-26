"""Release version consistency checks."""

from __future__ import annotations

import data2agent
from scripts import check_release_version


def test_data2agent_version_comes_from_project_metadata():
    assert data2agent.__version__ == check_release_version._project_version()


def test_release_version_check_accepts_matching_tag():
    current = check_release_version._project_version()
    assert check_release_version.main(["--tag", f"v{current}"]) == 0


def test_release_version_check_rejects_mismatched_tag(capsys):
    assert check_release_version.main(["--tag", "v999.0.0"]) == 1
    err = capsys.readouterr().err
    assert "tag v999.0.0" in err
    assert "999.0.0" in err
