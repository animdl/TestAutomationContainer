# Workstation Benchmark Container

Runs 21 NVMe, RAM, CPU, and NVIDIA GPU checks in Docker. Results and
compressed logs are saved on the host.

## Requirements

Run the commands below in Bash on the Linux workstation, from the repository root.

- Docker with NVIDIA GPU support and a working host NVIDIA driver.
- Host Python 3.9+, Bash, and `findmnt`. The runner needs no Python packages.
- Two mounted NVMe filesystems with dedicated benchmark directories.
- Internet access to build the image.

The suite uses privileged containers, writes benchmark files, and applies heavy
hardware load. Verify the target drives before running it.

## 1. Build

```bash
export BENCH_IMAGE=bench:latest
docker build -t "$BENCH_IMAGE" .

docker run --rm --gpus all "$BENCH_IMAGE" nvidia-smi
docker run --rm "$BENCH_IMAGE" stress-ng --version
docker run --rm "$BENCH_IMAGE" pts-profile verify
docker run --rm "$BENCH_IMAGE" phoronix-test-suite list-installed-tests
```

Defaults: Ubuntu 24.04, CUDA 12.6.3, CUDA architecture `89`. Set `CUDA_VERSION`
and `CUDA_ARCH` through Docker's `--build-arg` if needed for your GPU.

Stress-ng is installed directly from Ubuntu packages in the container and run
without PTS. Each CPU stress step captures `stress-ng --version`; the package
version can change when the image is rebuilt.

The Dockerfile's `PTS_TESTS` argument is the single source of PTS profile version
pins. The build writes these pins into `/usr/local/share/benchmark/pts-profiles`.
The runner refers to base profile names; `pts-profile` resolves them through that
manifest and invokes PTS with the exact version, avoiding latest-version lookup.
The build verifies every pinned installation and launcher and fails with install
log excerpts if any are missing or failed, even when PTS returns exit code zero.
The image explicitly installs profile extraction and build dependencies, including
`bzip2` for STREAM and PHP ZIP support for PTS.
APT package lists remain available during PTS installation for dependency
resolution. The PTS path override includes its required trailing slash.
Stressapptest is built with `-include cstdint` to supply the integer types its
older source assumes are included indirectly; the flag applies only to that
profile's installation.
When updating pins, check whether the new profile requires changes to the
runner's workload options.
PTS itself and the other Git sources remain unpinned. Retain the built image
for comparisons.

FIO is pinned to `pts/fio-1.15.0` (fio 3.29). It uses the profile's single-job
launcher with Linux AIO, direct I/O, and `fio.buffered=0`. No job-count preset
is supplied because this profile does not expose one.

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
The rebuilt image must include the Dockerfile-generated profile manifest and
`pts-profile` helper.

Results default to `benchmark-results/YYYYMMDD-HHMMSS/`. To use another new or
empty directory instead:

```bash
python3 ./run-playbook ./benchmark-results/manual-run-001
```

Every invocation runs the full sequence; there is no resume or step selection.
The defaults are short runs for parser capture. PTS stages use the selected profiles' supported options and default arguments. Tool
output appears after each command finishes. Individual test failures generally
allow later steps to run; cancellation and runner errors stop the sequence.

| Steps | Checks |
| --- | --- |
| 2a–4b | SMART health, short self-tests, and self-test logs for both NVMe devices. |
| 5 | FIO: all 16 configurations, two PTS runs each to capture multi-sample statistics; each run has a 20-second workload, five-second start delay, and five-second ramp. |
| 6–7 | `memtester`: 2 GiB, one loop; PTS stressapptest: 2000 MB, 32 memory-copy threads, 60 seconds. |
| 8–10 | Native CPU stress at 75% for 60 seconds, one 90-second sysbench CPU run, and one PTS run for each STREAM operation. |
| 11–12 | CUDA memory stress: one iteration, one pass of test 10; GPU burn: 60 seconds. |
| 13a–13e | Five host/device and device-local bandwidth tests, three samples each. |
| 14a–14b | Native CPU stress at 100% and GPU burn together for 60 seconds. |

`PLAYBOOK` in `run-playbook` defines execution order and concurrency. Tuple
entries run sequentially; a list entry runs its member tuples concurrently.
The suite metadata and collected result order are derived from this declaration.
Steps before 14 run sequentially. Steps 14a and 14b run in separate concurrent
containers with separate statuses and logs. A temporary shared start barrier
holds both workloads until their commands reach the barrier. Both workloads
then start within the barrier's polling interval (plus OS scheduling delays).
The barrier times out after 120 seconds if its peer never arrives. Cancellation
attempts to stop both containers. The host output directory must support Docker
bind mounts for this barrier.

