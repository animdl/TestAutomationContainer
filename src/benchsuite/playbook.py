"""Benchmark suite definition: execution order, commands, parsers, and timeouts.

Profile versions live only in the Dockerfile; this module refers to base names.
"""

from __future__ import annotations

import os
from typing import NamedTuple

from .parsers import (
    ParserSpec,
    parse_cuda_memtest,
    parse_gpu_burn,
    parse_memtester,
    parse_nvbandwidth,
    parse_nvme_device_self_test,
    parse_nvme_self_test_log,
    parse_nvme_smart,
    parse_pts_fio,
    parse_pts_stream,
    parse_stress_ng,
    parse_stressapptest,
    parse_sysbench,
)

IMAGE = os.environ.get("BENCH_IMAGE", "bench:latest")
BENCH_MOUNTS = (
    (
        os.environ.get("NVME0_BENCH_PATH", "/mnt/hdd/pts-bench-nvme0"),
        "/mnt/bench/nvme0",
    ),
    (
        os.environ.get("NVME1_BENCH_PATH", "/mnt/pts-bench-nvme1"),
        "/mnt/bench/nvme1",
    ),
)

NVME0_DEVICE = "/dev/nvme0n1"
NVME1_DEVICE = "/dev/nvme1n1"
FIO_GROUPS = (("4a", ("read", "write"), "2m"),
              ("4b", ("randread", "randwrite"), "4k"))
MEMTESTER_LOOPS = 1
CUDA_MEMTEST_PASSES = 1
STRESS_SECONDS = 60
CPU_LOAD_PERCENT = 75
COMBINED_CPU_LOAD_PERCENT = 100
NVBANDWIDTH_SAMPLES = 3
STREAM_OPERATIONS = ("Copy", "Scale", "Add", "Triad")

# A step that exceeds its timeout is stopped and recorded as timed_out, and the
# sequence continues. Steps without a configured duration (their runtime depends
# on the hardware) get the default; fixed-duration steps get duration + margin.
DEFAULT_TIMEOUT_SECONDS = 1800
TIMEOUT_MARGIN_SECONDS = 600
STRESS_TIMEOUT_SECONDS = STRESS_SECONDS + TIMEOUT_MARGIN_SECONDS


class Step(NamedTuple):
    step: str
    command: str
    parser: ParserSpec
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS


def fio_options(workloads, block_size):
    return (
        f"fio.type={','.join(workloads)};"
        "fio.engine=libaio;"
        "fio.buffered=0;"
        "fio.direct=1;"
        f"fio.size={block_size};"
        f"fio.auto-disk-mount-points={','.join(target for _, target in BENCH_MOUNTS)}"
    )


STREAM_OPTIONS = f"stream.run-type={','.join(STREAM_OPERATIONS)}"


def pts_command(profile, options, runs=1):
    return (
        f"env PTS_DISPLAY_MODE=BASIC FORCE_TIMES_TO_RUN={runs} PRESET_OPTIONS_VALUES='{options}' "
        f"pts-profile run {profile}"
    )


def cpu_stress_command(load, combined=False):
    command = (
        f"stress-ng --cpu 0 --cpu-load {load} --timeout {STRESS_SECONDS} "
        "--metrics-brief"
    )
    if combined:
        command = "/usr/local/bin/combined-start cpu " + command
    return "stress-ng --version && " + command


def nvbandwidth_step(step, testcase):
    return Step(
        step, f"nvbandwidth -t {testcase} -b 1024 -i {NVBANDWIDTH_SAMPLES} -j",
        ParserSpec("nvbandwidth", parse_nvbandwidth),
    )


