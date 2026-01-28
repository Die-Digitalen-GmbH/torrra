from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.screen import Screen
from textual.widgets import ContentSwitcher
from typing_extensions import override

from torrra._types import Indexer, TorrentStatus
from torrra.core.config import get_config
from torrra.core.download import get_download_manager
from torrra.core.torrent import get_torrent_manager
from torrra.widgets.downloads import DownloadsContent
from torrra.widgets.search import SearchContent
from torrra.widgets.sidebar import Sidebar
from torrra.widgets.transcoding import TranscodingContent


class HomeScreen(Screen[None]):
    def __init__(
        self,
        indexer: Indexer,
        search_query: str,
        use_cache: bool,
        direct_download: str | None = None,
    ):
        super().__init__()
        self.indexer: Indexer = indexer
        self.search_query: str = search_query
        self.use_cache: bool = use_cache
        self.direct_download: str | None = direct_download

        self._sidebar: Sidebar
        self._content_switcher: ContentSwitcher
        self._downloads_content: DownloadsContent
        self._transcoding_content: TranscodingContent

    @override
    def compose(self) -> ComposeResult:
        initial_content = (
            "downloads_content" if self.direct_download else "search_content"
        )

        with Horizontal(id="main_layout"):
            yield Sidebar(id="sidebar")
            with ContentSwitcher(initial=initial_content, id="content_switcher"):
                yield DownloadsContent()
                yield SearchContent(
                    indexer=self.indexer,
                    search_query=self.search_query,
                    use_cache=self.use_cache,
                )
                yield TranscodingContent()

    def on_mount(self) -> None:
        self._sidebar = self.query_one(Sidebar)
        self._sidebar.can_focus = True  # re-enable focus

        self._content_switcher = self.query_one(ContentSwitcher)
        self._downloads_content = self.query_one(DownloadsContent)
        self._transcoding_content = self.query_one(TranscodingContent)

        # start torrents in background
        tm, dm = get_torrent_manager(), get_download_manager()
        torrents = tm.get_all_torrents()
        for torrent in torrents:
            dm.add_torrent(
                torrent["magnet_uri"],
                is_paused=torrent["is_paused"],
            )

        # Handle direct download if provided
        if self.direct_download:
            import asyncio
            from torrra.utils.direct_download import handle_direct_download

            asyncio.create_task(handle_direct_download(self, str(self.direct_download)))
            # start_direct_download(self, str(self.direct_download))

        # start timer to update data on both sidebar
        # and downloads content table
        self.set_interval(1, self._update_downloads_data)
        # start timer for transcoding updates (every 2 seconds)
        self.set_interval(2, self._update_transcoding_data)

        # Set up transcoding notifications
        if get_config().get("transcoding.enabled", False):
            from torrra.core.transcoder import get_transcode_manager

            get_transcode_manager().set_notification_callback(
                self._on_transcode_notification
            )

    def on_sidebar_item_selected(self, event: Sidebar.ItemSelected) -> None:
        self.query_one(ContentSwitcher).current = event.group_id

    def on_search_content_download_requested(self) -> None:
        self.query_one(ContentSwitcher).current = "downloads_content"
        self.query_one(Sidebar).select_node_by_group_id("downloads_content")

        self._downloads_content.focus_table()

    def _update_downloads_data(self) -> None:
        dm = get_download_manager()

        # Check for metadata updates
        dm.check_metadata_updates()

        magnet_uris = list(dm.torrents.keys())

        counts = {"Downloading": 0, "Seeding": 0, "Paused": 0, "Completed": 0}
        statuses: dict[str, TorrentStatus | None] = {}

        for uri in magnet_uris:
            status = dm.get_torrent_status(uri)
            statuses[uri] = status
            if not status:
                continue

            state_text = dm.get_torrent_state_text(status)
            if state_text in ("Downloading", "Fetching"):
                counts["Downloading"] += 1
            elif state_text in counts:
                counts[state_text] += 1

        self._sidebar.update_download_counts(counts)
        # only update downloads table if it is visible
        if self._content_switcher.current == "downloads_content":
            self._downloads_content.update_table_data(statuses)

    def _on_transcode_notification(self, event: str, filename: str) -> None:
        """Handle transcoding notifications."""
        short_name = (filename[:40] + "...") if len(filename) > 40 else filename

        if event == "started":
            self.notify(
                f"Started transcoding [b]{short_name}[/b]",
                title="Transcoding Started",
            )
        elif event == "completed":
            self.notify(
                f"Finished transcoding [b]{short_name}[/b]",
                title="Transcoding Finished",
            )
        elif event == "failed":
            self.notify(
                f"Failed to transcode [b]{short_name}[/b]",
                title="Transcoding Failed",
                severity="error",
            )

    async def _update_transcoding_data(self) -> None:
        """Process transcoding queue and update UI."""
        if not get_config().get("transcoding.enabled", False):
            return

        from torrra.core.transcoder import get_transcode_manager

        tm = get_transcode_manager()

        # Process pending jobs (start new ones if capacity)
        await tm.process_queue_async()

        # Update sidebar count
        jobs = tm.get_all_jobs()
        active_count = sum(1 for j in jobs if j["status"] in ("pending", "in_progress"))
        self._sidebar.update_transcoding_count(active_count)

        # Update UI if transcoding content is visible
        if self._content_switcher.current == "transcoding_content":
            self._transcoding_content.update_table_data()
