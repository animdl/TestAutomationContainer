"""Run the benchmark playbook in Docker and record parsed results."""

import logging
import signal
import sys
import tempfile
import time
import uuid
from datetime import datetime
from pathlib import Path

from .capture import (
    COLLECTOR_VERSION,
    SCHEMA_VERSION,
    artifact_prefix,
    atomic_save,
    compress_artifact,
    execute_command,
    execute_commands,
    inspect_image,
    host_system_info,
    pts_system_summary,
    read_parser_text,
    source_manifest,
    suite_hash,
    timestamp,
    utc_now,
)
from .parsers import PARSER_VERSION, parse_output, parser_descriptor
from .playbook import BENCH_MOUNTS, IMAGE, PLAYBOOK, TESTS, execution_groups

logger = logging.getLogger(__name__)


def docker_command(command, container_name=None, sync_dir=None):
    docker_args = [
        "docker",
        "run",
        "--rm",
        "--gpus",
        "all",
        "--privileged",
    ]
    if container_name:
        docker_args.extend(["--name", container_name])
    if sync_dir:
        docker_args.extend(["--mount", f"type=bind,src={sync_dir},dst=/run/benchmark-sync"])

    for source, destination in BENCH_MOUNTS:
        docker_args.extend(
            [
                "--mount",
                f"type=bind,src={source},dst={destination}",
            ]
        )

    docker_args.extend([IMAGE, "/bin/bash", "-lc", command])
    return docker_args


def prepare_output_directory(path):
    if path.exists() and any(path.iterdir()):
        raise RuntimeError(f"output directory is not empty: {path}")
    path.mkdir(parents=True, exist_ok=True)
    (path / "raw").mkdir()


def initial_test_result(test):
    """Start the checkpoint record; execution supplies precise timings later."""
    return {
        "step": test.step,
        "command": test.command,
        "timeout_seconds": test.timeout_seconds,
        "started_at": timestamp(),
        "finished_at": None,
        "duration_seconds": None,
        "exit_code": None,
        "execution_status": "running",
        "collection_status": "pending",
        "outcome": "unknown",
        "parser": parser_descriptor(test.parser),
        "metrics": None,
        "warnings": [],
        "artifacts": {},
    }


def build_initial_results(run_id, image, started_at):
    package_dir = Path(__file__).resolve().parent
    source_paths = sorted(package_dir.glob("*.py"))
    return {
        "schema_version": SCHEMA_VERSION,
        "collector": {
            "version": COLLECTOR_VERSION,
            "parser_version": PARSER_VERSION,
            "python_version": sys.version.split()[0],
            "sources": source_manifest(source_paths),
        },
        "run_id": run_id,
        "image": image,
        "system": {"host": host_system_info(), "pts_summary": None, "pts_summary_step": None},
        "suite": {
            "command_sha256": suite_hash([(test.step, test.command) for test in TESTS]),
            "test_count": len(TESTS),
            "steps": [test.step for test in TESTS],
        },
        "started_at": timestamp(started_at),
        "finished_at": None,
        "duration_seconds": None,
        "status": "running",
        "tests": [],
        "warnings": [],
        "errors": [],
    }


def prepare_step(test, output_dir, results):
    """Checkpoint before launch, including every member of a concurrent group."""
    output_file = output_dir / "results.json"
    print(f"\n[{test.step}] {test.command}", flush=True)
    test_result = initial_test_result(test)
    results["tests"].append(test_result)
    atomic_save(output_file, results)

    prefix = artifact_prefix(test.step)
    stdout_path = output_dir / "raw" / f"{prefix}.stdout.log"
    stderr_path = output_dir / "raw" / f"{prefix}.stderr.log"
    container_name = f"benchmark-{uuid.uuid4().hex}"
    return test_result, stdout_path, stderr_path, container_name


