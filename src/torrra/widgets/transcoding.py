from pathlib import Path
from typing import cast

from textual.app import ComposeResult
from textual.containers import Vertical
from typing_extensions import override

from torrra.core.transcoder import TranscodeJob, TranscodeManager, get_transcode_manager
from torrra.widgets.data_table import AutoResizingDataTable
from torrra.widgets.details_panel import DetailsPanel


class TranscodingContent(Vertical):
    COLS: list[tuple[str, str, int]] = [
        ("No", "no_col", 2),
        ("Source", "source", 20),
        ("Output", "output", 6),
        ("Status", "status", 10),
        ("Progress", "progress", 8),
    ]

    STATUS_DISPLAY = {
        "pending": "Pending",
        "in_progress": "Running",
        "completed": "Done",
        "failed": "Failed",
        "cancelled": "Cancelled",
    }

    def __init__(self) -> None:
        super().__init__(id="transcoding_content")
        self._jobs: list[TranscodeJob] = []
        self._selected_job: TranscodeJob | None = None

        self._tm: TranscodeManager = get_transcode_manager()

        self._table: AutoResizingDataTable[str]
        self._details_panel: DetailsPanel

    @override
    def compose(self) -> ComposeResult:
        yield AutoResizingDataTable(cursor_type="row")
        yield DetailsPanel(show_progress_bar=True)

    def on_mount(self) -> None:
        self._table = self.query_one(AutoResizingDataTable)
        self._table.expand_col = "source"

        self._details_panel = self.query_one(DetailsPanel)
        self._details_panel.border_title = "details"

        # setup table
        for label, key, width in self.COLS:
            self._table.add_column(label, width=width, key=key)

    def on_show(self) -> None:
        self._refresh_jobs()

    def _refresh_jobs(self) -> None:
        """Refresh the jobs list from database."""
        self._jobs = self._tm.get_all_jobs()
        self._table.clear()
        self._table.border_title = f"transcoding ({len(self._jobs)})"

        for idx, job in enumerate(self._jobs):
            source_name = Path(job["source_file"]).name
            output_format = Path(job["destination_file"] or "").suffix.lstrip(".")
            status_display = self.STATUS_DISPLAY.get(job["status"], job["status"])
            progress = (
                f"{int(job['progress'])}%" if job["status"] == "in_progress" else "-"
            )

            if job["status"] == "completed":
                progress = "100%"

            self._table.add_row(
                str(idx + 1),
                source_name,
                output_format.upper(),
                status_display,
                progress,
                key=str(job["id"]),
            )

    def key_c(self) -> None:
        """Cancel selected job."""
        if not self._selected_job:
            return

        job = self._selected_job
        if job["status"] in ("pending", "in_progress"):
            self._tm.cancel_job(job["id"])
            self.notify(
                f"Cancelled transcoding of [b]{Path(job['source_file']).name}[/b]",
                title="Transcode Cancelled",
            )
            self._refresh_jobs()
            self._details_panel.add_class("hidden")
            self._selected_job = None

    def key_d(self) -> None:
        """Remove selected job from list."""
        if not self._selected_job:
            return

        job = self._selected_job
        self._tm.remove_job(job["id"])
        self.notify(
            f"Removed [b]{Path(job['source_file']).name}[/b] from list",
            title="Job Removed",
        )
        self._refresh_jobs()
        self._details_panel.add_class("hidden")
        self._selected_job = None

    def on_details_panel_closed(self) -> None:
        self._selected_job = None

    def on_data_table_row_selected(
        self, event: AutoResizingDataTable.RowSelected
    ) -> None:
        row_key = cast(str, event.row_key.value)
        job_id = int(row_key)
        self._selected_job = next((j for j in self._jobs if j["id"] == job_id), None)

        if self._selected_job:
            self._update_details_panel(self._selected_job)
            self._details_panel.remove_class("hidden")
            self._details_panel.focus()
        else:
            self._details_panel.add_class("hidden")

    def focus_table(self) -> None:
        self._table.focus()

    def update_table_data(self) -> None:
        """Update the table with current job data."""
        # Re-fetch jobs to get updated progress
        updated_jobs = self._tm.get_all_jobs()
        job_map = {j["id"]: j for j in updated_jobs}

        # Check if job count changed - if so, full refresh
        if len(updated_jobs) != len(self._jobs):
            self._refresh_jobs()
            return

        # Update existing rows
        for job in self._jobs:
            updated = job_map.get(job["id"])
            if not updated:
                continue

            # Update local cache
            job.update(updated)

            status_display = self.STATUS_DISPLAY.get(job["status"], job["status"])
            progress = (
                f"{int(job['progress'])}%" if job["status"] == "in_progress" else "-"
            )

            if job["status"] == "completed":
                progress = "100%"

            self._table.update_cell(str(job["id"]), "status", status_display)
            self._table.update_cell(str(job["id"]), "progress", progress)

        # Update details panel if showing
        if self._selected_job:
            updated = job_map.get(self._selected_job["id"])
            if updated:
                self._selected_job.update(updated)
                self._update_details_panel(self._selected_job)

    def _update_details_panel(self, job: TranscodeJob) -> None:
        source_path = Path(job["source_file"])
        status_display = self.STATUS_DISPLAY.get(job["status"], job["status"])

        details = f"""
[b]{source_path.name}[/b]
[b]Source:[/b] {job["source_file"]}
[b]Destination:[/b] {job["destination_file"] or "N/A"}
[b]Status:[/b] {status_display}
[b]Created:[/b] {job["created_at"]}
"""
        if job["error_message"]:
            details += f"\n[b]Error:[/b] [red]{job['error_message']}[/red]"

        details += (
            "\n\n[dim]Press 'c' to cancel, 'd' to remove, or 'esc' to close.[/dim]"
        )

        progress = job["progress"] if job["status"] == "in_progress" else 0
        if job["status"] == "completed":
            progress = 100

        self._details_panel.update_content(details.strip(), progress=progress)
