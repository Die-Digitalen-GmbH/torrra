import hashlib
from functools import lru_cache
from pathlib import Path

import libtorrent as lt

from torrra._types import TorrentStatus
from torrra.core.config import get_config
from torrra.core.db import DB_DIR

RESUME_DATA_DIR = DB_DIR / "resume_data"


@lru_cache
def get_download_manager() -> "DownloadManager":
    return DownloadManager()


class DownloadManager:
    _STATE_MAP: dict[lt.torrent_status.states, tuple[str, str]] = {
        lt.torrent_status.states.downloading: ("Downloading", "DL"),
        lt.torrent_status.states.seeding: ("Seeding", "SE"),
        lt.torrent_status.states.finished: ("Completed", "CD"),
        lt.torrent_status.states.downloading_metadata: ("Fetching", "FE"),
    }

    def __init__(self) -> None:
        settings = {"listen_interfaces": "0.0.0.0:6881"}

        self.session: lt.session = lt.session(settings)
        self.torrents: dict[str, lt.torrent_handle] = {}
        self._metadata_updated: set[str] = (
            set()
        )  # Track torrents whose metadata has been updated

    @staticmethod
    def _resume_data_path(magnet_uri: str) -> Path:
        uri_hash = hashlib.sha1(magnet_uri.encode()).hexdigest()
        return RESUME_DATA_DIR / f"{uri_hash}.fastresume"

    def add_torrent(self, magnet_uri: str, is_paused: bool = False) -> None:
        if magnet_uri in self.torrents:
            # Torrent already exists, update paused state if needed
            handle = self.torrents[magnet_uri]
            if not handle.is_valid():
                # If handle is invalid, remove it and add the torrent fresh
                del self.torrents[magnet_uri]
            else:
                # Check current paused state and update if different
                current_status = handle.status()
                is_currently_paused = (
                    current_status.flags & lt.torrent_flags.paused
                ) != 0
                if is_currently_paused != is_paused:
                    handle.pause() if is_paused else handle.resume()
                return

        # Try to load resume data first
        resume_file = self._resume_data_path(magnet_uri)
        if resume_file.exists():
            try:
                atp = lt.read_resume_data(resume_file.read_bytes())
                atp.save_path = get_config().get("general.download_path")
                if is_paused:
                    atp.flags |= lt.torrent_flags.paused
                self.torrents[magnet_uri] = self.session.add_torrent(atp)
                return
            except Exception:
                resume_file.unlink(missing_ok=True)

        # Parse the magnet URI into torrent parameters (modern libtorrent 2.x API)
        atp = lt.parse_magnet_uri(magnet_uri)
        atp.save_path = get_config().get("general.download_path")
        if is_paused:
            atp.flags |= lt.torrent_flags.paused

        # Add the torrent to the session and start tracking
        self.torrents[magnet_uri] = self.session.add_torrent(atp)

    def remove_torrent(self, magnet_uri: str) -> None:
        handle = self.torrents.get(magnet_uri)
        if handle and handle.is_valid():
            self.session.remove_torrent(handle)
            del self.torrents[magnet_uri]
        self._resume_data_path(magnet_uri).unlink(missing_ok=True)

    def toggle_pause(self, magnet_uri: str) -> None:
        handle = self.torrents.get(magnet_uri)
        if not handle or not handle.is_valid():
            return

        status = handle.status()
        if (status.flags & lt.torrent_flags.paused) != 0:
            handle.resume()
        else:  # if not paused
            handle.pause()

    def get_torrent_status(self, magnet_uri: str) -> TorrentStatus | None:
        handle = self.torrents.get(magnet_uri)
        if not handle or not handle.is_valid():
            return None

        s = handle.status()
        return TorrentStatus(
            state=s.state,
            progress=s.progress * 100,
            down_speed=s.download_rate,
            up_speed=s.upload_rate,
            seeders=s.num_seeds,
            leechers=s.num_peers,
            is_paused=(s.flags & lt.torrent_flags.paused) != 0,
        )

    def get_torrent_state_text(self, status: TorrentStatus, short: bool = False) -> str:
        if status["is_paused"]:
            return "Paused" if not short else "PD"

        idx = 1 if short else 0
        return self._STATE_MAP.get(status["state"], ("N/A", "N/A"))[idx]

    def get_torrent_files(self, magnet_uri: str) -> list[str]:
        """Get list of file paths for a torrent."""
        handle = self.torrents.get(magnet_uri)
        if not handle or not handle.is_valid() or not handle.has_metadata():
            return []

        torrent_info = handle.torrent_file()
        if not torrent_info:
            return []

        save_path = handle.status().save_path
        files = []
        file_storage = torrent_info.files()
        for i in range(file_storage.num_files()):
            file_path = file_storage.file_path(i)
            full_path = str(Path(save_path) / file_path)
            files.append(full_path)
        return files

    def check_metadata_updates(self) -> None:
        from torrra.core.torrent import get_torrent_manager

        tm = get_torrent_manager()

        for magnet_uri, handle in self.torrents.items():
            # Only check for metadata if we haven't updated it yet
            if (
                magnet_uri not in self._metadata_updated
                and handle.is_valid()
                and handle.has_metadata()
            ):
                # Get the torrent info
                try:
                    torrent_info = handle.torrent_file()
                    if torrent_info:
                        title = torrent_info.name()
                        size = torrent_info.total_size()

                        # Update the database with the actual metadata
                        tm.update_torrent_metadata(magnet_uri, title, size)
                        # Mark this torrent as having its metadata updated
                        self._metadata_updated.add(magnet_uri)
                except (AttributeError, RuntimeError):
                    # Skip if metadata is not fully available yet
                    continue

    def enforce_seeding_policy(self) -> None:
        """Pause completed torrents if disable_seeding is enabled."""
        if not get_config().get("general.disable_seeding", False):
            return

        for handle in self.torrents.values():
            if not handle.is_valid():
                continue

            status = handle.status()
            is_paused = (status.flags & lt.torrent_flags.paused) != 0
            is_seeding = status.state in (
                lt.torrent_status.states.seeding,
                lt.torrent_status.states.finished,
            )

            if is_seeding and not is_paused:
                handle.pause()

    def save_all_resume_data(self) -> None:
        """Save resume data for all torrents to disk."""
        RESUME_DATA_DIR.mkdir(parents=True, exist_ok=True)

        for magnet_uri, handle in self.torrents.items():
            if not handle.is_valid() or not handle.has_metadata():
                continue
            try:
                handle.save_resume_data()
            except Exception:
                continue

        # Collect alerts to write resume data files
        import time

        deadline = time.monotonic() + 5
        pending = {uri for uri, h in self.torrents.items() if h.is_valid() and h.has_metadata()}

        while pending and time.monotonic() < deadline:
            self.session.wait_for_alert(1000)
            for alert in self.session.pop_alerts():
                if isinstance(alert, lt.save_resume_data_alert):
                    data = lt.write_resume_data_buf(alert.params)
                    # Find the magnet_uri for this handle
                    for uri, h in self.torrents.items():
                        if h == alert.handle:
                            self._resume_data_path(uri).write_bytes(data)
                            pending.discard(uri)
                            break
                elif isinstance(alert, lt.save_resume_data_failed_alert):
                    for uri, h in self.torrents.items():
                        if h == alert.handle:
                            pending.discard(uri)
                            break
