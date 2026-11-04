from __future__ import annotations

import asyncio
import codecs
import json
import os
import re
import signal
import subprocess
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypedDict

from torrra.core.config import get_config
from torrra.core.db import get_db_connection
from torrra.utils.video import VIDEO_EXTENSIONS

if TYPE_CHECKING:
    pass

# Subtitle encoder per output container. mov_text is what Apple's players offer
# as a selectable track in mp4, but it is text-only: bitmap tracks (PGS, VobSub)
# can't be converted to it. mkv stores any track as it is.
_SUBTITLE_ENCODERS = {
    ".mp4": "mov_text",
    ".m4v": "mov_text",
    ".mov": "mov_text",
    ".mkv": "copy",
}
_TEXT_SUBTITLE_CODECS = {"subrip", "srt", "ass", "ssa", "webvtt", "mov_text", "text"}
_SIDECAR_EXTENSIONS = {".srt", ".ass", ".ssa", ".vtt"}
_EPISODE = re.compile(r"s\d{1,2}e\d{1,3}", re.IGNORECASE)

# ISO 639-2/T code (the form mp4 specifies) and the other ways releases spell it.
# "hi" is left out: on a subtitle file it means hearing impaired, not Hindi.
_LANGUAGES = {
    "ara": "ar arabic",
    "bul": "bg bulgarian",
    "ces": "cs cze czech",
    "dan": "da danish",
    "deu": "de ger german deutsch",
    "ell": "el gre greek",
    "eng": "en english",
    "est": "et estonian",
    "fas": "fa per persian farsi",
    "fin": "fi finnish",
    "fra": "fr fre french francais français",
    "heb": "he hebrew",
    "hin": "hindi",
    "hrv": "hr croatian",
    "hun": "hu hungarian",
    "ind": "id indonesian",
    "ita": "it italian italiano",
    "jpn": "ja japanese",
    "kor": "ko korean",
    "lav": "lv latvian",
    "lit": "lt lithuanian",
    "nld": "nl dut dutch",
    "nor": "no nb nob norwegian",
    "pol": "pl polish",
    "por": "pt portuguese portugues português",
    "ron": "ro rum romanian",
    "rus": "ru russian",
    "slk": "sk slo slovak",
    "slv": "sl slovenian",
    "spa": "es spanish espanol español",
    "srp": "sr serbian",
    "swe": "sv swedish",
    "tha": "th thai",
    "tur": "tr turkish",
    "ukr": "uk ukrainian",
    "vie": "vi vietnamese",
    "zho": "zh chi chinese",
}
_LANGUAGE_CODES = {
    alias: code
    for code, aliases in _LANGUAGES.items()
    for alias in (code, *aliases.split())
}


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


def _parse_language(text: str) -> str | None:
    """Find a language in the end of a name, e.g. "Movie.en.forced" or "pt-BR"."""
    tokens = re.findall(r"[^\W\d_]+", text.lower())
    for token in reversed(tokens[-3:]):
        if token in _LANGUAGE_CODES:
            return _LANGUAGE_CODES[token]
    return None


def _track_language(tag: str | None) -> str:
    """Normalize a stream's language tag; the mp4 muxer drops anything but 3 letters."""
    tag = (tag or "").lower()
    return _parse_language(tag) or (tag if len(tag) == 3 else "und")


def _sidecar_language(sidecar: Path, source: Path) -> str:
    """Read a subtitle file's language from its name, minus the video's own name."""
    stem = sidecar.stem
    if stem.lower().startswith(source.stem.lower()):
        stem = stem[len(source.stem) :]
    return _parse_language(stem) or "und"


def _sidecar_charset(sidecar: Path) -> str | None:
    """Name the charset ffmpeg must be told about: it fails the job on non-UTF-8."""
    data = sidecar.read_bytes()
    # ffmpeg detects these from the BOM itself
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return None
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return "CP1252"
    return None


