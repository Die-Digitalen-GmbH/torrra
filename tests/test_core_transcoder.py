import asyncio
import json
import shutil
import signal
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from textual.app import App

from torrra.core.db import get_db_connection
from torrra.core import transcoder
from torrra.core.transcoder import TranscodeManager

posix_only = pytest.mark.skipif(
    not hasattr(signal, "SIGSTOP"), reason="needs SIGSTOP/SIGCONT"
)


@pytest.fixture
def manager(monkeypatch: pytest.MonkeyPatch):
    # conftest points the db at a temp dir; run `sleep` in place of ffmpeg so
    # a job stays in progress until stopped
    tm = TranscodeManager()
    monkeypatch.setattr(
        tm, "get_matching_rule", lambda _src: {"input_extension": ".mkv"}
    )
    monkeypatch.setattr(tm, "get_video_duration", lambda _src: None)
    monkeypatch.setattr(tm, "find_sidecar_subtitles", lambda _uri, _src: [])
    monkeypatch.setattr(tm, "build_ffmpeg_command", lambda *_args: ["sleep", "30"])
    return tm


def _insert_job(tmp_path: Path, status: str, progress: float = 0) -> int:
    source = tmp_path / f"{status}.mkv"
    source.touch()
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO transcode_jobs
                (magnet_uri, source_file, destination_file, status, progress)
            VALUES ('magnet:?xt=urn:btih:abc', ?, ?, ?, ?)
            """,
            (str(source), str(tmp_path / "out" / f"{status}.mp4"), status, progress),
        )
        conn.commit()
        return cursor.lastrowid or 0


def _status(tm: TranscodeManager, job_id: int) -> tuple[str, float]:
    job = next(j for j in tm.get_all_jobs() if j["id"] == job_id)
    return job["status"], job["progress"]


async def _wait_until_running(tm: TranscodeManager, job_id: int) -> None:
    while job_id not in tm._active_processes:  # pyright: ignore[reportPrivateUsage]
        await asyncio.sleep(0.01)


def test_requeue_interrupted_jobs(manager: TranscodeManager, tmp_path: Path):
    interrupted = _insert_job(tmp_path, "in_progress", progress=42)
    cancelled = _insert_job(tmp_path, "cancelled")
    completed = _insert_job(tmp_path, "completed", progress=100)

    manager.requeue_interrupted_jobs()

    assert _status(manager, interrupted) == ("pending", 0)
    assert _status(manager, cancelled) == ("cancelled", 0)
    assert _status(manager, completed) == ("completed", 100)
    assert [j["id"] for j in manager.get_pending_jobs()] == [interrupted]


async def test_requeue_leaves_jobs_running_in_this_process(
    manager: TranscodeManager, tmp_path: Path
):
    job_id = _insert_job(tmp_path, "pending")
    task = asyncio.create_task(
        manager.start_job_async(job_id, manager.get_pending_jobs()[0])
    )
    await _wait_until_running(manager, job_id)

    manager.requeue_interrupted_jobs()

    assert _status(manager, job_id)[0] == "in_progress"
    manager.cancel_job(job_id)
    await task


async def test_shutdown_stops_ffmpeg_and_requeues_job(
    manager: TranscodeManager, tmp_path: Path
):
    job_id = _insert_job(tmp_path, "pending")
    task = asyncio.create_task(
        manager.start_job_async(job_id, manager.get_pending_jobs()[0])
    )
    await _wait_until_running(manager, job_id)
    process = manager._active_processes[job_id]  # pyright: ignore[reportPrivateUsage]

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert process.returncode is not None
    assert _status(manager, job_id) == ("pending", 0)


async def test_user_cancel_stays_cancelled(manager: TranscodeManager, tmp_path: Path):
    job_id = _insert_job(tmp_path, "pending")
    task = asyncio.create_task(
        manager.start_job_async(job_id, manager.get_pending_jobs()[0])
    )
    await _wait_until_running(manager, job_id)

    manager.cancel_job(job_id)
    await task

    assert _status(manager, job_id)[0] == "cancelled"


def _process_state(process: asyncio.subprocess.Process) -> str:
    # first letter of ps STAT: T = stopped, S/R = running
    out = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(process.pid)],
        capture_output=True,
        text=True,
        check=False,
    )
    return out.stdout.strip()[:1]


async def _wait_for_state(process: asyncio.subprocess.Process, state: str) -> None:
    for _ in range(100):
        if _process_state(process) == state:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"process never reached state {state!r}")


@posix_only
async def test_pause_freezes_and_resume_continues_running_job(
    manager: TranscodeManager, tmp_path: Path
):
    job_id = _insert_job(tmp_path, "pending")
    task = asyncio.create_task(
        manager.start_job_async(job_id, manager.get_pending_jobs()[0])
    )
    await _wait_until_running(manager, job_id)
    process = manager._active_processes[job_id]  # pyright: ignore[reportPrivateUsage]

    manager.set_paused(True)
    await _wait_for_state(process, "T")
    assert _status(manager, job_id)[0] == "in_progress"

    manager.set_paused(False)
    await _wait_for_state(process, "S")

    manager.cancel_job(job_id)
    await task


async def test_pause_holds_queue_and_persists(
    manager: TranscodeManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    job_id = _insert_job(tmp_path, "pending")
    started: list[int] = []

    async def fake_start(jid: int, _job: object) -> None:
        started.append(jid)

    manager.set_paused(True)
    # a fresh manager (next app start) reads the paused state from the db
    restarted = TranscodeManager()
    monkeypatch.setattr(restarted, "start_job_async", fake_start)
    assert restarted.is_paused()

    await restarted.process_queue_async()
    await asyncio.sleep(0)
    assert started == []

    restarted.set_paused(False)
    await restarted.process_queue_async()
    await asyncio.sleep(0)
    assert started == [job_id]


@posix_only
async def test_cancel_while_paused_stops_frozen_job(
    manager: TranscodeManager, tmp_path: Path
):
    job_id = _insert_job(tmp_path, "pending")
    task = asyncio.create_task(
        manager.start_job_async(job_id, manager.get_pending_jobs()[0])
    )
    await _wait_until_running(manager, job_id)
    process = manager._active_processes[job_id]  # pyright: ignore[reportPrivateUsage]
    manager.set_paused(True)
    await _wait_for_state(process, "T")

    manager.cancel_job(job_id)
    await asyncio.wait_for(task, timeout=5)

    assert process.returncode is not None
    assert _status(manager, job_id)[0] == "cancelled"


@posix_only
async def test_shutdown_while_paused_requeues_frozen_job(
    manager: TranscodeManager, tmp_path: Path
):
    job_id = _insert_job(tmp_path, "pending")
    task = asyncio.create_task(
        manager.start_job_async(job_id, manager.get_pending_jobs()[0])
    )
    await _wait_until_running(manager, job_id)
    process = manager._active_processes[job_id]  # pyright: ignore[reportPrivateUsage]
    manager.set_paused(True)
    await _wait_for_state(process, "T")

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)

    assert process.returncode is not None
    assert _status(manager, job_id) == ("pending", 0)


def test_quitting_app_requeues_running_job(manager: TranscodeManager, tmp_path: Path):
    # the real shutdown path: the app quits while a transcode task is running
    job_id = _insert_job(tmp_path, "pending")

    class QuitMidTranscode(App[None]):
        async def on_mount(self) -> None:
            asyncio.create_task(
                manager.start_job_async(job_id, manager.get_pending_jobs()[0])
            )
            await _wait_until_running(manager, job_id)
            self.exit()

    QuitMidTranscode().run(headless=True)

    assert _status(manager, job_id) == ("pending", 0)
    assert not manager._active_processes  # pyright: ignore[reportPrivateUsage]


needs_ffmpeg = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="needs ffmpeg"
)
RULE: Any = {
    "input_extension": ".mkv",
    "output_format": "mp4",
    "resolution": "original",
}
SRT = "1\n00:00:00,500 --> 00:00:01,500\n{}\n\n"


def _make_movie(
    tmp_path: Path, *tracks: tuple[str, str], default: bool = False
) -> Path:
    """Write a short mkv with one SRT track per (language, text)."""
    inputs: list[str] = []
    maps = ["-map", "0", "-map", "1"]
    tags: list[str] = []
    for i, (language, text) in enumerate(tracks):
        srt = tmp_path / f"track{i}.txt"
        srt.write_text(SRT.format(text))
        inputs += ["-f", "srt", "-i", str(srt)]
        maps += ["-map", str(i + 2)]
        tags += [f"-metadata:s:s:{i}", f"language={language}"]
        if default:
            tags += [f"-disposition:s:{i}", "default"]
    movie = tmp_path / "Movie.mkv"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y"]
        + ["-f", "lavfi", "-i", "testsrc=d=2:s=64x48"]
        + ["-f", "lavfi", "-i", "sine=d=2"]
        + inputs
        + maps
        + ["-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-c:s", "srt"]
        + tags
        + [str(movie)],
        check=True,
    )
    return movie


def _transcode(source: Path, destination: Path, sidecars: tuple[Path, ...] = ()):
    cmd = TranscodeManager().build_ffmpeg_command(
        str(source), str(destination), RULE, sidecars
    )
    subprocess.run(cmd, check=True, capture_output=True)


def _subtitle_tracks(path: Path) -> list[dict[str, Any]]:
    probe = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_streams", "-select_streams", "s"]
        + ["-of", "json", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(probe.stdout)["streams"]


def _subtitle_text(path: Path, track: int = 0) -> str:
    return subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-map", f"0:s:{track}"]
        + ["-f", "srt", "-"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _languages(tracks: list[dict[str, Any]]) -> list[str]:
    return [t["tags"].get("language", "und") for t in tracks]


def _with_streams(monkeypatch: pytest.MonkeyPatch, *streams: dict[str, Any]):
    tm = TranscodeManager()
    monkeypatch.setattr(tm, "probe_streams", lambda _src: list(streams))
    return tm


def _subtitle_stream(index: int, codec: str, language: str = "eng") -> dict[str, Any]:
    return {
        "index": index,
        "codec_type": "subtitle",
        "codec_name": codec,
        "tags": {"language": language},
    }


VIDEO_STREAM = {"index": 0, "codec_type": "video", "codec_name": "h264"}


@needs_ffmpeg
def test_all_embedded_text_subtitles_become_tracks(tmp_path: Path):
    movie = _make_movie(tmp_path, ("fre", "bonjour"), ("eng", "hello"))
    out = tmp_path / "out.mp4"

    _transcode(movie, out)

    tracks = _subtitle_tracks(out)
    assert [t["codec_name"] for t in tracks] == ["mov_text", "mov_text"]
    assert _languages(tracks) == ["fra", "eng"]
    assert "bonjour" in _subtitle_text(out, 0)
    assert "hello" in _subtitle_text(out, 1)


@needs_ffmpeg
def test_default_flags_are_cleared_so_tracks_stay_alternatives(tmp_path: Path):
    movie = _make_movie(tmp_path, ("fre", "bonjour"), ("eng", "hello"), default=True)
    out = tmp_path / "out.mp4"

    _transcode(movie, out)

    # the mp4 muxer always enables the first track of a type; what matters is
    # that the second is not, which would split them into separate groups
    assert [t["disposition"]["default"] for t in _subtitle_tracks(out)] == [1, 0]


def _format_tags(path: Path) -> dict[str, str]:
    probe = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_format", "-of", "json", str(path)],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(probe.stdout)["format"]["tags"]


@needs_ffmpeg
def test_output_is_titled_from_the_release_name(tmp_path: Path):
    junk = "GalaxyTV - The.Innocent.S01E01.SPANISH.720p.NF.WEBRip.x264-GalaxyTV"
    movie = _make_movie(tmp_path, ("spa", "hola"))
    source = tmp_path / f"{junk.removeprefix('GalaxyTV - ')}.mkv"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(movie), "-map", "0", "-c", "copy"]
        + ["-metadata", f"title={junk}", "-metadata", f"comment={junk}"]
        + ["-metadata:s:a", "language=spa", str(source)],
        check=True,
    )
    out = tmp_path / "out.mp4"

    _transcode(source, out)

    tags = _format_tags(out)
    assert tags["title"] == "The Innocent S01E01"
    assert "comment" not in tags
    audio = [
        s
        for s in TranscodeManager().probe_streams(str(out))
        if s["codec_type"] == "audio"
    ]
    assert audio[0]["tags"]["language"] == "spa"


@needs_ffmpeg
def test_video_and_audio_survive_mapping(tmp_path: Path):
    movie = _make_movie(tmp_path, ("eng", "hello"))
    out = tmp_path / "out.mp4"

    _transcode(movie, out)

    types = [s["codec_type"] for s in TranscodeManager().probe_streams(str(out))]
    assert sorted(types) == ["audio", "subtitle", "video"]


@needs_ffmpeg
@pytest.mark.parametrize(
    ("name", "encoding"),
    [
        ("Movie.fr.srt", "utf-8"),
        ("Movie.fr.srt", "utf-8-sig"),
        ("Movie.fr.srt", "utf-16"),
        ("Movie.fr.srt", "latin-1"),  # exits 69 unless ffmpeg is told the charset
    ],
)
def test_sidecar_becomes_a_tagged_track(tmp_path: Path, name: str, encoding: str):
    movie = _make_movie(tmp_path)
    sidecar = tmp_path / name
    sidecar.write_bytes(SRT.format("café").encode(encoding))
    out = tmp_path / "out.mp4"

    _transcode(movie, out, (sidecar,))

    assert _languages(_subtitle_tracks(out)) == ["fra"]
    assert "café" in _subtitle_text(out)


def test_bitmap_subtitles_are_left_out_of_mp4(monkeypatch: pytest.MonkeyPatch):
    tm = _with_streams(
        monkeypatch,
        VIDEO_STREAM,
        _subtitle_stream(1, "hdmv_pgs_subtitle"),
        _subtitle_stream(2, "subrip"),
    )

    cmd = tm.build_ffmpeg_command("a.mkv", "a.mp4", RULE)

    assert "0:2" in cmd
    assert "0:1" not in cmd


def test_only_bitmap_subtitles_leaves_selection_to_ffmpeg(
    monkeypatch: pytest.MonkeyPatch,
):
    tm = _with_streams(
        monkeypatch, VIDEO_STREAM, _subtitle_stream(1, "hdmv_pgs_subtitle")
    )

    assert "-map" not in tm.build_ffmpeg_command("a.mkv", "a.mp4", RULE)


def test_mkv_output_copies_every_track(monkeypatch: pytest.MonkeyPatch):
    tm = _with_streams(
        monkeypatch,
        VIDEO_STREAM,
        _subtitle_stream(1, "hdmv_pgs_subtitle"),
        _subtitle_stream(2, "subrip"),
    )

    cmd = tm.build_ffmpeg_command("a.mkv", "a.mkv", RULE)

    assert {"0:1", "0:2"} <= set(cmd)
    assert cmd[cmd.index("-c:s") + 1] == "copy"


def test_unknown_container_leaves_selection_to_ffmpeg(monkeypatch: pytest.MonkeyPatch):
    tm = _with_streams(monkeypatch, VIDEO_STREAM, _subtitle_stream(1, "subrip"))

    assert "-map" not in tm.build_ffmpeg_command("a.mkv", "a.webm", RULE)


def test_unreadable_source_leaves_selection_to_ffmpeg(monkeypatch: pytest.MonkeyPatch):
    tm = _with_streams(monkeypatch)

    assert "-map" not in tm.build_ffmpeg_command("a.mkv", "a.mp4", RULE)


def test_keeps_the_audio_ffmpeg_would_pick(monkeypatch: pytest.MonkeyPatch):
    tm = _with_streams(
        monkeypatch,
        VIDEO_STREAM,
        {"index": 1, "codec_type": "audio", "channels": 2},
        {"index": 2, "codec_type": "audio", "channels": 6},
        {"index": 3, "codec_type": "audio", "channels": 6},
        _subtitle_stream(4, "subrip"),
    )

    cmd = tm.build_ffmpeg_command("a.mkv", "a.mp4", RULE)

    assert "0:2" in cmd
    assert not {"0:1", "0:3"} & set(cmd)


@pytest.mark.parametrize(
    ("tag", "expected"),
    [
        ("eng", "eng"),
        ("en", "eng"),  # dropped by the mp4 muxer if passed through
        ("fre", "fra"),
        ("pt-BR", "por"),
        ("wol", "wol"),
        ("", "und"),
    ],
)
def test_embedded_language_tags_are_normalized(
    monkeypatch: pytest.MonkeyPatch, tag: str, expected: str
):
    tm = _with_streams(monkeypatch, VIDEO_STREAM, _subtitle_stream(1, "subrip", tag))

    cmd = tm.build_ffmpeg_command("a.mkv", "a.mp4", RULE)

    assert cmd[cmd.index("-metadata:s:s:0") + 1] == f"language={expected}"


def _torrent(monkeypatch: pytest.MonkeyPatch, root: Path, *names: str) -> list[Path]:
    files = [root / name for name in names]
    for file in files:
        file.parent.mkdir(parents=True, exist_ok=True)
        file.touch()
    manager = SimpleNamespace(get_torrent_files=lambda _uri: [str(f) for f in files])
    monkeypatch.setattr("torrra.core.download.get_download_manager", lambda: manager)
    return files


def _sidecars(root: Path, source: str) -> list[str]:
    found = TranscodeManager().find_sidecar_subtitles("uri", str(root / source))
    return [str(f.relative_to(root)) for f in found]


def test_single_video_takes_every_subtitle_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    _torrent(
        monkeypatch,
        tmp_path,
        "Movie/Movie.2019.mkv",
        "Movie/Sample/sample.mkv",
        "Movie/Subs/2_English.srt",
        "Movie/Subs/French.ass",
        "Movie/Subs/idx.sub",
        "Movie/Movie.2019.nfo",
    )

    assert _sidecars(tmp_path, "Movie/Movie.2019.mkv") == [
        "Movie/Subs/2_English.srt",
        "Movie/Subs/French.ass",
    ]
    assert _sidecars(tmp_path, "Movie/Sample/sample.mkv") == []


def test_season_pack_matches_subtitles_per_episode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    _torrent(
        monkeypatch,
        tmp_path,
        "Show/Show.S01E01.1080p.mkv",
        "Show/Show.S01E02.1080p.mkv",
        "Show/Show.S01E03.1080p.mkv",
        "Show/Show.S01E01.1080p.en.srt",
        "Show/Subs/Show.S01E02.1080p/2_English.srt",
        "Show/Subs/Show.S01E03.English.srt",
        "Show/Subs/extras.srt",
    )

    assert _sidecars(tmp_path, "Show/Show.S01E01.1080p.mkv") == [
        "Show/Show.S01E01.1080p.en.srt"
    ]
    assert _sidecars(tmp_path, "Show/Show.S01E02.1080p.mkv") == [
        "Show/Subs/Show.S01E02.1080p/2_English.srt"
    ]
    assert _sidecars(tmp_path, "Show/Show.S01E03.1080p.mkv") == [
        "Show/Subs/Show.S01E03.English.srt"
    ]


def test_files_not_on_disk_are_skipped(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    files = _torrent(monkeypatch, tmp_path, "Movie.mkv", "Movie.en.srt")
    files[1].unlink()

    assert _sidecars(tmp_path, "Movie.mkv") == []


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("The.Italian.Job.en.srt", "eng"),  # not the title's language
        ("The.Italian.Job.en.forced.srt", "eng"),
        ("The.Italian.Job.English.SDH.srt", "eng"),
        ("The.Italian.Job.hi.srt", "und"),  # hearing impaired, not Hindi
        ("The.Italian.Job.pt-BR.srt", "por"),
        ("The.Italian.Job.srt", "und"),
        ("2_French.srt", "fra"),
        ("German.srt", "deu"),
    ],
)
def test_sidecar_language_comes_from_its_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str, expected: str
):
    sidecar = tmp_path / name
    sidecar.write_text(SRT.format("x"))
    tm = _with_streams(monkeypatch, VIDEO_STREAM)

    cmd = tm.build_ffmpeg_command("The.Italian.Job.mkv", "out.mp4", RULE, (sidecar,))

    assert cmd[cmd.index("-metadata:s:s:0") + 1] == f"language={expected}"


def test_oversized_subtitle_files_are_skipped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    files = _torrent(monkeypatch, tmp_path, "Movie.mkv", "Movie.en.srt", "Movie.fr.srt")
    with files[2].open("wb") as f:
        f.truncate(transcoder._MAX_SIDECAR_BYTES + 1)

    assert _sidecars(tmp_path, "Movie.mkv") == ["Movie.en.srt"]


def test_subtitle_file_count_is_capped(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    names = [f"Subs/{i:03}.srt" for i in range(transcoder._MAX_SIDECARS + 1)]
    _torrent(monkeypatch, tmp_path, "Movie.mkv", *names)

    assert len(_sidecars(tmp_path, "Movie.mkv")) == transcoder._MAX_SIDECARS


def _failing_setup(*_args: Any) -> list[Path]:
    raise OSError("unreadable")


async def test_setup_error_fails_the_job(
    manager: TranscodeManager, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    job_id = _insert_job(tmp_path, "pending")
    job = manager.get_pending_jobs()[0]
    monkeypatch.setattr(manager, "find_sidecar_subtitles", _failing_setup)

    await manager.start_job_async(job_id, job)

    assert _status(manager, job_id)[0] == "failed"
    assert manager.get_pending_jobs() == []


async def _start_blocked_in_setup(
    manager: TranscodeManager, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[int, threading.Event, "asyncio.Task[None]"]:
    job_id = _insert_job(tmp_path, "pending")
    job = manager.get_pending_jobs()[0]
    entered = threading.Event()
    release = threading.Event()

    def blocked(*_args: Any) -> list[str]:
        entered.set()
        release.wait(5)
        return ["sleep", "30"]

    monkeypatch.setattr(manager, "build_ffmpeg_command", blocked)
    task = asyncio.create_task(manager.start_job_async(job_id, job))
    # the loop keeps running: setup blocking it would hang here
    while not entered.is_set():
        await asyncio.sleep(0.01)
    return job_id, release, task


async def test_job_in_setup_is_claimed_and_survives_requeue(
    manager: TranscodeManager, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    job_id, release, task = await _start_blocked_in_setup(
        manager, monkeypatch, tmp_path
    )

    manager.requeue_interrupted_jobs()

    assert _status(manager, job_id)[0] == "in_progress"
    assert manager.get_pending_jobs() == []

    release.set()
    await _wait_until_running(manager, job_id)
    manager.cancel_job(job_id)
    await task


async def test_cancelling_during_setup_starts_nothing(
    manager: TranscodeManager, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    job_id, release, task = await _start_blocked_in_setup(
        manager, monkeypatch, tmp_path
    )

    manager.cancel_job(job_id)
    release.set()
    await task

    assert _status(manager, job_id)[0] == "cancelled"
    assert not manager._active_processes  # pyright: ignore[reportPrivateUsage]
