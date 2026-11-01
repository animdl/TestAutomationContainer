#!/usr/bin/env python3

from __future__ import annotations

import json
import logging
import re
import statistics
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

PARSER_VERSION = 5

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
    reasons: list[str] = []
    if metrics["critical_warning"] not in (None, 0):
        reasons.append("critical warning is nonzero")
    if metrics["media_errors"] not in (None, 0):
        reasons.append("media error count is nonzero")
    spare = metrics["available_spare_percent"]
    threshold = metrics["spare_threshold_percent"]
    if spare is not None and threshold is not None and spare < threshold:
        reasons.append("available spare is below its threshold")
    metrics["health_reasons"] = reasons

    warnings: list[str] = []
    if missing:
        warnings.append(f"missing SMART values: {', '.join(missing)}")
    if metrics["error_log_entries"] not in (None, 0):
        warnings.append("NVMe error log contains entries")
    return parsed_result(
        "partial" if missing else "complete",
        "fail" if reasons else "unknown" if missing else "pass",
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
    rf"(?m)^[ \t]*Type: (?P<label>[^\r\n]+)\r?\n(?:\r?\n)?"
    rf"(?P<samples>(?:[ \t]*{PTS_NUMBER}(?: (?:[KMGT]?i?B/s|IOPS))?[ \t]*\r?\n)+)"
    rf"\r?\n[ \t]*Average: (?P<average>{PTS_NUMBER}) "
    rf"(?P<unit>[KMGT]?i?B/s|IOPS)\r?\n"
    rf"(?:[ \t]*Deviation: (?P<deviation>{PTS_NUMBER})%)?"
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
    samples = [float(value) for value in re.findall(
        rf"(?m)^[ \t]*({PTS_NUMBER})", match.group("samples")
    )]
    return {
        "unit": match.group("unit"),
        "samples": samples,
        "average": float(match.group("average")),
        "deviation_percent": (
            float(match.group("deviation")) if match.group("deviation") else None
        ),
        "sample_count": len(samples),
    }


def _fio_statistics(text: str, name: str) -> dict[str, Any] | None:
    match = re.search(
        rf"(?m)^\s*{name}\s*(?:\(\s*([^)]*)\))?\s*:\s*"
        rf"min=\s*({PTS_NUMBER}), max=\s*({PTS_NUMBER}), "
        rf"(?:per=[^,]+, )?avg=\s*({PTS_NUMBER}), stdev=\s*({PTS_NUMBER}), samples=(\d+)",
        text,
    )
    if not match:
        return None
    return {
        "unit": match.group(1).strip() if match.group(1) else "IOPS",
        "minimum": float(match.group(2)), "maximum": float(match.group(3)),
        "average": float(match.group(4)), "stddev": float(match.group(5)),
        "sample_count": int(match.group(6)),
    }


def parse_pts_fio(
    stdout: str, stderr: str, options: dict[str, Any],
) -> ParsedResult:
    text = combined_text(stdout, stderr)
    versions = _pts_versions(
        text, r"(pts/fio-[0-9.]+)", r"(?m)^fio-([0-9.]+)",
    )
    if versions["tool_version"] is None:
        legacy = re.search(r"Flexible IO Tester ([0-9.]+):", text)
        versions["tool_version"] = legacy.group(1) if legacy else None
    grouped: dict[tuple[Any, ...], dict[str, Any]] = {}
    warnings: list[str] = []

    def configuration(label: str) -> dict[str, Any] | None:
        parts = label.rstrip(":").split(" - ")
        values = dict(part.split(": ", 1) for part in parts[1:] if ": " in part)
        try:
            fields = {
                "workload": parts[0].lower(), "engine": values["Engine"],
                "buffered": int(values["Buffered"]) if "Buffered" in values else None,
                "direct": int(values["Direct"]) if "Direct" in values else None,
                "block_size": values["Block Size"], "disk_target": values["Disk Target"],
                "job_count": int(values["Job Count"]) if "Job Count" in values else None,
            }
        except (KeyError, ValueError) as exc:
            warnings.append(f"could not parse FIO configuration label: {exc}")
            return None
        return grouped.setdefault(tuple(fields.values()), {
            **fields, "throughput": None, "iops": None,
            "reported_throughput": None, "throughput_source": "pts", "runs": [],
        })

    for match in PTS_RESULT_PATTERN.finditer(text):
        item = configuration(match.group("label"))
        if item is None:
            continue
        field = "iops" if match.group("unit") == "IOPS" else "reported_throughput"
        if item[field] is not None:
            warnings.append(f"duplicate {field} result for {match.group('label')}")
        item[field] = _pts_measurement(match)

    # BASIC mode includes native output. Its explicit units take precedence over
    # PTS's known incorrect bandwidth labels, while the PTS summary is preserved.
    for match in re.finditer(
        r"(?ms)^Flexible IO Tester \(Run (\d+) of (\d+)\)\r?\n"
        r"Type: ([^\r\n]+)\r?\n(.*?)(?=^Flexible IO Tester|\Z)", text,
    ):
        item = configuration(match.group(3))
        if item is None:
            continue
        body = match.group(4)
        summary = re.search(
            rf"(?m)^\s*(?:read|write):.*?\bBW=({PTS_NUMBER})([KMGT]?i?B/s)", body,
        )
        item["runs"].append({
            "run": int(match.group(1)),
            "errors": _number(r"\berr=\s*(\d+)", body, int),
            "duration_seconds": _number(r"/([0-9]+)msec\)", body, lambda n: int(n) / 1000),
            "throughput": _fio_statistics(body, "bw"),
            "summary_throughput": (
                {"value": float(summary.group(1)), "unit": summary.group(2)}
                if summary else None
            ),
            "iops": _fio_statistics(body, "iops"),
        })

    factors = {"B/s": 1 / 1048576, "KiB/s": 1 / 1024, "MiB/s": 1,
               "GiB/s": 1024, "TiB/s": 1048576, "kB/s": 1000 / 1048576,
               "KB/s": 1000 / 1048576, "MB/s": 1000000 / 1048576,
               "GB/s": 1000000000 / 1048576, "TB/s": 1000000000000 / 1048576}
    expected_samples = options.get("expected_samples")
    complete = True
    failed = False
    for item in grouped.values():
        item["throughput"] = item["reported_throughput"]
        runs = item["runs"]
        summaries_known = runs and all(
            run["summary_throughput"] and run["summary_throughput"]["unit"] in factors
            for run in runs
        )
        statistics_known = runs and all(
            run["throughput"] and run["throughput"]["unit"] in factors for run in runs
        )
        native_units_known = summaries_known or statistics_known
        if native_units_known:
            field = "summary_throughput" if summaries_known else "throughput"
            value = "value" if summaries_known else "average"
            samples = [run[field][value] * factors[run[field]["unit"]] for run in runs]
            average = statistics.fmean(samples)
            item["throughput"] = {
                "unit": "MiB/s", "samples": samples, "average": average,
                "deviation_percent": None, "sample_count": len(samples),
            }
            item["throughput_source"] = "native_summary" if summaries_known else "native_statistics"
            reported = item["reported_throughput"]
            if reported:
                factor = factors.get(reported["unit"])
                if factor is not None and abs(reported["average"] * factor - average) > max(0.02, average * 0.001):
                    warnings.append(
                        f"PTS throughput differs from native FIO units for {item['workload']} "
                        f"{item['block_size']} on {item['disk_target']}; native values used"
                    )
        if runs and (not native_units_known or any(
            run["throughput"] is None or run["iops"] is None or run["errors"] is None for run in runs
        )):
            complete = False
            warnings.append("incomplete native FIO run statistics")
        failed |= any(run["errors"] not in (None, 0) for run in runs)
        for field in ("throughput", "iops"):
            measurement = item[field]
            if measurement is None:
                complete = False
                warnings.append(f"missing FIO {field} for {item['workload']} {item['block_size']} on {item['disk_target']}")
            elif expected_samples is not None and measurement["sample_count"] != expected_samples:
                complete = False
                warnings.append(f"expected {expected_samples} FIO samples, found {measurement['sample_count']}")
        if (runs and expected_samples is not None
                and sorted(run["run"] for run in runs) != list(range(1, expected_samples + 1))):
            complete = False
            warnings.append("native FIO run numbers do not match expected repetitions")
    configurations = list(grouped.values())
    expected = int(options["expected_configurations"])
    if len(configurations) != expected:
        complete = False
        warnings.append(f"expected {expected} FIO configurations, found {len(configurations)}")
    complete &= versions["profile"] is not None
    metrics = {
        "kind": "pts_fio", **versions,
        "expected_configuration_count": expected,
        "configuration_count": len(configurations), "configurations": configurations,
    }
    return parsed_result("complete" if complete else "partial",
                         "fail" if failed else "pass" if complete else "unknown", metrics, warnings)


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
        "configured_threads": _number(r"Threads: (\d+)", text, int),
        "configured_duration_seconds": _number(r"Duration: (\d+)", text, int),
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
    text = combined_text(stdout, stderr)
    version = re.search(r"(?m)^stress-ng, version ([^\s]+)", text)
    load = _number(r"--cpu-load (\d+)", text, int)
    duration = _number(r"--timeout (\d+)(?:\s|$)", text, int)
    passed = re.search(r"passed:\s+(\d+):", text)
    failed = re.search(r"failed:\s+(\d+)", text)
    skipped = re.search(r"skipped:\s+(\d+)", text)
    untrustworthy = re.search(r"metrics untrustworthy:\s+(\d+)", text)
    successful = "successful run completed" in text
    passed_count = int(passed.group(1)) if passed else None
    failed_count = int(failed.group(1)) if failed else None
    untrustworthy_count = int(untrustworthy.group(1)) if untrustworthy else None
    complete = successful and passed_count is not None and failed_count is not None
    if failed_count is not None and failed_count > 0:
        outcome = "fail"
    elif untrustworthy_count not in (None, 0):
        outcome = "unknown"
    elif complete and passed_count is not None and passed_count > 0 and failed_count == 0:
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
    if untrustworthy_count not in (None, 0):
        warnings.append("stress-ng reported untrustworthy metrics")
    if stable_warnings:
        warnings.append(f"stable CPU method warning repeated {stable_warnings} times")
    warnings.extend(re.findall(r"(?m)^.*(?:note:.*(?:governor|sched_autogroup)).*$", text))
    if not complete and outcome == "unknown":
        warnings.append("complete stress-ng summary not found")
    metrics = {
        "kind": "stress_ng",
        "version": version.group(1) if version else None,
        "passed_workers": passed_count,
        "failed_workers": failed_count,
        "skipped_workers": int(skipped.group(1)) if skipped else None,
        "metrics_untrustworthy": untrustworthy_count,
        "completion_marker_found": successful,
        "configured_cpu_load_percent": load if load is not None else options.get("cpu_load_percent"),
        "configured_duration_seconds": duration if duration is not None else options.get("duration_seconds"),
        "dispatched_cpu_workers": _number(r"dispatching hogs:\s+(\d+) cpu", text, int),
        "stressor_metrics": [
            {
                "stressor": match.group(1), "bogo_ops": int(match.group(2)),
                "real_time_seconds": float(match.group(3)),
                "user_time_seconds": float(match.group(4)),
                "system_time_seconds": float(match.group(5)),
                "bogo_ops_per_second_real": float(match.group(6)),
                "bogo_ops_per_second_cpu": float(match.group(7)),
            }
            for match in re.finditer(
                rf"(?m)^stress-ng: metrc:\s+\[\d+\]\s+(\S+)\s+(\d+)"
                rf"\s+({PTS_NUMBER})\s+({PTS_NUMBER})\s+({PTS_NUMBER})"
                rf"\s+({PTS_NUMBER})\s+({PTS_NUMBER})\s*$", text
            )
        ],
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
        r"Stream ([0-9][^:\r\n]*):",
    )
    revision = re.search(r"STREAM version \$Revision:\s*([\d.]+)", text)
    if revision:
        versions["tool_version"] = revision.group(1)
    validation_failed = bool(re.search(r"(?i)Failed Validation", text))
    validation_passed = "Solution Validates:" in text
    validation = False if validation_failed else True if validation_passed else None
    by_operation: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    for match in PTS_RESULT_PATTERN.finditer(text):
        operation = match.group("label").strip().rstrip(":")
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
    if validation_failed:
        warnings.append("STREAM reported failed solution validation")
    elif validation is None and revision:
        warnings.append("native STREAM solution validation is missing")
    complete = (
        not missing
        and not unexpected
        and len(by_operation) == len(expected_operations)
        and versions["profile"] is not None
        and (not revision or validation is not None)
    )
    metrics = {
        "kind": "pts_stream",
        **versions,
        "solution_validated": validation,
        "expected_operation_count": len(expected_operations),
        "operation_count": len(by_operation),
        "operations": operations,
    }
    return parsed_result(
        "complete" if complete else "partial",
        "fail" if validation_failed else "pass" if complete else "unknown",
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
    warnings = re.findall(r"(?m)^.*\bWARNING:.*$", text)
    if any(value == 0 for value in finished_seconds) and any(float(value[0]) > 0 for value in pass_reports):
        warnings.append("cuda_memtest reports zero completion time alongside positive elapsed time; reported timings retained separately")
    if not complete and outcome != "fail":
        warnings.append("expected cuda_memtest completion not found")
    return parsed_result(
        "complete" if complete or outcome == "fail" else "partial",
        outcome,
        metrics,
        warnings,
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
    error_counts = [
        int(value)
        for section in re.findall(r"errors:\s*([^\r\n]*?)(?=\s+temps:|$)", text, re.MULTILINE)
        for value in re.findall(r"\b\d+\b", section)
    ]
    gpu_statuses = re.findall(r"(?m)^\s*GPU\s+(\d+):\s+(OK|FAULTY|FAILED)\s*$", text)
    progress = re.findall(r"proc'd:\s*([^\r\n]*?)(?=\s+errors:|$)", text, re.MULTILINE)
    # The display repeats the last completed work while temperatures update.
    # Count each work update once, but retain every temperature/error reading.
    gflops = []
    previous_iterations = None
    for section in progress:
        updates = re.findall(r"(\d+)\s+\(([\d.]+) Gflop/s\)", section)
        iterations = tuple(count for count, _ in updates)
        if iterations != previous_iterations:
            gflops.extend(float(value) for _, value in updates)
        previous_iterations = iterations
    temperatures = [
        float(value)
        for section in re.findall(r"temps:\s*([^\r\n]*)", text)
        for value in re.findall(r"([\d.]+) C", section)
    ]
    processed = [int(value) for value in re.findall(r"proc'd:\s*(\d+)", text)]
    tested = re.search(r"Tested\s+(\d+) GPUs?:", text)
    burn_seconds = _number(r"Burning for\s+(\d+) seconds", text, int)
    devices = [
        {"gpu": int(gpu), "name": name.strip()}
        for gpu, name in re.findall(r"(?m)^GPU\s+(\d+):\s+(.+?)\s+\(UUID:", text)
    ]
    maximum_errors = max(error_counts) if error_counts else None
    tested_count = int(tested.group(1)) if tested else None
    status_ids = {int(gpu) for gpu, _ in gpu_statuses}
    device_ids = {device["gpu"] for device in devices}
    summary_complete = (
        tested_count is not None
        and tested_count > 0
        and len(gpu_statuses) == len(status_ids) == tested_count
        and (not device_ids or device_ids == status_ids)
    )
    all_ok = bool(gpu_statuses) and all(value == "OK" for _, value in gpu_statuses)
    complete = summary_complete and maximum_errors is not None
    if any(value != "OK" for _, value in gpu_statuses) or (
        maximum_errors is not None and maximum_errors > 0
    ):
        outcome = "fail"
    elif complete and all_ok and maximum_errors == 0:
        outcome = "pass"
    else:
        outcome = "unknown"
    metrics = {
        "kind": "gpu_burn",
        "configured_duration_seconds": burn_seconds,
        "tested_gpu_count": tested_count,
        "gpu_devices": devices,
        "gpu_statuses": [
            {"gpu": int(gpu), "status": value} for gpu, value in gpu_statuses
        ],
        "sample_count": len(gflops),
        "progress_record_count": len(progress),
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
        "complete" if complete else "partial",
        outcome,
        metrics,
        [] if complete else ["expected gpu-burn completion not found"],
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
    warnings = []
    for testcase in root.get("testcases") or []:
        matrix = []
        label = testcase.get("name") or "unnamed testcase"
        for row_index, row in enumerate(testcase.get("bandwidth_matrix") or []):
            if not row or any(value in (None, "N/A") for value in row):
                # An empty row preserves its GPU index without inventing values
                # or shifting the remaining columns in the numeric-only schema.
                matrix.append([])
                warnings.append(f"{label}: bandwidth row {row_index} is unavailable; row omitted")
            else:
                matrix.append([float(value) for value in row])
        if not matrix:
            warnings.append(f"{label}: bandwidth matrix is missing")
        total = testcase.get("sum")
        if total in (None, "N/A"):
            warnings.append(f"{label}: bandwidth sum is unavailable")
        testcases.append(
            {
                "name": testcase.get("name"),
                "description": testcase.get("bandwidth_description"),
                "status": testcase.get("status"),
                "sum_gb_per_second": (
                    float(total) if total not in (None, "N/A") else None
                ),
                "bandwidth_matrix_gb_per_second": matrix,
            }
        )
    statuses = [testcase["status"] for testcase in testcases]
    if not statuses:
        warnings.append("nvbandwidth did not report any testcases")
    elif any(status is None for status in statuses):
        warnings.append("nvbandwidth testcase status is missing")
    complete = bool(statuses) and not warnings
    if any(status not in (None, "Passed") for status in statuses):
        outcome = "fail"
    elif complete:
        outcome = "pass"
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
        warnings,
    )


def parser_descriptor(spec: ParserSpec) -> dict[str, Any]:
    return {"name": spec.name, "version": PARSER_VERSION}


def parse_output(spec: ParserSpec, stdout: str, stderr: str) -> ParsedResult:
    try:
        result = spec.function(stdout, stderr, spec.options)
        if spec.name in {'pts_fio', 'pts_stream', 'stressapptest', 'stress_ng', 'sysbench'}:
            # PTS can exit zero without launching a test, including PHP crashes.
            # Keep the real process exit code; distinguish these from format gaps.
            diagnostics = re.findall(
                r'(?m)^.*(?:PHP Fatal error:|\[NOTICE\]\s+pts/\S+ is not installed\.|PTS profile error:|Syntax error:|syntax error:).*$',
                combined_text(stdout, stderr),
            )
            if diagnostics:
                result['outcome'] = 'fail'
                result['warnings'].extend(line.strip() for line in diagnostics)
            if spec.name in {'stressapptest', 'stress_ng', 'sysbench'} and result['metrics'] is not None:
                text = combined_text(stdout, stderr)
                pts = re.search(r"Phoronix Test Suite v([^\s]+)", text)
                profile = re.search(r"Running pinned PTS profile: (pts/[^\s]+)", text)
                result['metrics'].update({
                    'phoronix_test_suite_version': pts.group(1) if pts else None,
                    'profile': profile.group(1) if profile else None,
                })
        return result
    except Exception as exc:
        logger.exception("Parser %s failed", spec.name)
        return parsed_result(
            "parser_error",
            "unknown",
            None,
            [f"{type(exc).__name__}: {exc}"],
        )
