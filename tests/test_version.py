import subprocess
from pathlib import Path

from torrra._version import checkout_version


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=root,
        check=True,
        capture_output=True,
    )


def _checkout(root: Path) -> Path:
    (root / "pyproject.toml").write_text('[project]\nversion = "2.1.4"\n')
    _git(root, "init")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "first")
    return root


def test_each_commit_is_a_new_version(tmp_path: Path):
    root = _checkout(tmp_path)
    assert checkout_version(root) == "2.1.1"

    _git(root, "commit", "--allow-empty", "-m", "second")

    assert checkout_version(root) == "2.1.2"


def test_uncommitted_changes_are_marked(tmp_path: Path):
    root = _checkout(tmp_path)

    (root / "new.py").touch()

    assert checkout_version(root) == "2.1.1+dirty"


def test_not_a_checkout(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text('version = "2.1.4"\n')

    assert checkout_version(tmp_path) is None


def test_no_pyproject(tmp_path: Path):
    assert checkout_version(tmp_path) is None
