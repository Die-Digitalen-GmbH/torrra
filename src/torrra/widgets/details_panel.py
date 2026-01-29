from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.message import Message
from textual.widgets import ProgressBar, Static
from typing_extensions import override

from torrra.utils.helpers import human_readable_size
from torrra.utils.metadata import TorrentFile


class DetailsPanel(Vertical):
    class Closed(Message):
        """Posted when the panel is closed."""

    def __init__(self, show_progress_bar: bool = False) -> None:
        super().__init__(classes="hidden")
        self.show_progress_bar: bool = show_progress_bar
        # UI refs
        self._content_widget: Static
        self._progress_bar: ProgressBar | None = None
        self._files_container: VerticalScroll
        self._files_widget: Static

    @override
    def compose(self) -> ComposeResult:
        yield Static()
        with VerticalScroll(id="files_container", classes="hidden"):
            yield Static(id="files_content")
        if self.show_progress_bar:
            yield ProgressBar(total=100)

    def on_mount(self) -> None:
        self._content_widget = self.query_one("DetailsPanel > Static", Static)
        self._files_container = self.query_one("#files_container", VerticalScroll)
        self._files_widget = self.query_one("#files_content", Static)
        if self.show_progress_bar:
            self._progress_bar = self.query_one(ProgressBar)
        # enable focus for this widget
        self.can_focus: bool = True

    def key_escape(self) -> None:
        self.add_class("hidden")
        self.post_message(self.Closed())

    def update_content(self, content: str, progress: float | None = None) -> None:
        self._content_widget.update(content)
        if self._progress_bar and progress is not None:
            self._progress_bar.progress = progress

    def show_files_loading(self) -> None:
        """Show a loading indicator for files."""
        self._files_widget.update("[dim]Fetching files...[/dim]")
        self._files_container.remove_class("hidden")

    def update_files(self, files: list[TorrentFile]) -> None:
        """Display the list of files in the torrent."""
        if not files:
            self._files_widget.update("[dim]No files found[/dim]")
            self._files_container.remove_class("hidden")
            return

        lines = [f"[b]Files ({len(files)}):[/b]"]
        for f in files:
            size_str = human_readable_size(f.size)
            lines.append(f"  [dim]•[/dim] {f.path} [dim]({size_str})[/dim]")

        self._files_widget.update("\n".join(lines))
        self._files_container.remove_class("hidden")

    def show_files_error(self, message: str) -> None:
        """Display an error message for files fetching."""
        self._files_widget.update(f"[dim]{message}[/dim]")
        self._files_container.remove_class("hidden")

    def clear_files(self) -> None:
        """Hide the files section."""
        self._files_container.add_class("hidden")
        self._files_widget.update("")