FIO uses `FORCE_TIMES_TO_RUN=2` to capture multi-sample statistics. All other PTS
commands use `FORCE_TIMES_TO_RUN=1`. These explicit counts avoid dynamic extra
runs. Native stress-ng, stressapptest, and GPU burn use `STRESS_SECONDS`
(60 by default). Set `CPU_LOAD_PERCENT` (75) for standalone CPU stress and
`COMBINED_CPU_LOAD_PERCENT` (100) for combined CPU/GPU stress in `run-playbook`.
Stress-ng uses all configured CPUs and reports brief native metrics. These
controls are also saved in the parsed metrics. Sysbench uses its CPU preset,
all detected logical CPUs, and default 90-second duration.
NVMe self-tests explicitly select the short test (`-s 1`), not the extended test.
SMART and self-test log commands only read status.

FIO takes roughly 16 minutes across 16 configurations with two runs each,
before PTS overhead. Its runtime and startup timings come from the pinned
1.15.0 profile (fio 3.29). STREAM is pinned to 1.3.4 to avoid the broken
exit-status line in the 1.3.5 launcher.
STREAM retains the profile binary's internal 100 iterations per operation even
with one PTS run. Memtester, CUDA memory testing, STREAM, and the controller's
short NVMe self-tests have hardware-dependent runtimes; they do not have a
60-second wall-clock limit. These settings are for output capture and basic
sanity checks, rather than prolonged hardware stability validation.

## Capture after this update

Rebuild the image and verify the installed profile versions before the target
run. Treat the new run as a new baseline: tool versions, FIO behavior, and the
combined CPU/GPU contention have changed. Monitor temperatures, power, and
throttling during combined load, and keep cooling and power settings consistent
for later comparisons. Confirm the benchmark files land on the intended mounts.

The parser recognizes missing-profile notices and PHP fatal errors as failed
outcomes even if PTS returns zero. `execution_status` and `exit_code` retain the
actual process result; these failures are recorded in per-test warnings.
Sysbench selects `sysbench.run-test=cpu` without injecting a duration override.
PTS source and generated launchers are left unmodified. Rebuild the image to
apply the FIO/STREAM pins.
`pts-profile` also checks that every profile option has an explicit workload
preset before launching PTS, so profile changes cannot silently add workloads or
fall through to broken noninteractive prompts.
Keep the entire output directory, including `results.json` and every compressed
raw log, even if parsing reports issues or the runner exits with code 1. PTS
uses `PTS_DISPLAY_MODE=BASIC` to retain underlying tool output for that work.
Execution success and parsing completeness are separate; review actual command
failures rather than assuming every reported issue is a parser mismatch.

## Results

Each run produces `results.json` and `raw/*.stdout.log.gz` / `raw/*.stderr.log.gz`.
The JSON contains metrics, warnings, timings, image/source identities, and log
sizes and hashes. Its format is defined by `results.schema.json` (version 5).

- `execution_status`: command execution state.
- `collection_status`: parsing completeness.
- `outcome`: `pass`, `fail`, or `unknown`; no performance regression thresholds.

FIO retains PTS summaries and native per-run bandwidth/IOPS statistics and whole-run bandwidth summaries. When
native bandwidth is available, `throughput` uses normalized MiB/s from whole-run summaries when available
(falling back to sampled statistics), and
`reported_throughput` preserves the PTS value and unit. Unit discrepancies
produce warnings. Missing deviation and tool versions remain null. Shell
syntax errors produce failed outcomes even when measurements are collected.
Stress-ng metrics and environment notices, and CUDA allocation/timing warnings,
are retained. Existing captures are not rewritten; the new schema applies to
newly parsed results.

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

Schema/parser version 5 distinguishes FIO `throughput_source` values
`native_summary`, `native_statistics`, and `pts`; each native run also retains
`summary_throughput` separately from sampled `throughput` statistics. STREAM
records `solution_validated` (null when unavailable) and the native revision.
A reported STREAM validation failure produces a failed outcome. GPU burn
`sample_count` and GFLOPS summaries count work updates once, while
`progress_record_count` counts all display records. Temperature and error
readings still include repeated progress records. GFLOPS means are arithmetic
means across work updates, not time-weighted measurements.

See [the target run review](benchmark-review-20261031.md) for findings from
the 2026-10-31 capture. Replay and regression checks require `jsonschema`:
`python -m unittest test_benchmark_parsers -v`. The replay check skips when
the original capture directory is absent.