# A list entry groups concurrent workloads; every Step entry runs alone.
PLAYBOOK = [
    Step("1a", f"nvme smart-log {NVME0_DEVICE} -o json",
         ParserSpec("nvme_smart", parse_nvme_smart, {"device": NVME0_DEVICE})),
    Step("1b", f"nvme smart-log {NVME1_DEVICE} -o json",
         ParserSpec("nvme_smart", parse_nvme_smart, {"device": NVME1_DEVICE})),
    Step("2a", f"nvme device-self-test {NVME0_DEVICE} -s 1 -w",
         ParserSpec("nvme_device_self_test", parse_nvme_device_self_test, {"device": NVME0_DEVICE})),
    Step("2b", f"nvme device-self-test {NVME1_DEVICE} -s 1 -w",
         ParserSpec("nvme_device_self_test", parse_nvme_device_self_test, {"device": NVME1_DEVICE})),
    Step("3a", f"nvme self-test-log {NVME0_DEVICE} -o json",
         ParserSpec("nvme_self_test_log", parse_nvme_self_test_log, {"device": NVME0_DEVICE})),
    Step("3b", f"nvme self-test-log {NVME1_DEVICE} -o json",
         ParserSpec("nvme_self_test_log", parse_nvme_self_test_log, {"device": NVME1_DEVICE})),
    *[Step(step, pts_command("pts/fio", fio_options(workloads, block_size), runs=2),
           ParserSpec("pts_fio", parse_pts_fio, {
               "expected_configurations": len(workloads) * len(BENCH_MOUNTS),
               "expected_samples": 2,
               "require_latency": True,
               "expected_workloads": workloads,
               "expected_block_size": block_size,
               "expected_targets": [target for _, target in BENCH_MOUNTS],
           })) for step, workloads, block_size in FIO_GROUPS],
    Step("5", f"memtester 2G {MEMTESTER_LOOPS}",
         ParserSpec("memtester", parse_memtester, {"expected_loops": MEMTESTER_LOOPS})),
    Step("6", pts_command("pts/stressapptest",
                          "stressapptest.cpu-threads=32;stressapptest.ram-capacity=2000;"
                          f"stressapptest.duration={STRESS_SECONDS}"),
         ParserSpec("stressapptest", parse_stressapptest), STRESS_TIMEOUT_SECONDS),
    Step("7", cpu_stress_command(CPU_LOAD_PERCENT),
         ParserSpec("stress_ng", parse_stress_ng, {
             "cpu_load_percent": CPU_LOAD_PERCENT, "duration_seconds": STRESS_SECONDS,
         }), STRESS_TIMEOUT_SECONDS),
    Step("8", pts_command("pts/sysbench", "sysbench.run-test=cpu"),
         ParserSpec("sysbench", parse_sysbench)),
    Step("9", pts_command("pts/stream", STREAM_OPTIONS),
         ParserSpec("pts_stream", parse_pts_stream, {"expected_operations": STREAM_OPERATIONS})),
    Step("10a", f"cuda_memtest --disable_all --enable_test 1 --num_passes {CUDA_MEMTEST_PASSES} --verbose 2",
         ParserSpec("cuda_memtest", parse_cuda_memtest, {"test_id": 1, "expected_passes": CUDA_MEMTEST_PASSES})),
    Step("10b", f"cuda_memtest --disable_all --enable_test 2 --num_passes {CUDA_MEMTEST_PASSES} --verbose 2",
         ParserSpec("cuda_memtest", parse_cuda_memtest, {"test_id": 2, "expected_passes": CUDA_MEMTEST_PASSES})),
    Step("10c", f"cuda_memtest --stress --num_iterations 1 --num_passes {CUDA_MEMTEST_PASSES} --verbose 2",
         ParserSpec("cuda_memtest", parse_cuda_memtest, {"test_id": 10, "expected_passes": CUDA_MEMTEST_PASSES})),
    Step("11", f"gpu-burn -tc {STRESS_SECONDS}",
         ParserSpec("gpu_burn", parse_gpu_burn), STRESS_TIMEOUT_SECONDS),
    nvbandwidth_step("12a", "host_to_device_memcpy_ce"),
    nvbandwidth_step("12b", "device_to_host_memcpy_ce"),
    nvbandwidth_step("12c", "host_to_device_bidirectional_memcpy_ce"),
    nvbandwidth_step("12d", "device_to_host_bidirectional_memcpy_ce"),
    nvbandwidth_step("12e", "device_local_copy"),
    nvbandwidth_step("12f", "device_local_copy_sm"),
    [
        Step("13a", cpu_stress_command(COMBINED_CPU_LOAD_PERCENT, combined=True),
             ParserSpec("stress_ng", parse_stress_ng, {
                 "cpu_load_percent": COMBINED_CPU_LOAD_PERCENT, "duration_seconds": STRESS_SECONDS,
             }), STRESS_TIMEOUT_SECONDS),
        Step("13b", f"/usr/local/bin/combined-start gpu gpu-burn -tc {STRESS_SECONDS}",
             ParserSpec("gpu_burn", parse_gpu_burn), STRESS_TIMEOUT_SECONDS),
    ],
]


def execution_groups(playbook):
    return [entry if isinstance(entry, list) else [entry] for entry in playbook]


TESTS = [test for group in execution_groups(PLAYBOOK) for test in group]
