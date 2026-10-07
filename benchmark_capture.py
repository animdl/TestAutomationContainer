#!/usr/bin/env python3

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
from typing import Any, BinaryIO, Iterable


COLLECTOR_VERSION = 1
SCHEMA_VERSION = 3


@dataclass
class ExecutionResult:
    started_at: str
    finished_at: str
    duration_seconds: float
    exit_code: int | None
    execution_status: str
    interrupted: bool
    launch_error: str | None = None


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
    ).encode("utf-8")
    return sha256_bytes(payload)


def source_manifest(paths: Iterable[Path]) -> dict[str, str]:
    return {path.name: sha256_path(path) for path in paths}


def inspect_image(image: str) -> tuple[dict[str, Any], str | None]:
    metadata = {"requested": image, "image_id": None, "repo_digests": []}
    try:
        completed = subprocess.run(
            ["docker", "image", "inspect", image],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
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


def execute_command(
    command: list[str],
    stdout_path: Path,
    stderr_path: Path,
) -> ExecutionResult:
    started = utc_now()
    monotonic_started = time.monotonic()
    exit_code: int | None = None
    status = "launch_failed"
    interrupted = False
    launch_error: str | None = None

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
            interrupted = True
            status = "interrupted"
        except OSError as exc:
            launch_error = f"{type(exc).__name__}: {exc}"
            stderr_handle.write(f"runner launch error: {launch_error}\n".encode("utf-8"))
        finally:
            stdout_handle.flush()
            stderr_handle.flush()

    return ExecutionResult(
        started_at=timestamp(started),
        finished_at=timestamp(),
        duration_seconds=round(time.monotonic() - monotonic_started, 6),
        exit_code=exit_code,
        execution_status=status,
        interrupted=interrupted,
        launch_error=launch_error,
    )


def compress_stream(source: BinaryIO, destination_handle: BinaryIO) -> None:
    with gzip.GzipFile(
        filename="",
        mode="wb",
        fileobj=destination_handle,
        mtime=0,
    ) as destination:
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            destination.write(chunk)


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


def _require_fields(value: dict[str, Any], fields: set[str], label: str) -> None:
    missing = sorted(fields - value.keys())
    if missing:
        raise ValueError(f"{label} missing fields: {', '.join(missing)}")


def validate_results_shape(results: dict[str, Any]) -> None:
    _require_fields(
        results,
        {
            "schema_version",
            "collector",
            "run_id",
            "image",
            "suite",
            "started_at",
            "finished_at",
            "duration_seconds",
            "status",
            "tests",
            "warnings",
            "errors",
        },
        "results",
    )
    if results["schema_version"] != SCHEMA_VERSION:
        raise ValueError("unsupported results schema version")
    if not isinstance(results["tests"], list):
        raise ValueError("tests must be a list")
    if results["suite"]["test_count"] != len(results["suite"]["steps"]):
        raise ValueError("suite test_count does not match steps")
    if len(set(results["suite"]["steps"])) != len(results["suite"]["steps"]):
        raise ValueError("suite steps are not unique")

    required_test_fields = {
        "step",
        "command",
        "started_at",
        "finished_at",
        "duration_seconds",
        "exit_code",
        "execution_status",
        "collection_status",
        "outcome",
        "parser",
        "metrics",
        "warnings",
        "artifacts",
    }
    for test in results["tests"]:
        label = f"test {test.get('step', '?')}"
        _require_fields(test, required_test_fields, label)
        if test["metrics"] is not None:
            if test["metrics"].get("kind") != test["parser"].get("name"):
                raise ValueError(f"{label} parser and metrics kind differ")
        for stream_name, artifact in test["artifacts"].items():
            if stream_name not in {"stdout", "stderr"}:
                raise ValueError(f"{label} has unknown artifact {stream_name}")
            _require_fields(
                artifact,
                {
                    "path",
                    "compression",
                    "uncompressed_bytes",
                    "compressed_bytes",
                    "content_sha256",
                    "artifact_sha256",
                },
                f"{label} {stream_name} artifact",
            )


def atomic_save(path: Path, results: dict[str, Any]) -> None:
    validate_results_shape(results)
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