def collect_step(test, output_dir, results, prepared, execution):
    test_result, stdout_path, stderr_path, _ = prepared
    output_file = output_dir / "results.json"

    stdout = read_parser_text(stdout_path)
    stderr = read_parser_text(stderr_path)
    print(stdout, end="", flush=True)
    if stderr:
        print(stderr, end="", file=sys.stderr, flush=True)

    if results["system"]["pts_summary"] is None and test.parser.name in (
        "pts_fio", "pts_stream", "stressapptest", "sysbench",
    ):
        summary = pts_system_summary(stdout)
        if summary:
            results["system"]["pts_summary"] = summary
            results["system"]["pts_summary_step"] = test.step

    parsed = parse_output(test.parser, stdout, stderr)
    warnings = list(parsed["warnings"])
    outcome = parsed["outcome"]
    if execution.execution_status == "failed":
        outcome = "fail"
        warnings.append(f"command exited with code {execution.exit_code}")
    elif execution.execution_status == "timed_out":
        outcome = "fail"
        warnings.append(f"command timed out after {test.timeout_seconds} seconds and was stopped")
    elif execution.execution_status != "completed":
        outcome = "unknown"
        warnings.append(
            "test outcome is incomplete because execution status is "
            f"{execution.execution_status}"
        )
    if execution.launch_error:
        warnings.append(execution.launch_error)
    if execution.cleanup_error:
        warnings.append(execution.cleanup_error)

    stdout_artifact = compress_artifact(stdout_path, output_dir)
    stderr_artifact = compress_artifact(stderr_path, output_dir)
    test_result.update(
        {
            "started_at": execution.started_at,
            "finished_at": execution.finished_at,
            "duration_seconds": execution.duration_seconds,
            "exit_code": execution.exit_code,
            "execution_status": execution.execution_status,
            "collection_status": parsed["collection_status"],
            "outcome": outcome,
            "metrics": parsed["metrics"],
            "warnings": warnings,
            "artifacts": {
                "stdout": stdout_artifact,
                "stderr": stderr_artifact,
            },
        }
    )
    atomic_save(output_file, results)
    print(
        f"[{test.step}] execution={execution.execution_status} "
        f"collection={test_result['collection_status']} "
        f"outcome={test_result['outcome']} "
        f"duration={execution.duration_seconds:.3f}s",
        flush=True,
    )
    return execution.execution_status == "interrupted"


def run_steps(tests, output_dir, results):
    prepared = [prepare_step(test, output_dir, results) for test in tests]
    if len(tests) == 1:
        (test,), (item,) = tests, prepared
        executions = [execute_command(
            docker_command(test.command, item[3]), item[1], item[2],
            container_name=item[3], timeout_seconds=test.timeout_seconds,
        )]
    else:
        with tempfile.TemporaryDirectory(prefix=".combined-", dir=output_dir.resolve()) as sync_dir:
            jobs = [
                (docker_command(test.command, item[3], sync_dir), item[1], item[2], item[3],
                 test.timeout_seconds)
                for test, item in zip(tests, prepared)
            ]
            executions = execute_commands(jobs)
    interrupted = False
    for test, item, execution in zip(tests, prepared, executions):
        interrupted |= collect_step(test, output_dir, results, item, execution)
    return interrupted


def main():
    run_name = datetime.now().strftime("%Y%m%d-%H%M%S")
    output_dir = Path("benchmark-results") / run_name
    if len(sys.argv) > 1:
        output_dir = Path(sys.argv[1])

    try:
        prepare_output_directory(output_dir)
    except (OSError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    output_file = output_dir / "results.json"
    run_started_at = utc_now()
    run_started = time.monotonic()
    image, image_warning = inspect_image(IMAGE)
    results = build_initial_results(run_name, image, run_started_at)
    if image_warning:
        results["warnings"].append(image_warning)
    atomic_save(output_file, results)

    interrupted = False

    def interrupt_on_termination(signum, frame):
        del signum, frame
        raise KeyboardInterrupt

    previous_sigterm = signal.signal(signal.SIGTERM, interrupt_on_termination)

    try:
        for group in execution_groups(PLAYBOOK):
            if run_steps(group, output_dir, results):
                interrupted = True
                break
    except KeyboardInterrupt:
        interrupted = True
    except Exception as exc:
        logger.exception("Benchmark runner failed")
        results["status"] = "runner_error"
        results["errors"].append(f"{type(exc).__name__}: {exc}")
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)
        if interrupted:
            results["status"] = "interrupted"
        elif results["status"] != "runner_error":
            all_successful = len(results["tests"]) == len(TESTS) and all(
                test["execution_status"] == "completed"
                and test["collection_status"] == "complete"
                and test["outcome"] == "pass"
                for test in results["tests"]
            )
            results["status"] = (
                "completed" if all_successful else "completed_with_issues"
            )
        results["finished_at"] = timestamp()
        results["duration_seconds"] = round(time.monotonic() - run_started, 6)
        atomic_save(output_file, results)

    print(f"\nResults: {output_file}")
    if results["status"] == "completed":
        return 0
    if results["status"] == "interrupted":
        return 130
    return 1 if results["status"] == "completed_with_issues" else 2


if __name__ == "__main__":
    raise SystemExit(main())
