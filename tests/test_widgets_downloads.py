from typing import Any
from unittest.mock import MagicMock

import pytest
from textual.app import App, ComposeResult
from textual.coordinate import Coordinate
from typing_extensions import override

from torrra.widgets import downloads as downloads_module
from torrra.widgets.data_table import AutoResizingDataTable
from torrra.widgets.details_panel import DetailsPanel
from torrra.widgets.downloads import DownloadsContent


def _torrent(name: str) -> dict[str, Any]:
    return {
        "magnet_uri": f"magnet:?xt=urn:btih:{name}",
        "title": name,
        "size": 1024,
        "source": "mock",
        "is_paused": False,
        "is_notified": False,
    }


class DownloadsApp(App[None]):
    @override
    def compose(self) -> ComposeResult:
        yield DownloadsContent()


@pytest.fixture
def tm(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    manager = MagicMock()
    manager.get_all_torrents.return_value = [_torrent(n) for n in ("a", "b", "c")]
    monkeypatch.setattr(downloads_module, "get_torrent_manager", lambda: manager)
    # no status: the details panel keeps its content, nothing formats a mock
    download_manager = MagicMock()
    download_manager.get_torrent_status.return_value = None
    monkeypatch.setattr(
        downloads_module, "get_download_manager", lambda: download_manager
    )
    return manager


def _removed(tm: MagicMock) -> list[str]:
    return [call.args[0] for call in tm.remove_torrent.call_args_list]


async def test_repeated_delete_removes_the_row_under_the_cursor(tm: MagicMock):
    app = DownloadsApp()
    async with app.run_test() as pilot:
        await pilot.press("d", "d")

        content = app.query_one(DownloadsContent)
        assert _removed(tm) == [
            _torrent("a")["magnet_uri"],
            _torrent("b")["magnet_uri"],
        ]
        assert content._selected_torrent is not None
        assert content._selected_torrent["title"] == "c"


async def test_delete_without_waiting_for_highlight_messages(tm: MagicMock):
    app = DownloadsApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        content = app.query_one(DownloadsContent)

        # called directly, so queued messages cannot run between the deletes
        content.key_d()
        content.key_d()

        assert content.query_one(AutoResizingDataTable).row_count == 1


async def test_stale_highlight_after_deleting_last_row_keeps_selection(tm: MagicMock):
    app = DownloadsApp()
    async with app.run_test() as pilot:
        content = app.query_one(DownloadsContent)
        table = content.query_one(AutoResizingDataTable)
        table.move_cursor(row=2)
        await pilot.pause()

        # the clamp to row 1 queues a highlight for b; b is deleted before it lands
        content.key_d()
        content.key_d()
        await pilot.pause()

        assert table.cursor_coordinate == Coordinate(0, 0)
        assert content._selected_torrent is not None
        assert content._selected_torrent["title"] == "a"
        assert not content.query_one(DetailsPanel).has_class("hidden")


async def test_deleting_every_row_clears_selection_and_hides_panel(tm: MagicMock):
    app = DownloadsApp()
    async with app.run_test() as pilot:
        await pilot.press("d", "d", "d", "d")

        content = app.query_one(DownloadsContent)
        assert len(_removed(tm)) == 3
        assert content._selected_torrent is None
        assert content.query_one(DetailsPanel).has_class("hidden")


async def test_table_shows_seeders_and_leechers(tm: MagicMock):
    app = DownloadsApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        content = app.query_one(DownloadsContent)
        content._dm.get_torrent_state_text.return_value = "DL"
        magnet_uri = _torrent("a")["magnet_uri"]
        table = content.query_one(AutoResizingDataTable)
        assert table.get_cell(magnet_uri, "seeders_leechers") == "0/0"

        content.update_table_data(
            {
                magnet_uri: {
                    "state": 3,
                    "progress": 50.0,
                    "down_speed": 0,
                    "up_speed": 0,
                    "seeders": 12,
                    "leechers": 34,
                    "is_paused": False,
                }
            }
        )

        assert table.get_cell(magnet_uri, "seeders_leechers") == "12/34"
