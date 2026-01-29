import asyncio
import tempfile
from dataclasses import dataclass

import libtorrent as lt

from torrra.utils.magnet import resolve_magnet_uri


@dataclass
class TorrentFile:
    """Represents a file within a torrent."""

    path: str
    size: int


async def fetch_torrent_files(
    raw_uri: str, timeout: float = 30.0
) -> list[TorrentFile] | None:
    """
    Fetch the list of files in a torrent from its magnet URI or .torrent URL.

    Uses the existing DownloadManager session (which has an established DHT)
    to fetch metadata, then removes the torrent.

    Args:
        raw_uri: The magnet URI or .torrent URL
        timeout: Maximum time to wait for metadata (seconds)

    Returns:
        List of TorrentFile objects, or None if metadata couldn't be fetched
    """
    # Resolve .torrent URLs to magnet URIs
    magnet_uri = await resolve_magnet_uri(raw_uri)
    if not magnet_uri or not magnet_uri.startswith("magnet:"):
        return None

    from torrra.core.download import get_download_manager

    dm = get_download_manager()
    session = dm.session

    # Check if torrent is already in the session (being downloaded)
    existing_handle = dm.torrents.get(magnet_uri)
    if existing_handle and existing_handle.is_valid() and existing_handle.has_metadata():
        return _extract_files(existing_handle)

    handle = None
    try:
        # Parse and add the torrent temporarily
        atp = lt.parse_magnet_uri(magnet_uri)
        atp.save_path = tempfile.gettempdir()
        atp.flags |= lt.torrent_flags.upload_mode  # Don't download, just get metadata

        handle = session.add_torrent(atp)

        # Wait for metadata with timeout
        elapsed = 0.0
        poll_interval = 0.5
        while elapsed < timeout:
            if handle.has_metadata():
                break
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval
        else:
            # Timeout reached
            return None

        return _extract_files(handle)

    except Exception:
        return None
    finally:
        # Clean up: remove the temporary torrent (only if we added it)
        if handle is not None and magnet_uri not in dm.torrents:
            try:
                session.remove_torrent(handle, lt.session.delete_files)
            except Exception:
                pass


def _extract_files(handle: lt.torrent_handle) -> list[TorrentFile] | None:
    """Extract file list from a torrent handle."""
    torrent_info = handle.torrent_file()
    if not torrent_info:
        return None

    files = []
    file_storage = torrent_info.files()
    for i in range(file_storage.num_files()):
        files.append(
            TorrentFile(
                path=file_storage.file_path(i),
                size=file_storage.file_size(i),
            )
        )
    return files
