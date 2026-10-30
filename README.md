# Workstation Benchmark Container

Runs 21 NVMe, RAM, CPU, and NVIDIA GPU checks in Docker. Results and
compressed logs are saved on the host.

## Requirements

Run the commands below in Bash on the Linux workstation, from the repository root.

- Docker with NVIDIA GPU support and a working host NVIDIA driver.
- Host Python 3.10+, Bash, and `findmnt`. The runner needs no Python packages.
- Two mounted NVMe filesystems with dedicated benchmark directories.
- Internet access to build the image.

The suite uses privileged containers, writes benchmark files, and applies heavy
hardware load. Verify the target drives before running it.

## 1. Build

```bash
export BENCH_IMAGE=bench:latest
docker build -t "$BENCH_IMAGE" .

docker run --rm --gpus all "$BENCH_IMAGE" nvidia-smi
docker run --rm "$BENCH_IMAGE" phoronix-test-suite list-installed-tests
```

Defaults: Ubuntu 24.04, CUDA 12.6.3, CUDA architecture `89`. Set `CUDA_VERSION`
and `CUDA_ARCH` through Docker's `--build-arg` if needed for your GPU.

The Dockerfile's `PTS_TESTS` argument is the source of PTS profile version pins.
The runner uses unversioned profile names to select the installed versions.
Keep one installed version per profile in the image. When updating pins, check
whether the new profile requires changes to the runner's workload options.
PTS itself and the other Git sources remain unpinned. Retain the built image
for comparisons.

FIO is pinned to `pts/fio-2.0.0` (fio 3.35) to use an unmodified upstream launcher
without the argument mismatch found in profiles 2.1.0 and 2.2.0. It explicitly
uses one job, Linux AIO, direct I/O, and `fio.buffered=0`.

## 2. Verify mounts

Mount the intended filesystems first. Adjust these host paths to match your setup:

```bash
lsblk -o NAME,TYPE,SIZE,FSTYPE,MOUNTPOINTS

export NVME0_BENCH_PATH=/mnt/hdd/pts-bench-nvme0
export NVME1_BENCH_PATH=/mnt/pts-bench-nvme1
sudo mkdir -p "$NVME0_BENCH_PATH" "$NVME1_BENCH_PATH"

bash ./verify-mounts
```

The paths default to those shown above and map to `/mnt/bench/nvme0` and
`/mnt/bench/nvme1` inside containers. Verification checks that they use different
filesystem devices and are writable inside Docker. It does not prove that they
belong to different physical drives. The runner does not invoke this check.

NVMe health and self-test commands separately target `/dev/nvme0n1` and
`/dev/nvme1n1`. If your device names differ, edit `NVME0_DEVICE` and `NVME1_DEVICE`
in `run-playbook`. Changing the directory variables does not change these devices.

## 3. Run

In the same shell, after mount verification succeeds:

```bash
python3 ./run-playbook
```

Run on the host, keeping `run-playbook`, `benchmark_capture.py`, and
`benchmark_parsers.py` together. `BENCH_IMAGE` defaults to `bench:latest`.

Results default to `benchmark-results/YYYYMMDD-HHMMSS/`. To use another new or
empty directory instead:

```bash
python3 ./run-playbook ./benchmark-results/manual-run-001
```

Every invocation runs the full sequence; there is no resume or step selection.
Fixed-duration stress tests take about 20 minutes, plus the other tests. Tool
output appears after each command finishes. Individual test failures generally
allow later steps to run; cancellation and runner errors stop the sequence.

| Steps | Checks |
| --- | --- |
| 2a–4b | SMART health, short self-tests, and self-test logs for both NVMe devices. |
| 5 | FIO: random/sequential reads and writes, direct I/O, 4 KiB/2 MiB blocks, both filesystems; 16 configurations. |
| 6–7 | `memtester`: 2 GiB, five loops; PTS stressapptest: 2000 MB, 32 threads, 300 seconds, `-W`. |
| 8–10 | CPU stress at 75% for five minutes, a five-second sysbench CPU test, and STREAM Copy/Scale/Add/Triad. |
| 11–12 | CUDA memory stress: five iterations, two passes; GPU burn: five minutes. |
| 13a–13e | Five host/device and device-local bandwidth tests. |
| 14a–14b | PTS CPU stress at 100% and GPU burn together for five minutes. |

Steps before 14 run sequentially. Steps 14a and 14b run in separate concurrent
containers with separate statuses and logs. A temporary shared start barrier
holds GPU burn until PTS reaches the actual stress-ng launch. Both workloads
then start within the barrier's polling interval (plus OS scheduling delays).
The barrier times out after 120 seconds if its peer never arrives. Cancellation
attempts to stop both containers. The host output directory must support Docker
bind mounts for this barrier.

Stressapptest, stress-ng, and sysbench now run through PTS. Explicit presets
preserve the original workloads, including 75%/100% CPU load, five-minute stress
durations, and a five-second sysbench CPU run on all detected logical CPUs.
These tests use `FORCE_TIMES_TO_RUN=1`; FIO and STREAM retain PTS repetition
behavior. FIO can take over an hour across 16 configurations and repeated runs.

## Capture after this update

Rebuild the image and verify the installed profile versions before the target
run. Treat the new run as a new baseline: tool versions, FIO behavior, and the
combined CPU/GPU contention have changed. Monitor temperatures, power, and
throttling during combined load, and keep cooling and power settings consistent
for later comparisons. Confirm the benchmark files land on the intended mounts.

Parser changes are intentionally deferred until target output is available.
Keep the entire output directory, including `results.json` and every compressed
raw log, even if parsing reports issues or the runner exits with code 1. PTS
uses `PTS_DISPLAY_MODE=BASIC` to retain underlying tool output for that work.
Execution success and parsing completeness are separate; review actual command
failures rather than assuming every reported issue is a parser mismatch.

## Results

Each run produces `results.json` and `raw/*.stdout.log.gz` / `raw/*.stderr.log.gz`.
The JSON contains metrics, warnings, timings, image/source identities, and log
sizes and hashes. Its format is defined by `results.schema.json` (version 3).

- `execution_status`: command execution state.
- `collection_status`: parsing completeness.
- `outcome`: `pass`, `fail`, or `unknown`; no performance regression thresholds.

Unavailable bandwidth rows are represented by empty lists with warnings.
Results are checkpointed during execution; interrupted collection can leave
unfinished records and uncompressed logs. Cancellation attempts to remove the
active container; cleanup failures are recorded in warnings and stderr.

| Exit code | Meaning |
| --- | --- |
| `0` | All commands completed, parsed fully, and passed. |
| `1` | Sequence finished with issues. |
| `2` | Handled setup or runner error. |
| `130` | Interrupted. |
