#!/usr/bin/env python3

from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
import statistics
from typing import Any, Callable


PARSER_VERSION = 1

ParsedResult = dict[str, Any]
ParserFunction = Callable[[str, str, dict[str, Any]], ParsedResult]


@dataclass(frozen=True)
class ParserSpec:
    name: str
    function: ParserFunction
    options: dict[str, Any] = field(default_factory=dict)


def parsed_result(
    collection_status: str,
    outcome: str,
    metrics: dict[str, Any] | None,
    warnings: list[str] | None = None,
) -> ParsedResult:
    return {
        "collection_status": collection_status,
        "outcome": outcome,
        "metrics": metrics,
        "warnings": warnings or [],
    }


def combined_text(stdout: str, stderr: str) -> str:
    return stdout + ("\n" if stdout and stderr else "") + stderr


def extract_json_document(text: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    position = 0
    while True:
        start = text.find("{", position)
        if start < 0:
            break
        try:
            value, _ = decoder.raw_decode(text[start:])
        except json.JSONDecodeError:
            position = start + 1
            continue
        if isinstance(value, dict):
            return value
        position = start + 1
    raise ValueError("no JSON object found in output")


def _number(
    pattern: str,
    text: str,
    cast: Callable[[str], Any] = float,
) -> Any:
    match = re.search(pattern, text)
    return cast(match.group(1)) if match else None


def _integer_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def parse_nvme_smart(
    stdout: str,
    stderr: str,
    options: dict[str, Any],
) -> ParsedResult:
    del stderr
    payload = extract_json_document(stdout)
    temperature_kelvin = _integer_or_none(payload.get("temperature"))
    temperature_celsius = (
        round(temperature_kelvin - 273.15, 2)
        if temperature_kelvin is not None
        else None
    )
    metrics = {
        "kind": "nvme_smart",
        "device": options["device"],
        "critical_warning": _integer_or_none(payload.get("critical_warning")),
        "temperature_kelvin": temperature_kelvin,
        "temperature_celsius": temperature_celsius,
        "available_spare_percent": _integer_or_none(payload.get("avail_spare")),
        "spare_threshold_percent": _integer_or_none(payload.get("spare_thresh")),
        "percentage_used": _integer_or_none(payload.get("percent_used")),
        "data_units_read": _integer_or_none(payload.get("data_units_read")),
        "data_units_written": _integer_or_none(payload.get("data_units_written")),
        "power_cycles": _integer_or_none(payload.get("power_cycles")),
        "power_on_hours": _integer_or_none(payload.get("power_on_hours")),
        "unsafe_shutdowns": _integer_or_none(payload.get("unsafe_shutdowns")),
        "media_errors": _integer_or_none(payload.get("media_errors")),
        "error_log_entries": _integer_or_none(payload.get("num_err_log_entries")),
        "warning_temperature_time_minutes": _integer_or_none(
            payload.get("warning_temp_time")
        ),
        "critical_temperature_time_minutes": _integer_or_none(
            payload.get("critical_comp_time")
        ),
        "temperature_sensor_1_kelvin": _integer_or_none(
            payload.get("temperature_sensor_1")
        ),
        "temperature_sensor_2_kelvin": _integer_or_none(
            payload.get("temperature_sensor_2")
        ),
        "health_reasons": [],
    }

    required = (
        "critical_warning",
        "available_spare_percent",
        "spare_threshold_percent",
        "media_errors",
    )
    missing = [name for name in required if metrics[name] is None]
    if missing:
        return parsed_result(
            "partial",
            "unknown",
            metrics,
            [f"missing SMART values: {', '.join(missing)}"],
        )

    reasons: list[str] = []
    if metrics["critical_warning"] != 0:
        reasons.append("critical warning is nonzero")
    if metrics["media_errors"] != 0:
        reasons.append("media error count is nonzero")
    if metrics["available_spare_percent"] < metrics["spare_threshold_percent"]:
        reasons.append("available spare is below its threshold")
    metrics["health_reasons"] = reasons

    warnings: list[str] = []
    if metrics["error_log_entries"] not in (None, 0):
        warnings.append("NVMe error log contains entries")
    return parsed_result(
        "complete",
        "fail" if reasons else "pass",
        metrics,
        warnings,
    )


def parse_nvme_device_self_test(
    stdout: str,
    stderr: str,
    options: dict[str, Any],
) -> ParsedResult:
    text = combined_text(stdout, stderr)
    percentages = [int(value) for value in re.findall(r"]\s+(\d+)%", text)]
    short_started = bool(re.search(r"(?i)Short Device self-test started", text))
    extended_started = bool(
        re.search(r"(?i)Extended Device self-test started", text)
    )
    started = short_started or extended_started
    maximum = max(percentages, default=None)
    explicit_failure = bool(
        re.search(r"(?i)\b(failed|failure|fatal error)\b", text)
    )
    complete = explicit_failure or (started and maximum == 100)
    metrics = {
        "kind": "nvme_device_self_test",
        "device": options["device"],
        "test_type": (
            "extended" if extended_started else "short" if short_started else None
        ),
        "started": started,
        "maximum_progress_percent": maximum,
        "completion_marker_found": maximum == 100,
        "failure_marker_found": explicit_failure,
    }
    return parsed_result(
        "complete" if complete else "partial",
        "fail" if explicit_failure else "pass" if complete else "unknown",
        metrics,
        [] if complete else ["self-test completion marker not found"],
    )


def parse_nvme_self_test_log(
    stdout: str,
    stderr: str,
    options: dict[str, Any],
) -> ParsedResult:
    del stderr
    payload = extract_json_document(stdout)
    reports = payload.get("List of Valid Reports") or []
    latest = reports[0] if reports and isinstance(reports[0], dict) else {}
    operation = _integer_or_none(payload.get("Current Device Self-Test Operation"))
    completion = _integer_or_none(payload.get("Current Device Self-Test Completion"))
    latest_report = {
        "result_code": _integer_or_none(latest.get("Self test result")),
        "test_code": _integer_or_none(latest.get("Self test code")),
        "segment_number": _integer_or_none(latest.get("Segment number")),
        "valid_diagnostic_information": _integer_or_none(
            latest.get("Valid Diagnostic Information")
        ),
        "power_on_hours": _integer_or_none(latest.get("Power on hours")),
        "vendor_specific": _integer_or_none(latest.get("Vendor Specific")),
    }
    metrics = {
        "kind": "nvme_self_test_log",
        "device": options["device"],
        "current_operation": operation,
        "current_completion_percent": completion,
        "valid_report_count": len(reports),
        "latest_report": latest_report,
    }
    result_code = latest_report["result_code"]
    if operation is None or result_code is None:
        return parsed_result(
            "partial",
            "unknown",
            metrics,
            ["current operation or latest self-test result is missing"],
        )
    if operation != 0:
        return parsed_result(
            "complete",
            "unknown",
            metrics,
            ["an NVMe device self-test is still in progress"],
        )
    if result_code == 0:
        return parsed_result("complete", "pass", metrics)
    if result_code == 15:
        return parsed_result(
            "complete",
            "unknown",
            metrics,
            ["no completed self-test result is available"],
        )
    return parsed_result(
        "complete",
        "fail",
        metrics,
        [f"latest NVMe self-test result code is {result_code}"],
    )


PTS_NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"
PTS_RESULT_PATTERN = re.compile(
    rf"(?m)^    Type: (?P<label>[^\r\n]+):\r?\n"
    rf"(?P<samples>(?:        {PTS_NUMBER}\r?\n)+)"
    rf"\r?\n    Average: (?P<average>{PTS_NUMBER}) "
    rf"(?P<unit>MB/s|IOPS)\r?\n"
    rf"    Deviation: (?P<deviation>{PTS_NUMBER})%"
)


def _pts_versions(text: str, profile_pattern: str, tool_pattern: str) -> dict[str, Any]:
    pts = re.search(r"Phoronix Test Suite v([^\s]+)", text)
    profile = re.search(profile_pattern, text)
    tool = re.search(tool_pattern, text)
    return {
        "phoronix_test_suite_version": pts.group(1) if pts else None,
        "profile": profile.group(1) if profile else None,
        "tool_version": tool.group(1).strip() if tool else None,
    }


def _pts_measurement(match: re.Match[str]) -> dict[str, Any]:
    samples = [float(value) for value in re.findall(PTS_NUMBER, match.group("samples"))]
    return {
        "unit": match.group("unit"),
        "samples": samples,
        "average": float(match.group("average")),
        "deviation_percent": float(match.group("deviation")),
        "sample_count": len(samples),
    }


def parse_pts_fio(
    stdout: str,
    stderr: str,
    options: dict[str, Any],
) -> ParsedResult:
    text = combined_text(stdout, stderr)
    versions = _pts_versions(
        text,
        r"(pts/fio-[0-9.]+)",
        r"Flexible IO Tester\s+([^:\r\n]+):",
    )
    grouped: dict[tuple[str, str, int | None, int | None, str, str], dict[str, Any]] = {}
    warnings: list[str] = []

    for match in PTS_RESULT_PATTERN.finditer(text):
        parts = [part.strip() for part in match.group("label").split(" - ")]
        workload = parts[0].lower()
        values: dict[str, str] = {}
        for part in parts[1:]:
            if ": " in part:
                key, value = part.split(": ", 1)
                values[key] = value
        try:
            buffered = int(values["Buffered"]) if "Buffered" in values else None
            direct = int(values["Direct"]) if "Direct" in values else None
            key = (
                workload,
                values["Engine"],
                buffered,
                direct,
                values["Block Size"],
                values["Disk Target"],
            )
        except (KeyError, ValueError) as exc:
            warnings.append(f"could not parse FIO configuration label: {exc}")
            continue

        configuration = grouped.setdefault(
            key,
            {
                "workload": workload,
                "engine": values["Engine"],
                "buffered": buffered,
                "direct": direct,
                "block_size": values["Block Size"],
                "disk_target": values["Disk Target"],
                "throughput": None,
                "iops": None,
            },
        )
        field = "throughput" if match.group("unit") == "MB/s" else "iops"
        if configuration[field] is not None:
            warnings.append(
                f"duplicate {field} result for {workload} {values['Block Size']} "
                f"on {values['Disk Target']}"
            )
        configuration[field] = _pts_measurement(match)

    configurations = list(grouped.values())
    expected = int(options["expected_configurations"])
    complete_pairs = all(
        item["throughput"] is not None and item["iops"] is not None
        for item in configurations
    )
    complete = (
        len(configurations) == expected
        and complete_pairs
        and versions["profile"] is not None
        and versions["tool_version"] is not None
    )
    if len(configurations) != expected:
        warnings.append(
            f"expected {expected} FIO configurations, found {len(configurations)}"
        )
    if configurations and not complete_pairs:
        warnings.append("one or more FIO configurations is missing throughput or IOPS")

    metrics = {
        "kind": "pts_fio",
        **versions,
        "expected_configuration_count": expected,
        "configuration_count": len(configurations),
        "configurations": configurations,
    }
    return parsed_result(
        "complete" if complete else "partial",
        "pass" if complete else "unknown",
        metrics,
        warnings,
    )


def parse_memtester(
    stdout: str,
    stderr: str,
    options: dict[str, Any],
) -> ParsedResult:
    text = combined_text(stdout, stderr)
    loops = [(int(a), int(b)) for a, b in re.findall(r"Loop (\d+)/(\d+):", text)]
    failures = len(re.findall(r"(?i)FAILURE", text))
    successful_checks = len(re.findall(r"(?m)ok\r?$", text))
    done = "Done." in text
    expected = int(options["expected_loops"])
    completed = max((loop for loop, _ in loops), default=0)
    declared = max((total for _, total in loops), default=0)
    requested = re.search(r"want\s+(\d+)MB", text)
    allocated = re.search(r"got\s+(\d+)MB", text)
    complete = done and completed >= expected and declared == expected
    metrics = {
        "kind": "memtester",
        "requested_mb": int(requested.group(1)) if requested else None,
        "allocated_mb": int(allocated.group(1)) if allocated else None,
        "memory_locked": "trying mlock ...locked." in text,
        "expected_loops": expected,
        "declared_loops": declared,
        "completed_loops": completed,
        "successful_checks": successful_checks,
        "failures": failures,
        "completion_marker_found": done,
    }
    if failures:
        outcome = "fail"
    elif complete:
        outcome = "pass"
    else:
        outcome = "unknown"
    return parsed_result(
        "complete" if complete or failures else "partial",
        outcome,
        metrics,
        [] if complete or failures else ["expected memtester completion not found"],
    )


def parse_stressapptest(
    stdout: str,
    stderr: str,
    options: dict[str, Any],
) -> ParsedResult:
    del options
    text = combined_text(stdout, stderr)
    status_match = re.search(r"Status:\s+(PASS|FAIL)", text)
    completed = re.search(
        r"Completed:\s+([\d.]+)M in ([\d.]+)s ([\d.]+)MB/s, with "
        r"(\d+) hardware incidents, (\d+) errors",
        text,
    )
    configured = re.search(r"Starting SAT,\s+(\d+)M", text)
    memory_copy = re.search(r"Memory Copy:\s+[\d.]+M at ([\d.]+)MB/s", text)
    incidents = int(completed.group(4)) if completed else None
    errors = int(completed.group(5)) if completed else None
    reported = status_match.group(1) if status_match else None
    complete = reported is not None and completed is not None
    if reported == "FAIL" or (incidents is not None and incidents > 0) or (
        errors is not None and errors > 0
    ):
        outcome = "fail"
    elif complete and reported == "PASS" and incidents == 0 and errors == 0:
        outcome = "pass"
    else:
        outcome = "unknown"
    warnings = re.findall(r"(?m)^.*region number .* exceeds region count.*$", text)
    if not complete:
        warnings.append("complete stressapptest summary not found")
    metrics = {
        "kind": "stressapptest",
        "reported_status": reported,
        "configured_memory_mb": int(configured.group(1)) if configured else None,
        "processed_mb": float(completed.group(1)) if completed else None,
        "reported_duration_seconds": float(completed.group(2)) if completed else None,
        "throughput_mb_per_second": float(completed.group(3)) if completed else None,
        "memory_copy_mb_per_second": (
            float(memory_copy.group(1)) if memory_copy else None
        ),
        "hardware_incidents": incidents,
        "errors": errors,
    }
    return parsed_result(
        "complete" if complete else "partial",
        outcome,
        metrics,
        warnings,
    )


def parse_stress_ng(
    stdout: str,
    stderr: str,
    options: dict[str, Any],
) -> ParsedResult:
    del options
    text = combined_text(stdout, stderr)
    passed = re.search(r"passed:\s+(\d+):", text)
    failed = re.search(r"failed:\s+(\d+)", text)
    skipped = re.search(r"skipped:\s+(\d+)", text)
    untrustworthy = re.search(r"metrics untrustworthy:\s+(\d+)", text)
    successful = "successful run completed" in text
    passed_count = int(passed.group(1)) if passed else None
    failed_count = int(failed.group(1)) if failed else None
    complete = successful and passed_count is not None and failed_count is not None
    if failed_count is not None and failed_count > 0:
        outcome = "fail"
    elif complete and passed_count > 0 and failed_count == 0:
        outcome = "pass"
    else:
        outcome = "unknown"
    stable_warnings = len(
        re.findall(
            r"for stable load results, select a specific cpu stress method",
            text,
        )
    )
    warnings = []
    if stable_warnings:
        warnings.append(f"stable CPU method warning repeated {stable_warnings} times")
    if not complete and outcome == "unknown":
        warnings.append("complete stress-ng summary not found")
    metrics = {
        "kind": "stress_ng",
        "passed_workers": passed_count,
        "failed_workers": failed_count,
        "skipped_workers": int(skipped.group(1)) if skipped else None,
        "metrics_untrustworthy": (
            int(untrustworthy.group(1)) if untrustworthy else None
        ),
        "completion_marker_found": successful,
    }
    return parsed_result(
        "complete" if complete or outcome == "fail" else "partial",
        outcome,
        metrics,
        warnings,
    )


def parse_sysbench(
    stdout: str,
    stderr: str,
    options: dict[str, Any],
) -> ParsedResult:
    del options
    text = combined_text(stdout, stderr)
    version = re.search(r"sysbench\s+([^\s]+)", text)
    fairness_events = re.search(
        r"events \(avg/stddev\):\s+([\d.]+)/([\d.]+)", text
    )
    fairness_time = re.search(
        r"execution time \(avg/stddev\):\s+([\d.]+)/([\d.]+)", text
    )
    metrics = {
        "kind": "sysbench",
        "version": version.group(1) if version else None,
        "threads": _number(r"Number of threads:\s+(\d+)", text, int),
        "events_per_second": _number(r"events per second:\s+([\d.]+)", text),
        "total_events": _number(r"total number of events:\s+(\d+)", text, int),
        "total_time_seconds": _number(r"total time:\s+([\d.]+)s", text),
        "latency_ms_min": _number(r"(?m)^\s*min:\s+([\d.]+)", text),
        "latency_ms_average": _number(r"(?m)^\s*avg:\s+([\d.]+)", text),
        "latency_ms_max": _number(r"(?m)^\s*max:\s+([\d.]+)", text),
        "latency_ms_p95": _number(r"95th percentile:\s+([\d.]+)", text),
        "latency_ms_sum": _number(r"(?m)^\s*sum:\s+([\d.]+)", text),
        "events_fairness_average": (
            float(fairness_events.group(1)) if fairness_events else None
        ),
        "events_fairness_stddev": (
            float(fairness_events.group(2)) if fairness_events else None
        ),
        "execution_time_fairness_average": (
            float(fairness_time.group(1)) if fairness_time else None
        ),
        "execution_time_fairness_stddev": (
            float(fairness_time.group(2)) if fairness_time else None
        ),
    }
    required = ("events_per_second", "total_events", "total_time_seconds")
    complete = all(metrics[name] is not None for name in required)
    return parsed_result(
        "complete" if complete else "partial",
        "pass" if complete else "unknown",
        metrics,
        [] if complete else ["complete sysbench statistics not found"],
    )


def parse_pts_stream(
    stdout: str,
    stderr: str,
    options: dict[str, Any],
) -> ParsedResult:
    text = combined_text(stdout, stderr)
    versions = _pts_versions(
        text,
        r"(pts/stream-[0-9.]+)",
        r"Stream\s+([^:\r\n]+):",
    )
    by_operation: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    for match in PTS_RESULT_PATTERN.finditer(text):
        operation = match.group("label").strip()
        if operation in by_operation:
            warnings.append(f"duplicate STREAM result for {operation}")
        by_operation[operation] = {
            "operation": operation,
            "measurement": _pts_measurement(match),
        }

    expected_operations = list(options["expected_operations"])
    operations = [
        by_operation[name]
        for name in expected_operations
        if name in by_operation
    ]
    unexpected = sorted(set(by_operation) - set(expected_operations))
    operations.extend(by_operation[name] for name in unexpected)
    missing = [name for name in expected_operations if name not in by_operation]
    if missing:
        warnings.append(f"missing STREAM operations: {', '.join(missing)}")
    if unexpected:
        warnings.append(f"unexpected STREAM operations: {', '.join(unexpected)}")
    complete = (
        not missing
        and not unexpected
        and len(by_operation) == len(expected_operations)
        and versions["profile"] is not None
        and versions["tool_version"] is not None
    )
    metrics = {
        "kind": "pts_stream",
        **versions,
        "expected_operation_count": len(expected_operations),
        "operation_count": len(by_operation),
        "operations": operations,
    }
    return parsed_result(
        "complete" if complete else "partial",
        "pass" if complete else "unknown",
        metrics,
        warnings,
    )


def parse_cuda_memtest(
    stdout: str,
    stderr: str,
    options: dict[str, Any],
) -> ParsedResult:
    text = combined_text(stdout, stderr)
    expected = int(options["expected_passes"])
    finished_seconds = [
        float(value)
        for value in re.findall(r"Test10 finished in ([\d.]+) seconds", text)
    ]
    allocated = re.findall(r"Allocated\s+(\d+) MB", text)
    pass_reports = re.findall(
        r"test10: elapsedtime=([\d.]+), bandwidth=([\d.]+) GB/s", text
    )
    error_counts = [
        int(value)
        for value in re.findall(
            r"(?i)\berrors?\s*(?:count)?\s*[:=]\s*(\d+)", text
        )
    ]
    explicit_failure = bool(
        re.search(r"(?im)^.*\b(?:ERROR|FAIL|FATAL)\b.*$", text)
    )
    exited = "Program exits" in text
    maximum_errors = max(error_counts) if error_counts else None
    complete = exited and len(finished_seconds) >= expected
    if explicit_failure or (maximum_errors is not None and maximum_errors > 0):
        outcome = "fail"
    elif complete:
        outcome = "pass"
    else:
        outcome = "unknown"
    device = re.search(r"Device name=([^,\r\n]+)", text)
    metrics = {
        "kind": "cuda_memtest",
        "device_name": device.group(1) if device else None,
        "allocated_mb": int(allocated[-1]) if allocated else None,
        "expected_passes": expected,
        "completed_passes": len(finished_seconds),
        "finished_seconds": finished_seconds,
        "reported_elapsed_times": [float(value[0]) for value in pass_reports],
        "bandwidth_gb_per_second": [float(value[1]) for value in pass_reports],
        "reported_error_counts": error_counts,
        "maximum_reported_errors": maximum_errors,
        "completion_marker_found": exited,
        "failure_marker_found": explicit_failure,
    }
    return parsed_result(
        "complete" if complete or outcome == "fail" else "partial",
        outcome,
        metrics,
        [] if complete or outcome == "fail" else ["expected cuda_memtest completion not found"],
    )


def _float_stat(values: list[float], function: Callable[[list[float]], float]) -> float | None:
    return round(function(values), 6) if values else None


def parse_gpu_burn(
    stdout: str,
    stderr: str,
    options: dict[str, Any],
) -> ParsedResult:
    del options
    text = combined_text(stdout, stderr)
    error_counts = [int(value) for value in re.findall(r"errors:\s*(\d+)", text)]
    gpu_statuses = re.findall(r"(?m)^\s*GPU\s+(\d+):\s+(OK|FAULTY|FAILED)\s*$", text)
    gflops = [float(value) for value in re.findall(r"\(([\d.]+) Gflop/s\)", text)]
    temperatures = [float(value) for value in re.findall(r"temps:\s*([\d.]+) C", text)]
    processed = [int(value) for value in re.findall(r"proc'd:\s*(\d+)", text)]
    tested = re.search(r"Tested\s+(\d+) GPUs?:", text)
    burn_seconds = _number(r"Burning for\s+(\d+) seconds", text, int)
    devices = [
        {"gpu": int(gpu), "name": name.strip()}
        for gpu, name in re.findall(r"(?m)^GPU\s+(\d+):\s+(.+?)\s+\(UUID:", text)
    ]
    maximum_errors = max(error_counts) if error_counts else None
    all_ok = bool(gpu_statuses) and all(value == "OK" for _, value in gpu_statuses)
    complete = tested is not None and all_ok and maximum_errors is not None
    if any(value != "OK" for _, value in gpu_statuses) or (
        maximum_errors is not None and maximum_errors > 0
    ):
        outcome = "fail"
    elif complete and maximum_errors == 0:
        outcome = "pass"
    else:
        outcome = "unknown"
    metrics = {
        "kind": "gpu_burn",
        "configured_duration_seconds": burn_seconds,
        "tested_gpu_count": int(tested.group(1)) if tested else None,
        "gpu_devices": devices,
        "gpu_statuses": [
            {"gpu": int(gpu), "status": value} for gpu, value in gpu_statuses
        ],
        "sample_count": len(gflops),
        "maximum_reported_errors": maximum_errors,
        "gflops_min": min(gflops) if gflops else None,
        "gflops_median": _float_stat(gflops, statistics.median),
        "gflops_mean": _float_stat(gflops, statistics.fmean),
        "gflops_max": max(gflops) if gflops else None,
        "gflops_final": gflops[-1] if gflops else None,
        "temperature_celsius_min": min(temperatures) if temperatures else None,
        "temperature_celsius_max": max(temperatures) if temperatures else None,
        "temperature_celsius_final": temperatures[-1] if temperatures else None,
        "processed_iterations_final": processed[-1] if processed else None,
    }
    return parsed_result(
        "complete" if complete or outcome == "fail" else "partial",
        outcome,
        metrics,
        [] if complete or outcome == "fail" else ["expected gpu-burn completion not found"],
    )


def parse_nvbandwidth(
    stdout: str,
    stderr: str,
    options: dict[str, Any],
) -> ParsedResult:
    del stderr, options
    payload = extract_json_document(stdout)
    root = payload.get("nvbandwidth") or {}
    testcases = []
    for testcase in root.get("testcases") or []:
        matrix = []
        for row in testcase.get("bandwidth_matrix") or []:
            matrix.append([float(value) for value in row])
        testcases.append(
            {
                "name": testcase.get("name"),
                "description": testcase.get("bandwidth_description"),
                "status": testcase.get("status"),
                "sum_gb_per_second": (
                    float(testcase["sum"]) if testcase.get("sum") is not None else None
                ),
                "bandwidth_matrix_gb_per_second": matrix,
            }
        )
    statuses = [testcase["status"] for testcase in testcases]
    complete = bool(statuses) and all(status is not None for status in statuses)
    if statuses and all(status == "Passed" for status in statuses):
        outcome = "pass"
    elif statuses:
        outcome = "fail"
    else:
        outcome = "unknown"
    metrics = {
        "kind": "nvbandwidth",
        "version": root.get("version"),
        "driver_version": root.get("Driver Version"),
        "cuda_driver_api_version": _integer_or_none(
            root.get("CUDA Driver API Version")
        ),
        "cuda_runtime_version": _integer_or_none(root.get("CUDA Runtime Version")),
        "gpu_devices": [str(device) for device in root.get("GPU Device list") or []],
        "testcases": testcases,
    }
    return parsed_result(
        "complete" if complete else "partial",
        outcome,
        metrics,
        [] if complete else ["nvbandwidth did not report any testcases"],
    )


PARSER_SPECS: dict[str, ParserSpec] = {
    "2a": ParserSpec("nvme_smart", parse_nvme_smart, {"device": "/dev/nvme0n1"}),
    "2b": ParserSpec("nvme_smart", parse_nvme_smart, {"device": "/dev/nvme1n1"}),
    "3a": ParserSpec(
        "nvme_device_self_test",
        parse_nvme_device_self_test,
        {"device": "/dev/nvme0n1"},
    ),
    "3b": ParserSpec(
        "nvme_device_self_test",
        parse_nvme_device_self_test,
        {"device": "/dev/nvme1n1"},
    ),
    "4a": ParserSpec(
        "nvme_self_test_log",
        parse_nvme_self_test_log,
        {"device": "/dev/nvme0n1"},
    ),
    "4b": ParserSpec(
        "nvme_self_test_log",
        parse_nvme_self_test_log,
        {"device": "/dev/nvme1n1"},
    ),
    "5": ParserSpec(
        "pts_fio",
        parse_pts_fio,
        {"expected_configurations": 16},
    ),
    "6": ParserSpec("memtester", parse_memtester, {"expected_loops": 5}),
    "7": ParserSpec("stressapptest", parse_stressapptest),
    "8": ParserSpec("stress_ng", parse_stress_ng),
    "9": ParserSpec("sysbench", parse_sysbench),
    "10": ParserSpec(
        "pts_stream",
        parse_pts_stream,
        {"expected_operations": ("Copy", "Scale", "Add", "Triad")},
    ),
    "11": ParserSpec("cuda_memtest", parse_cuda_memtest, {"expected_passes": 2}),
    "12": ParserSpec("gpu_burn", parse_gpu_burn),
    "13a": ParserSpec("nvbandwidth", parse_nvbandwidth),
    "13b": ParserSpec("nvbandwidth", parse_nvbandwidth),
    "13c": ParserSpec("nvbandwidth", parse_nvbandwidth),
    "13d": ParserSpec("nvbandwidth", parse_nvbandwidth),
    "13e": ParserSpec("nvbandwidth", parse_nvbandwidth),
    "14a": ParserSpec("stress_ng", parse_stress_ng),
    "14b": ParserSpec("gpu_burn", parse_gpu_burn),
}


def parser_descriptor(step: str) -> dict[str, Any]:
    spec = PARSER_SPECS[step]
    return {"name": spec.name, "version": PARSER_VERSION}


def parse_step(step: str, stdout: str, stderr: str) -> ParsedResult:
    spec = PARSER_SPECS[step]
    try:
        return spec.function(stdout, stderr, spec.options)
    except Exception as exc:
        return parsed_result(
            "parser_error",
            "unknown",
            None,
            [f"{type(exc).__name__}: {exc}"],
        )
