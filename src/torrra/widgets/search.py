import subprocess
from enum import Enum
from typing import cast

from textual import on, work
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import Input, Static
from typing_extensions import override

from torrra._types import Indexer, Torrent
from torrra.core.config import get_config
from torrra.core.constants import DEFAULT_MAX_RETRIES, DEFAULT_TIMEOUT
from torrra.core.torrent import get_torrent_manager
from torrra.indexers.base import BaseIndexer
from torrra.utils.helpers import human_readable_size, lazy_import
from torrra.utils.magnet import resolve_magnet_uri
from torrra.utils.video import detect_video_extension, is_transcodable_extension
from torrra.widgets.data_table import AutoResizingDataTable
from torrra.widgets.details_panel import DetailsPanel
from torrra.widgets.spinner import Spinner


class SortMode(Enum):
    RELEVANCY = "relevancy"
    SEEDERS = "seeders"


class SearchContent(Vertical):
    COLS: list[tuple[str, str, int]] = [
        ("No", "no_col", 2),
        ("Title", "title_col", 25),
        ("Size", "size_col", 10),
        ("S:L", "seeders_leechers_col", 6),
    ]

    class SearchResults(Message):
        def __init__(self, results: list[Torrent], query: str) -> None:
            self.results: list[Torrent] = results
            self.query: str = query
            super().__init__()

    class DownloadRequested(Message):
        def __init__(self, torrent: Torrent) -> None:
            self.torrent: Torrent = torrent
            super().__init__()

    def __init__(self, indexer: Indexer, search_query: str, use_cache: bool):
        super().__init__(id="search_content")
        self.indexer: Indexer = indexer
        self.search_query: str = search_query
        self.use_cache: bool = use_cache

        # instance-level cache
        self._indexer_instance_cache: BaseIndexer | None = None

        # application states
        self._search_results_map: dict[str, Torrent] = {}
        self._search_results_list: list[Torrent] = []  # original order from indexer
        self._selected_torrent: Torrent | None = None
        self._sort_mode: SortMode = SortMode.RELEVANCY

        # ui refs (cached later)
        self._search_input: Input
        self._table: AutoResizingDataTable[str]
        self._details_panel: DetailsPanel
        self._loader: Vertical

    @override
    def compose(self) -> ComposeResult:
        yield Input(placeholder="Search...", value=self.search_query)
        yield AutoResizingDataTable(cursor_type="row", classes="hidden")
        yield DetailsPanel()
        with Vertical(id="loader"):
            yield Static()
            yield Spinner(name="shark")

    def on_mount(self) -> None:
        self._search_input = self.query_one(Input)
        self._search_input.border_title = "search"
        self._search_input.focus()

        self._table = self.query_one(AutoResizingDataTable)
        self._table.expand_col = "title_col"
        self._table.border_title = "results"

        self._details_panel = self.query_one(DetailsPanel)
        self._details_panel.border_title = "details"

        self._loader = self.query_one("#loader", Vertical)
        # setup table
        for label, key, width in self.COLS:
            self._table.add_column(label, width=width, key=key)
        # send initial search
        self.post_message(Input.Submitted(self._search_input, self.search_query))

    def key_s(self) -> None:
        """Toggle sort mode between relevancy and seeders."""
        if not self._search_results_list:
            return

        # Toggle sort mode
        if self._sort_mode == SortMode.RELEVANCY:
            self._sort_mode = SortMode.SEEDERS
        else:
            self._sort_mode = SortMode.RELEVANCY

        self._refresh_table()

    def _get_sorted_results(self) -> list[Torrent]:
        """Return results sorted according to current sort mode."""
        if self._sort_mode == SortMode.SEEDERS:
            return sorted(
                self._search_results_list,
                key=lambda t: t.seeders,
                reverse=True,
            )
        # RELEVANCY: return original order
        return list(self._search_results_list)

    def _refresh_table(self) -> None:
        """Refresh the table with current sort mode."""
        self._table.clear()
        sorted_results = self._get_sorted_results()

        seen: set[str] = set()
        for idx, torrent in enumerate(sorted_results):
            if torrent.magnet_uri in seen:
                continue

            seen.add(torrent.magnet_uri)
            self._table.add_row(
                str(idx + 1),
                torrent.title,
                human_readable_size(torrent.size),
                f"{str(torrent.seeders)}:{str(torrent.leechers)}",
                key=torrent.magnet_uri,
            )

        # Update border title to show sort mode
        sort_label = "seeders" if self._sort_mode == SortMode.SEEDERS else "relevancy"
        self._table.border_title = (
            f"results ({len(sorted_results)}) [dim]sorted by {sort_label} (s)[/dim]"
        )

    async def key_enter(self) -> None:
        if not self._details_panel.has_focus:
            return

        if self._selected_torrent:
            # uri returned from the indexer
            # might not be a proper magnet uri, resolve anyways
            raw_magnet_uri = self._selected_torrent.magnet_uri
            resolved_magnet_uri = await resolve_magnet_uri(raw_magnet_uri)

            if resolved_magnet_uri is None:
                return

            # update with resolved magnet_uri
            self._selected_torrent.magnet_uri = resolved_magnet_uri

            config = get_config()
            if config.get("general.download_in_external_client", False):
                if config.get("general.use_transmission", False):
                    tran_user = config.get("general.transmission_user", "")
                    tran_pass = config.get("general.transmission_pass", "")

                    subprocess.run(
                        [
                            "transmission-remote",
                            "--auth",
                            tran_user + ":" + tran_pass,
                            "-a",
                            resolved_magnet_uri,
                        ],
                        capture_output=True,
                        text=True,
                    )
                    self.notify(
                        "Opened in [b]transmission-remote[/b]",
                        title="Torrent Opened",
                    )
                else:
                    self.app.open_url(resolved_magnet_uri)
                    self.notify(
                        "Opened in default magnet: handler",
                        title="Torrent Opened",
                    )
            else:  # continue with libtorrent
                tm = get_torrent_manager()
                tm.add_torrent(self._selected_torrent)
                title = self._selected_torrent.title
                short_title = (title[:30] + "...") if len(title) > 30 else title
                self.notify(
                    f"Started downloading [b]{short_title}[/b]",
                    title="Download Started",
                )
                self.post_message(self.DownloadRequested(self._selected_torrent))

    def on_input_submitted(self, event: Input.Submitted) -> None:
        query = event.value
        if not query:
            return

        self._table.add_class("hidden")
        self._table.clear()
        self._details_panel.add_class("hidden")
        self._loader.remove_class("hidden")
        cast(Spinner, self._loader.children[1]).resume()
        cast(Static, self._loader.children[0]).update(
            f"Searching for [b]{query}[/b]..."
        )

        self._perform_search(query)

    @work(exclusive=True)
    async def _perform_search(self, query: str) -> None:
        try:
            indexer = self._get_indexer_instance()
            results = await indexer.search(query, use_cache=self.use_cache)
            self.post_message(self.SearchResults(results or [], query))
        except Exception:
            self.notify(
                "Search failed, check indexer settings",
                title="Search Failed",
                severity="error",
            )  # post empty results just to stop spinners
            self.post_message(self.SearchResults([], query))

    @on(SearchResults)
    def on_search_results(self, message: SearchResults) -> None:
        if not message.results:
            cast(Spinner, self._loader.children[1]).pause()
            cast(Static, self._loader.children[0]).update(
                f"Nothing Found for [b]{message.query}[/b]"
            )  # show loader and exit
            return

        self._loader.add_class("hidden")
        self._table.remove_class("hidden")
        self._table.focus()  # initial focus table

        # Store results and build lookup map
        self._search_results_list = message.results
        self._search_results_map = {t.magnet_uri: t for t in message.results}

        # Reset to default sort mode on new search
        self._sort_mode = SortMode.RELEVANCY
        self._refresh_table()

    def on_details_panel_closed(self):
        self._selected_torrent = None
        self._table.focus()

    def on_data_table_row_selected(
        self, event: AutoResizingDataTable.RowSelected
    ) -> None:
        magnet_uri = cast(str, event.row_key.value)
        self._selected_torrent = self._search_results_map.get(magnet_uri)
        if not self._selected_torrent:
            return

        # Detect video file extension from title
        video_ext = detect_video_extension(self._selected_torrent.title)
        format_info = ""
        if video_ext:
            ext_display = video_ext.upper().lstrip(".")
            if is_transcodable_extension(video_ext):
                format_info = (
                    f" - [b]Format:[/b] {ext_display} [green](transcodable)[/green]"
                )
            else:
                format_info = f" - [b]Format:[/b] {ext_display}"

        details = f"""
[b]{self._selected_torrent.title}[/b]
[b]Size:[/b] {human_readable_size(self._selected_torrent.size)} - [b]Seeders:[/b] {self._selected_torrent.seeders} - [b]Leechers:[/b] {self._selected_torrent.leechers} - [b]Source:[/b] {self._selected_torrent.source}{format_info}

[dim]Press 'enter' to download or 'esc' to close.[/dim]
"""

        self._details_panel.update_content(details.strip())
        self._details_panel.remove_class("hidden")
        self._details_panel.focus()

    def focus_search_input(self) -> None:
        """Focus the search input field."""
        self._search_input.focus()

    def _get_indexer_instance(self) -> BaseIndexer:

        name = self.indexer.name
        indexer_cls_str = f"torrra.indexers.{name}.{name.title()}Indexer"

        indexer_cls = lazy_import(indexer_cls_str)
        assert issubclass(indexer_cls, BaseIndexer)
        indexer_instance = indexer_cls(
            url=self.indexer.url,
            api_key=self.indexer.api_key,
            timeout=get_config().get("general.timeout", DEFAULT_TIMEOUT),
            max_retries=get_config().get("general.max_retries", DEFAULT_MAX_RETRIES),
        )

        self._indexer_instance_cache = indexer_instance
        return indexer_instance
