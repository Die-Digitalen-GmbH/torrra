from unittest.mock import MagicMock

from torrra.core.download import DownloadManager


def test_leechers_exclude_connected_seeds():
    manager = DownloadManager.__new__(DownloadManager)
    handle = MagicMock()
    handle.status.return_value = MagicMock(
        num_seeds=3, num_peers=10, progress=0.5, flags=0
    )
    manager.torrents = {"magnet": handle}

    status = manager.get_torrent_status("magnet")

    assert status is not None
    assert status["seeders"] == 3
    assert status["leechers"] == 7
