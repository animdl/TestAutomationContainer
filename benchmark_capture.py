#!/usr/bin/env python3

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import time
from collections.abc import Iterable, Sequence
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO

COLLECTOR_VERSION = 5
SCHEMA_VERSION = 6


@dataclass
class ExecutionResult:
    started_at: str
    finished_at: str
    duration_seconds: float
    exit_code: int | None
    execution_status: str
    launch_error: str | None = None
    cleanup_error: str | None = None


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def timestamp(value: datetime | None = None) -> str:
    return (value or utc_now()).isoformat().replace("+00:00", "Z")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def suite_hash(tests: list[tuple[str, str]]) -> str:
    payload = json.dumps(
        [{"step": step, "command": command} for step, command in tests],
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return sha256_bytes(payload)


def source_manifest(paths: Iterable[Path]) -> dict[str, str]:
    return {path.name: sha256_path(path) for path in paths}


def inspect_image(image: str) -> tuple[dict[str, Any], str | None]:
    metadata = {"requested": image, "image_id": None, "repo_digests": []}
    try:
        completed = subprocess.run(
            ["docker", "image", "inspect", image],
            capture_output=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return metadata, f"docker image inspection failed: {type(exc).__name__}: {exc}"

    if completed.returncode != 0:
        error = completed.stderr.decode("utf-8", errors="replace").strip()
        return metadata, (
            f"docker image inspection exited {completed.returncode}: "
            f"{error or 'no diagnostic output'}"
        )
    try:
        inspected = json.loads(completed.stdout.decode("utf-8"))[0]
    except (IndexError, KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return metadata, f"docker image inspection returned invalid JSON: {exc}"
    metadata["image_id"] = inspected.get("Id")
    metadata["repo_digests"] = inspected.get("RepoDigests") or []
    return metadata, None


def artifact_prefix(step: str) -> str:
    match = re.fullmatch(r"(\d+)(.*)", step)
    if not match:
        return re.sub(r"[^a-zA-Z0-9_.-]", "_", step)
    return f"{int(match.group(1)):02d}{match.group(2)}"


def remove_container(container_name: str) -> str | None:
    """Remove this step's container even if the attached Docker client was killed."""
    previous_handlers = {}
    try:
        # Let cleanup finish when cancellation is requested more than once.
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[signum] = signal.signal(signum, signal.SIG_IGN)
        completed = subprocess.run(
            ["docker", "rm", "--force", container_name],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=30,
            check=False,
        )
        diagnostic = (completed.stderr or b"").decode("utf-8", errors="replace").strip()
        # --rm may already have removed the container after it exited.
        if completed.returncode and "No such container" not in diagnostic:
            return f"container cleanup failed for {container_name}: {diagnostic or completed.returncode}"
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"container cleanup failed for {container_name}: {type(exc).__name__}: {exc}"
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
    return None


def execute_command(
    command: list[str],
    stdout_path: Path,
    stderr_path: Path,
    container_name: str | None = None,
) -> ExecutionResult:
    started = utc_now()
    monotonic_started = time.monotonic()
    exit_code: int | None = None
    status = "launch_failed"
    launch_error: str | None = None
    cleanup_error: str | None = None

    with stdout_path.open("wb") as stdout_handle, stderr_path.open("wb") as stderr_handle:
        try:
            completed = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=stdout_handle,
                stderr=stderr_handle,
                check=False,
            )
            exit_code = completed.returncode
            status = "completed" if exit_code == 0 else "failed"
        except KeyboardInterrupt:
            status = "interrupted"
            if container_name:
                cleanup_error = remove_container(container_name)
                if cleanup_error:
                    stderr_handle.write(f"runner cleanup error: {cleanup_error}\n".encode())
        except OSError as exc:
            launch_error = f"{type(exc).__name__}: {exc}"
            stderr_handle.write(f"runner launch error: {launch_error}\n".encode())
        finally:
            stdout_handle.flush()
            stderr_handle.flush()

    return ExecutionResult(
        started_at=timestamp(started),
        finished_at=timestamp(),
        duration_seconds=round(time.monotonic() - monotonic_started, 6),
        exit_code=exit_code,
        execution_status=status,
        launch_error=launch_error,
        cleanup_error=cleanup_error,
    )


def execute_commands(
    jobs: Sequence[tuple[list[str], Path, Path, str | None]],
) -> list[ExecutionResult]:
    """Launch a group before waiting; retain separate logs and clean up all jobs."""
    records = [ExecutionResult(timestamp(), "", 0, None, "launch_failed") for _ in jobs]
    processes: list[subprocess.Popen[bytes] | None] = [None] * len(jobs)
    starts = [time.monotonic()] * len(jobs)
    with ExitStack() as files:
        try:
            for i, (command, stdout_path, stderr_path, _) in enumerate(jobs):
                starts[i] = time.monotonic()
                record = records[i]
                record.started_at = timestamp()
                stdout = files.enter_context(stdout_path.open("wb"))
                stderr = files.enter_context(stderr_path.open("wb"))
                try:
                    processes[i] = subprocess.Popen(
                        command, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                    )
                except OSError as exc:
                    record.launch_error = f"{type(exc).__name__}: {exc}"
                    record.finished_at = timestamp()
                    record.duration_seconds = round(time.monotonic() - starts[i], 6)
                    stderr.write(f"runner launch error: {record.launch_error}\n".encode())

            pending = {i: process for i, process in enumerate(processes) if process is not None}
            while pending:
                for i, process in list(pending.items()):
                    code = process.poll()
                    if code is not None:
                        record = records[i]
                        record.exit_code = code
                        record.execution_status = "completed" if code == 0 else "failed"
                        record.finished_at = timestamp()
                        record.duration_seconds = round(time.monotonic() - starts[i], 6)
                        del pending[i]
                if pending:
                    time.sleep(0.05)
        except KeyboardInterrupt:
            for i, record in enumerate(records):
                if not record.finished_at:
                    record.execution_status = "interrupted"
                    record.finished_at = timestamp()
                    record.duration_seconds = round(time.monotonic() - starts[i], 6)
        finally:
            # Stop Docker clients before removing containers to avoid a late launch.
            # Cleanup also covers exceptions during launch or polling.
            previous_handlers = {}
            try:
                for signum in (signal.SIGINT, signal.SIGTERM):
                    previous_handlers[signum] = signal.signal(signum, signal.SIG_IGN)
                for i, process in enumerate(processes):
                    if process is not None and process.poll() is None:
                        process.kill()
                        process.wait(timeout=30)
                    if not records[i].finished_at or records[i].execution_status == "interrupted":
                        name = jobs[i][3]
                        if name:
                            records[i].cleanup_error = remove_container(name)
            finally:
                for signum, handler in previous_handlers.items():
                    signal.signal(signum, handler)
    return records


def compress_stream(source: BinaryIO, destination_handle: BinaryIO) -> None:
    with gzip.GzipFile(
        filename="",
        mode="wb",
        fileobj=destination_handle,
        mtime=0,
    ) as destination:
        shutil.copyfileobj(source, destination, length=1024 * 1024)


def compress_artifact(path: Path, output_dir: Path) -> dict[str, Any]:
    content_sha256 = sha256_path(path)
    uncompressed_bytes = path.stat().st_size
    compressed_path = path.with_name(path.name + ".gz")
    temporary = compressed_path.with_name(
        f".{compressed_path.name}.{os.getpid()}.tmp"
    )
    try:
        with path.open("rb") as source, temporary.open("wb") as destination:
            compress_stream(source, destination)
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(temporary, compressed_path)
        path.unlink()
    finally:
        if temporary.exists():
            temporary.unlink()

    return {
        "path": compressed_path.relative_to(output_dir).as_posix(),
        "compression": "gzip",
        "uncompressed_bytes": uncompressed_bytes,
        "compressed_bytes": compressed_path.stat().st_size,
        "content_sha256": content_sha256,
        "artifact_sha256": sha256_path(compressed_path),
    }


def read_parser_text(path: Path) -> str:
    return path.read_bytes().decode("utf-8", errors="replace")


def validate_results_consistency(results: dict[str, Any]) -> None:
    """Check relationships between fields; schema validation lives in tests."""
    steps = results["suite"]["steps"]
    if results["suite"]["test_count"] != len(steps):
        raise ValueError("suite test_count does not match steps")
    if len(set(steps)) != len(steps):
        raise ValueError("suite steps are not unique")
    for test in results["tests"]:
        if (
            test["metrics"] is not None
            and test["metrics"].get("kind") != test["parser"].get("name")
        ):
            raise ValueError(f"test {test['step']} parser and metrics kind differ")


def atomic_save(path: Path, results: dict[str, Any]) -> None:
    validate_results_consistency(results)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(results, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
