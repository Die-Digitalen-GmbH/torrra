import re
import subprocess
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        check=True,
        text=True,
        timeout=2,
    ).stdout.strip()


def checkout_version(root: Path) -> str | None:
    """Version a git checkout from its commits, or None if root isn't one.

    An editable install only refreshes its metadata on reinstall, so a checkout
    can't rely on it. The commit count is the patch number, which makes each
    commit a new version without anyone editing pyproject.toml; major and minor
    still come from there. "+dirty" marks uncommitted changes.
    """
    try:
        found = re.search(
            r'^version = "(\d+\.\d+)\.', (root / "pyproject.toml").read_text(), re.M
        )
        if not found or not (root / ".git").exists():
            return None
        count = _git(root, "rev-list", "--count", "HEAD")
        dirty = "+dirty" if _git(root, "status", "--porcelain") else ""
    except (OSError, subprocess.SubprocessError):
        return None
    return f"{found[1]}.{count}{dirty}"


def checkout_root(module: Path) -> Path | None:
    """Find the checkout a module of this package sits in, by the source layout.

    Anywhere else the folder above it isn't ours to trust: a one-file build
    unpacks under the shared temp dir, and git runs commands a repo's config names.
    """
    parents = module.resolve().parents
    return parents[2] if parents[1].name == "src" else None


def _resolve_version() -> str:
    root = checkout_root(Path(__file__))
    if root and (checkout := checkout_version(root)):
        return checkout
    try:
        return version("torrra")
    except PackageNotFoundError:
        return "dev"


__version__ = _resolve_version()
