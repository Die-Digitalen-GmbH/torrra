from typing import Any
from unittest.mock import MagicMock

import pytest
from textual.pilot import Pilot

from torrra._types import Torrent
from torrra.screens import welcome
from torrra.widgets import sidebar


@pytest.fixture(autouse=True)
def pinned_version(monkeypatch: pytest.MonkeyPatch):
    # the version changes with every commit, a snapshot can't hold it
    monkeypatch.setattr(welcome, "__version__", "0.0.0")
    monkeypatch.setattr(sidebar, "__version__", "0.0.0")


def test_home_screen_snapshot(
    app_factory: Any, mock_indexer: MagicMock, snap_compare: Any
):
    # return mock torrents as result
    mock_indexer.search.return_value = [
        Torrent(
            magnet_uri="magnet:?xt=urn:btih:arch_new",
            title="Arch Linux 2025.11.01",
            size=1073741824,
            seeders=850,
            leechers=50,
            source="LinuxTacker",
        ),
        Torrent(
            magnet_uri="magnet:?xt=urn:btih:arch_old",
            title="Arch Linux 2024.01.01",
            size=838860800,
            seeders=5,
            leechers=15,
            source="LinuxTacker",
        ),
    ]

    async def run_before(pilot: Pilot[Any]):
        await pilot.pause()

    app = app_factory("arch linux iso")
    app.theme = "textual-dark"  # default theme
    assert snap_compare(app, run_before=run_before)


def test_welcome_screen_snapshot(app_factory: Any, snap_compare: Any):
    async def run_before(pilot: Pilot[Any]):
        await pilot.press(*list("arch linux iso"))
        await pilot.pause()

    app = app_factory()
    app.theme = "textual-dark"  # default theme
    assert snap_compare(app, run_before=run_before)


def test_theme_selector_screen_snapshot(app_factory: Any, snap_compare: Any):
    async def run_before(pilot: Pilot[Any]):
        await pilot.press("ctrl+t")
        await pilot.pause()

    app = app_factory()
    app.theme = "textual-dark"  # default theme
    assert snap_compare(app, run_before=run_before)
