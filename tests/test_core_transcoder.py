import asyncio
import signal
import subprocess
from pathlib import Path

import pytest
from textual.app import App

from torrra.core.db import get_db_connection
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
    monkeypatch.setattr(
        tm, "build_ffmpeg_command", lambda _src, _dst, _rule: ["sleep", "30"]
    )
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
