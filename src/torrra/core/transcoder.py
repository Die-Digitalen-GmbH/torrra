from __future__ import annotations

import asyncio
import os
import subprocess
from collections.abc import Awaitable, Callable
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypedDict

from torrra.core.config import get_config
from torrra.core.db import get_db_connection

if TYPE_CHECKING:
    pass


class TranscodeRule(TypedDict):
    """A transcoding rule from config."""

    input_extension: str
    output_format: str
    resolution: str  # "original", "720p", "1080p", "4k"


class TranscodeJob(TypedDict):
    """A transcoding job record."""

    id: int
    magnet_uri: str
    source_file: str
    destination_file: str | None
    status: str  # pending, in_progress, completed, failed
    progress: float
    error_message: str | None
    created_at: str


@lru_cache
def get_transcode_manager() -> TranscodeManager:
    return TranscodeManager()


def check_ffmpeg_available() -> bool:
    """Check if ffmpeg is available."""
    ffmpeg_path = get_config().get("transcoding.ffmpeg_path", "ffmpeg")
    try:
        result = subprocess.run(
            [ffmpeg_path, "-version"],
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


class TranscodeManager:
    """Manages transcoding operations using ffmpeg."""

    def __init__(self) -> None:
        self._active_processes: dict[int, asyncio.subprocess.Process] = {}
        self._total_durations: dict[int, float] = {}
        # Callbacks for notifications: (event, filename)
        # Events: "started", "completed", "failed"
        self._notification_callback: (
            Callable[[str, str], None] | Callable[[str, str], Awaitable[None]] | None
        ) = None

    def set_notification_callback(
        self,
        callback: Callable[[str, str], None] | Callable[[str, str], Awaitable[None]],
    ) -> None:
        """Set a callback for transcoding notifications.

        Callback receives (event, filename) where event is "started", "completed", or "failed".
        """
        self._notification_callback = callback

    async def _notify(self, event: str, filename: str) -> None:
        """Send a notification via the callback if set."""
        if self._notification_callback:
            result = self._notification_callback(event, filename)
            if asyncio.iscoroutine(result):
                await result

    def get_rules(self) -> list[TranscodeRule]:
        """Get all transcoding rules from config."""
        rules = get_config().get("transcoding.rules", [])
        if not isinstance(rules, list):
            return []
        return rules

    def get_matching_rule(self, file_path: str) -> TranscodeRule | None:
        """Find a transcoding rule matching the file extension."""
        ext = Path(file_path).suffix.lower()
        for rule in self.get_rules():
            rule_ext = rule.get("input_extension", "")
            # Normalize extension (handle with or without leading dot)
            if not rule_ext.startswith("."):
                rule_ext = f".{rule_ext}"
            if ext == rule_ext.lower():
                return rule
        return None

    def get_destination_path(self) -> str:
        """Get the destination path for transcoded files."""
        dest = get_config().get("transcoding.destination_path", "")
        if dest:
            return dest
        return get_config().get("general.download_path")

    def queue_job(self, magnet_uri: str, source_file: str) -> int:
        """Add a new transcoding job to the database queue."""
        rule = self.get_matching_rule(source_file)
        if not rule:
            raise ValueError(f"No matching rule for {source_file}")

        # Build destination file path
        dest_dir = self.get_destination_path()
        source_path = Path(source_file)
        output_format = rule.get("output_format", "mp4")
        dest_file = str(Path(dest_dir) / f"{source_path.stem}.{output_format}")

        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO transcode_jobs
                    (magnet_uri, source_file, destination_file, status, progress)
                VALUES (?, ?, ?, 'pending', 0)
                """,
                (magnet_uri, source_file, dest_file),
            )
            conn.commit()
            return cursor.lastrowid or 0

    def get_all_jobs(self) -> list[TranscodeJob]:
        """Get all transcoding jobs."""
        with get_db_connection() as conn:
            conn.row_factory = _dict_factory
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, magnet_uri, source_file, destination_file,
                       status, progress, error_message, created_at
                FROM transcode_jobs
                ORDER BY created_at DESC
                """
            )
            return cursor.fetchall()

    def get_pending_jobs(self) -> list[TranscodeJob]:
        """Get all pending transcoding jobs."""
        with get_db_connection() as conn:
            conn.row_factory = _dict_factory
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, magnet_uri, source_file, destination_file,
                       status, progress, error_message, created_at
                FROM transcode_jobs
                WHERE status = 'pending'
                ORDER BY created_at ASC
                """
            )
            return cursor.fetchall()

    def get_in_progress_count(self) -> int:
        """Get the count of jobs currently in progress."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT COUNT(*) FROM transcode_jobs WHERE status = 'in_progress'"
            )
            row = cursor.fetchone()
            return row[0] if row else 0

    def update_job_status(
        self,
        job_id: int,
        status: str,
        progress: float | None = None,
        error_message: str | None = None,
    ) -> None:
        """Update a job's status."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            if progress is not None and error_message is not None:
                cursor.execute(
                    """
                    UPDATE transcode_jobs
                    SET status = ?, progress = ?, error_message = ?
                    WHERE id = ?
                    """,
                    (status, progress, error_message, job_id),
                )
            elif progress is not None:
                cursor.execute(
                    """
                    UPDATE transcode_jobs
                    SET status = ?, progress = ?
                    WHERE id = ?
                    """,
                    (status, progress, job_id),
                )
            elif error_message is not None:
                cursor.execute(
                    """
                    UPDATE transcode_jobs
                    SET status = ?, error_message = ?
                    WHERE id = ?
                    """,
                    (status, error_message, job_id),
                )
            else:
                cursor.execute(
                    "UPDATE transcode_jobs SET status = ? WHERE id = ?",
                    (status, job_id),
                )
            conn.commit()

    def update_job_progress(self, job_id: int, progress: float) -> None:
        """Update a job's progress."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE transcode_jobs SET progress = ? WHERE id = ?",
                (progress, job_id),
            )
            conn.commit()

    def cancel_job(self, job_id: int) -> None:
        """Cancel a running or pending job."""
        # If running, terminate the process
        if job_id in self._active_processes:
            process = self._active_processes[job_id]
            process.terminate()
            del self._active_processes[job_id]

        self.update_job_status(job_id, "cancelled")

    def remove_job(self, job_id: int) -> None:
        """Remove a job from the database."""
        # Cancel if running
        if job_id in self._active_processes:
            self.cancel_job(job_id)

        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM transcode_jobs WHERE id = ?", (job_id,))
            conn.commit()

    def process_completed_download(self, magnet_uri: str) -> list[int]:
        """Scan completed torrent files and queue matching transcode jobs.

        Returns list of job IDs created.
        """
        if not get_config().get("transcoding.enabled", False):
            return []

        from torrra.core.download import get_download_manager

        dm = get_download_manager()
        files = dm.get_torrent_files(magnet_uri)
        job_ids = []

        for file_path in files:
            if self.get_matching_rule(file_path):
                try:
                    job_id = self.queue_job(magnet_uri, file_path)
                    job_ids.append(job_id)
                except ValueError:
                    continue

        return job_ids

    def build_ffmpeg_command(
        self, source: str, destination: str, rule: TranscodeRule
    ) -> list[str]:
        """Build ffmpeg command based on transcoding rule."""
        ffmpeg_path = get_config().get("transcoding.ffmpeg_path", "ffmpeg")
        cmd = [
            ffmpeg_path,
            "-i",
            source,
            "-y",  # Overwrite output
            "-progress",
            "pipe:1",  # Output progress to stdout
            "-nostats",
        ]

        resolution = rule.get("resolution", "original")

        # Video codec - use libx264 for mp4/m4v compatibility
        cmd.extend(["-c:v", "libx264", "-preset", "medium", "-crf", "23"])

        # Resolution scaling
        if resolution != "original":
            height_map = {"720p": 720, "1080p": 1080, "4k": 2160}
            height = height_map.get(resolution, 1080)
            # Scale to height while preserving aspect ratio, ensure even dimensions
            cmd.extend(["-vf", f"scale=-2:{height}"])

        # Audio codec - use AAC for compatibility
        cmd.extend(["-c:a", "aac", "-b:a", "192k"])

        cmd.append(destination)
        return cmd

    def get_video_duration(self, file_path: str) -> float | None:
        """Get video duration in seconds using ffprobe."""
        ffmpeg_path = get_config().get("transcoding.ffmpeg_path", "ffmpeg")
        # ffprobe is usually alongside ffmpeg
        ffprobe_path = ffmpeg_path.replace("ffmpeg", "ffprobe")

        try:
            result = subprocess.run(
                [
                    ffprobe_path,
                    "-v",
                    "quiet",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    file_path,
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 0 and result.stdout.strip():
                return float(result.stdout.strip())
        except (FileNotFoundError, subprocess.TimeoutExpired, ValueError):
            pass
        return None

    def parse_ffmpeg_progress(self, line: str, job_id: int) -> float | None:
        """Parse ffmpeg progress output and return percentage."""
        # ffmpeg progress output has lines like: out_time_ms=12345678
        if line.startswith("out_time_ms="):
            try:
                current_ms = int(line.split("=")[1])
                total_duration = self._total_durations.get(job_id)
                if total_duration and total_duration > 0:
                    # Convert ms to seconds and calculate percentage
                    progress = (current_ms / 1000000) / total_duration * 100
                    return min(progress, 99.9)  # Cap at 99.9 until complete
            except (ValueError, IndexError):
                pass
        return None

    async def start_job_async(self, job_id: int, job: TranscodeJob) -> None:
        """Start transcoding a specific job asynchronously."""
        source = job["source_file"]
        destination = job["destination_file"]

        if not destination:
            self.update_job_status(
                job_id, "failed", error_message="No destination file"
            )
            return

        # Check source file exists
        if not os.path.exists(source):
            self.update_job_status(
                job_id, "failed", error_message=f"Source file not found: {source}"
            )
            return

        rule = self.get_matching_rule(source)
        if not rule:
            self.update_job_status(
                job_id, "failed", error_message="No matching transcoding rule"
            )
            return

        # Get video duration for progress calculation
        duration = self.get_video_duration(source)
        if duration:
            self._total_durations[job_id] = duration

        # Ensure destination directory exists
        dest_dir = Path(destination).parent
        dest_dir.mkdir(parents=True, exist_ok=True)

        # Build command
        cmd = self.build_ffmpeg_command(source, destination, rule)

        self.update_job_status(job_id, "in_progress", progress=0)

        # Get filename for notifications
        source_filename = Path(source).name

        # Notify that transcoding started
        await self._notify("started", source_filename)

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            self._active_processes[job_id] = process

            # Read progress from stdout
            if process.stdout:
                async for line_bytes in process.stdout:
                    line = line_bytes.decode().strip()
                    progress = self.parse_ffmpeg_progress(line, job_id)
                    if progress is not None:
                        self.update_job_progress(job_id, progress)

            await process.wait()

            # Clean up
            if job_id in self._active_processes:
                del self._active_processes[job_id]
            if job_id in self._total_durations:
                del self._total_durations[job_id]

            if process.returncode == 0:
                self.update_job_status(job_id, "completed", progress=100)
                await self._notify("completed", source_filename)
            else:
                stderr = ""
                if process.stderr:
                    stderr_bytes = await process.stderr.read()
                    stderr = stderr_bytes.decode()[-500:]  # Last 500 chars
                self.update_job_status(
                    job_id,
                    "failed",
                    error_message=f"ffmpeg exited with code {process.returncode}: {stderr}",
                )
                await self._notify("failed", source_filename)

        except asyncio.CancelledError:
            self.update_job_status(job_id, "cancelled")
            raise
        except Exception as e:
            self.update_job_status(job_id, "failed", error_message=str(e))
            await self._notify("failed", source_filename)

    def process_queue(self) -> None:
        """Start pending jobs if capacity available (called from sync context)."""
        # This is a no-op in sync context - the async processing happens
        # via process_queue_async called from the Textual event loop
        pass

    async def process_queue_async(self) -> None:
        """Start pending jobs if capacity available."""
        max_parallel = get_config().get("transcoding.max_parallel_jobs", 5)
        in_progress = self.get_in_progress_count()

        if in_progress >= max_parallel:
            return

        # Start as many jobs as we have capacity for
        pending = self.get_pending_jobs()
        jobs_to_start = max_parallel - in_progress

        for job in pending[:jobs_to_start]:
            # Don't await - let it run in background
            asyncio.create_task(self.start_job_async(job["id"], job))


def _dict_factory(cursor: Any, row: tuple[Any, ...]) -> dict[str, Any]:
    """Convert sqlite row to dict."""
    fields = [column[0] for column in cursor.description]
    return dict(zip(fields, row))