def _ffprobe_path() -> str:
    # ffprobe is usually alongside ffmpeg
    return (
        get_config()
        .get("transcoding.ffmpeg_path", "ffmpeg")
        .replace("ffmpeg", "ffprobe")
    )


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
        self._paused: bool | None = None  # loaded from the db on first use
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

    def is_paused(self) -> bool:
        """Whether transcoding is globally paused."""
        if self._paused is None:
            with get_db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT value FROM app_state WHERE key = 'transcoding_paused'"
                )
                row = cursor.fetchone()
                self._paused = bool(row and row[0] == "1")
        return self._paused

    def set_paused(self, paused: bool) -> None:
        """Pause or resume all transcoding.

        Pausing holds the queue and freezes running ffmpeg processes, so they
        continue where they stopped on resume. Where processes can't be frozen
        (Windows), running jobs finish and only the queue is held.
        """
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO app_state (key, value) VALUES ('transcoding_paused', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                ("1" if paused else "0",),
            )
            conn.commit()
        self._paused = paused

        for process in self._active_processes.values():
            self._freeze(process, paused)

    @staticmethod
    def _freeze(process: asyncio.subprocess.Process, frozen: bool) -> None:
        if not hasattr(signal, "SIGSTOP") or process.returncode is not None:
            return
        with suppress(ProcessLookupError):
            process.send_signal(signal.SIGSTOP if frozen else signal.SIGCONT)

    def requeue_interrupted_jobs(self) -> None:
        """Requeue jobs left in progress by a previous run so they restart."""
        # ffmpeg can't resume a partial output, so the job restarts from zero
        # (the -y flag overwrites the partial file). Jobs running in this
        # process are left alone.
        active = list(self._active_processes)
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"""
                UPDATE transcode_jobs
                SET status = 'pending', progress = 0, error_message = NULL
                WHERE status = 'in_progress'
                    AND id NOT IN ({",".join("?" * len(active))})
                """,
                active,
            )
            conn.commit()

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
            # a frozen process only acts on the signal once continued
            self._freeze(process, False)
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

    def find_sidecar_subtitles(self, magnet_uri: str, source: str) -> list[Path]:
        """Find the subtitle files of a torrent that belong to a video.

        A file belongs to the video if it carries the video's name or episode,
        or sits in a folder named after it. In a torrent with a single video,
        every subtitle file does.
        """
        from torrra.core.download import get_download_manager

        source_path = Path(source)
        stem = source_path.stem.lower()
        episode = _EPISODE.search(source_path.stem)
        files = [Path(f) for f in get_download_manager().get_torrent_files(magnet_uri)]
        videos = [
            f
            for f in files
            if f.suffix.lower() in VIDEO_EXTENSIONS
            and not f.stem.lower().endswith("sample")
            and f.parent.name.lower() != "sample"
        ]

        def belongs(sidecar: Path) -> bool:
            if videos == [source_path] or sidecar.stem.lower().startswith(stem):
                return True
            if stem in (part.lower() for part in sidecar.parts):
                return True
            where = f"{sidecar.parent.name}/{sidecar.name}"
            found = _EPISODE.search(where)
            return bool(episode and found and found[0].lower() == episode[0].lower())

        return sorted(
            f
            for f in files
            if f.suffix.lower() in _SIDECAR_EXTENSIONS and f.is_file() and belongs(f)
        )

    def probe_streams(self, file_path: str) -> list[dict[str, Any]]:
        """List a file's streams using ffprobe; empty if it can't be read."""
        try:
            result = subprocess.run(
                [_ffprobe_path(), "-v", "quiet", "-show_streams", "-of", "json"]
                + [file_path],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 0:
                return json.loads(result.stdout).get("streams", [])
        except (FileNotFoundError, subprocess.TimeoutExpired, ValueError):
            pass
        return []

    def _subtitle_args(
        self, source: str, destination: str, sidecars: Sequence[Path]
    ) -> tuple[list[str], list[str]]:
        """Build the ffmpeg input and output args that keep subtitles as tracks.

        Both are empty when there is nothing to keep, or the container can't
        hold it, which leaves stream selection to ffmpeg.
        """
        encoder = _SUBTITLE_ENCODERS.get(Path(destination).suffix.lower())
        if not encoder:
            return [], []

        streams = self.probe_streams(source)
        embedded = [
            s
            for s in streams
            if s.get("codec_type") == "subtitle"
            and (encoder == "copy" or s.get("codec_name") in _TEXT_SUBTITLE_CODECS)
        ]
        if not streams or not (embedded or sidecars):
            return [], []

        # -map turns off ffmpeg's own selection, so make the same picks it would.
        # 0:V skips cover art, which ffmpeg counts as a video stream.
        outputs = ["-map", "0:V:0"]
        audio = [s for s in streams if s.get("codec_type") == "audio"]
        if audio:
            best = max(audio, key=lambda s: s.get("channels", 0))
            outputs += ["-map", f"0:{best['index']}"]

        languages: list[str] = []
        for stream in embedded:
            outputs += ["-map", f"0:{stream['index']}"]
            languages.append(_track_language(stream.get("tags", {}).get("language")))

        inputs: list[str] = []
        for input_index, sidecar in enumerate(sidecars, start=1):
            charset = _sidecar_charset(sidecar)
            if charset:
                inputs += ["-sub_charenc", charset]
            inputs += ["-i", str(sidecar)]
            outputs += ["-map", f"{input_index}:0"]
            languages.append(_sidecar_language(sidecar, Path(source)))

        outputs += ["-c:s", encoder]
        for track, language in enumerate(languages):
            outputs += [f"-metadata:s:s:{track}", f"language={language}"]
            # subtitles stay off until picked, and the mp4 muxer splits tracks
            # flagged default into separate groups, one for each, which a
            # player's language menu can't treat as alternatives
            outputs += [f"-disposition:s:{track}", "-default"]

        return inputs, outputs

    def build_ffmpeg_command(
        self,
        source: str,
        destination: str,
        rule: TranscodeRule,
        sidecars: Sequence[Path] = (),
    ) -> list[str]:
        """Build ffmpeg command based on transcoding rule."""
        ffmpeg_path = get_config().get("transcoding.ffmpeg_path", "ffmpeg")
        subtitle_inputs, subtitle_outputs = self._subtitle_args(
            source, destination, sidecars
        )
        cmd = [
            ffmpeg_path,
            "-i",
            source,
            *subtitle_inputs,
            "-y",  # Overwrite output
            "-progress",
            "pipe:1",  # Output progress to stdout
            "-nostats",
        ]

        resolution = rule.get("resolution", "original")

        # Video codec - use libx264 for mp4/m4v compatibility
        # Force 8-bit pixel format for iPad/Apple device compatibility
        # (10-bit H.264 High 10 profile is not supported on most Apple devices)
        cmd.extend(
            [
                "-c:v",
                "libx264",
                "-preset",
                "medium",
                "-crf",
                "23",
                "-pix_fmt",
                "yuv420p",
            ]
        )

        # Resolution scaling
        if resolution != "original":
            height_map = {"720p": 720, "1080p": 1080, "4k": 2160}
            height = height_map.get(resolution, 1080)
            # Scale to height while preserving aspect ratio, ensure even dimensions
            cmd.extend(["-vf", f"scale=-2:{height}"])

        # Audio codec - use AAC for compatibility
        # Use audio filter to normalize channel layouts (5.1(side) -> 5.1) and ensure
        # proper channel mapping for formats like EAC3 that use non-standard layouts
        cmd.extend(
            [
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-af",
                "aformat=channel_layouts=5.1|stereo",
            ]
        )

        cmd.extend(subtitle_outputs)
        cmd.append(destination)
        return cmd

    def get_video_duration(self, file_path: str) -> float | None:
        """Get video duration in seconds using ffprobe."""
        try:
            result = subprocess.run(
                [
                    _ffprobe_path(),
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
        sidecars = self.find_sidecar_subtitles(job["magnet_uri"], source)
        cmd = self.build_ffmpeg_command(source, destination, rule, sidecars)

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
            # transcoding may have been paused while the process was spawning
            if self.is_paused():
                self._freeze(process, True)

            # Read progress from stdout
            if process.stdout:
                async for line_bytes in process.stdout:
                    line = line_bytes.decode().strip()
                    progress = self.parse_ffmpeg_progress(line, job_id)
                    if progress is not None:
                        self.update_job_progress(job_id, progress)

            await process.wait()

            # Clean up; cancel_job() already removed the process if the user
            # cancelled, and it has set the status itself
            cancelled = self._active_processes.pop(job_id, None) is None
            self._total_durations.pop(job_id, None)

            if cancelled:
                return
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
            # The task is only cancelled when the app shuts down: stop ffmpeg
            # and put the job back in the queue for the next start
            self._total_durations.pop(job_id, None)
            process = self._active_processes.pop(job_id, None)
            if process and process.returncode is None:
                with suppress(ProcessLookupError):
                    process.kill()
                await process.wait()
            self.update_job_status(job_id, "pending", progress=0)
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
        if self.is_paused():
            return

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
