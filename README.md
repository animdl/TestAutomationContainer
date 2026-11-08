# Workstation Benchmark Container

Benchmarks and stress-tests edge workstations (NVMe, RAM, CPU, NVIDIA GPU) with
a fixed, pinned tool set, so results from different runs and machines can be
compared over time. A host-side runner launches each of the 25 steps in its own
Docker container, captures stdout/stderr, parses every tool's output, and writes
`results.json` plus compressed raw logs.

The suite runs privileged containers, writes benchmark files, and applies heavy
hardware load. Verify the target drives before running it.

## Requirements

Run everything in Bash on the Linux workstation, from the repository root.

- Docker with the NVIDIA Container Toolkit, and a working host NVIDIA driver
  (`nvidia-smi` on the host).
- [uv](https://docs.astral.sh/uv/) 0.13.7 or newer. It installs the pinned Python
  (`.python-version`) on first use; the runner itself needs no other packages.
- `findmnt` (util-linux).
- Two mounted NVMe filesystems with dedicated benchmark directories.
- Internet access to build the image and for uv's first run.

## Layout

| Path | Runs on | Contents |
| --- | --- | --- |
| `Dockerfile` | build | The pinned image. Its build context is only `container/`. |
| `container/` | in the image | `pts-profile` (pinned PTS launcher), `fio_profile.py` (FIO latency customization), `cuda_memtest_patch.py` (test 10 size fix, applied at build), `combined-start` (CPU/GPU start barrier). |
| `src/benchsuite/` | host | `playbook.py` (suite definition), `runner.py`, `capture.py`, `parsers.py`, `results.schema.json`. |
| `scripts/` | host | `verify-mounts`. |

## 1. Build

```bash
export BENCH_IMAGE=bench:latest
docker build -t "$BENCH_IMAGE" .

docker run --rm --gpus all "$BENCH_IMAGE" nvidia-smi
docker run --rm "$BENCH_IMAGE" pts-profile verify
```

`CUDA_ARCH` defaults to `89`; set it with `--build-arg` for other GPUs. The build
fails if any pinned PTS profile did not install, even when PTS reported success.

### What is pinned

| Input | Pin | Where |
| --- | --- | --- |
| Base image | `nvidia/cuda:12.6.3-devel-ubuntu24.04` by digest | `CUDA_IMAGE_DIGEST` |
| Ubuntu packages | Archive snapshot `20261103T000000Z` | `UBUNTU_SNAPSHOT` |
| stress-ng, memtester, nvme-cli | Exact package versions | `STRESS_NG_VERSION`, `MEMTESTER_VERSION`, `NVME_CLI_VERSION` |
| GCC 13 (builds every source-built test) | Exact package version | `GCC_VERSION` |
| PTS, gpu-burn, nvbandwidth, cuda_memtest | Full Git commit hashes, checked after checkout | `*_COMMIT` |
| PTS test profiles | `pts/fio-1.15.0`, `pts/stream-1.3.4`, `pts/stressapptest-1.0.1`, `pts/sysbench-1.1.0` | `PTS_TESTS` |

All pins are Dockerfile build arguments. The snapshot makes every Ubuntu package
resolve identically on each rebuild; the explicit versions make the build fail
if the snapshot ever stops providing them. NVIDIA's CUDA apt repository is not
snapshotted, but the build installs nothing from it. When you change a pin,
rebuild, revalidate tool output, and treat the next run as a new baseline. If
you change `CUDA_VERSION`, supply the matching `CUDA_IMAGE_DIGEST`.

`PTS_TESTS` is the only place PTS versions are set. The build writes them to
`/usr/local/share/benchmark/pts-profiles`; the runner uses base names such as
`pts/fio`, and `pts-profile` resolves them to the pinned version. It also refuses
to launch a profile unless every option has an explicit preset, so a profile
change cannot silently add workloads.

The FIO 1.15.0 launcher (fio 3.29) gets one guarded change,
`benchmark-fio-latency-v1`: it enables completion-latency measurement and the
50/95/99th percentiles. The build fails if the upstream block differs, and each
FIO step logs the launcher's SHA-256. Changing the FIO pin requires reviewing
`container/fio_profile.py`.

cuda_memtest gets one guarded source change before it is compiled,
`benchmark-cuda-memtest-test10-size-v1`. Upstream, the test 10 (stress) kernels
take the allocation size as a 32-bit `int`, so an allocation of 2 GiB or more is
truncated modulo 2^32 (on a 16 GB card, 2,836 of 15,124 MiB were tested) while
the reported bandwidth still counts the whole allocation. The patch widens the
size and the per-block offset to `unsigned long`, and the build fails if the
upstream source differs. Changing `CUDA_MEMTEST_COMMIT` requires reviewing
`container/cuda_memtest_patch.py`.

## 2. Verify mounts

Mount the intended filesystems first, and adjust these paths to your setup:

```bash
lsblk -o NAME,TYPE,SIZE,FSTYPE,MOUNTPOINTS

export NVME0_BENCH_PATH=/mnt/hdd/pts-bench-nvme0
export NVME1_BENCH_PATH=/mnt/pts-bench-nvme1
sudo mkdir -p "$NVME0_BENCH_PATH" "$NVME1_BENCH_PATH"

bash scripts/verify-mounts
```

The paths map to `/mnt/bench/nvme0` and `/mnt/bench/nvme1` in the containers.
The check confirms they are on different filesystems and writable inside Docker;
it does not prove they are different physical drives. The runner does not call it.

NVMe health and self-test steps target `/dev/nvme0n1` and `/dev/nvme1n1`. If your
device names differ, edit `NVME0_DEVICE` and `NVME1_DEVICE` in
`src/benchsuite/playbook.py`; the directory variables do not change them.

## 3. Run

```bash
export WORKSTATION_NAME=workstation1
uv run run-playbook
```

Results go to `benchmark-results/YYYYMMDD-HHMMSS/`. To use another new or empty
directory instead, pass it: `uv run run-playbook benchmark-results/manual-run-001`.

| Environment variable | Default | Purpose |
| --- | --- | --- |
| `WORKSTATION_NAME` | the hostname, with a warning | Human-readable machine identity in the results. |
| `BENCH_IMAGE` | `bench:latest` | Image to run. |
| `NVME0_BENCH_PATH`, `NVME1_BENCH_PATH` | as above | Host benchmark directories. |

Every invocation runs the full sequence. Tool output appears after each step
finishes. A failed or timed-out step does not stop the run; cancellation
(Ctrl-C or SIGTERM) and runner errors do, and cancellation removes the active
containers.

### Sequence

The defaults are short, for output capture and basic sanity checks rather than
prolonged stability validation. Durations and counts are constants at the top of
`src/benchsuite/playbook.py`; `PLAYBOOK` there defines the order. A list entry in
`PLAYBOOK` runs its steps concurrently; every other entry runs alone.

| Steps | Checks |
| --- | --- |
| 1a–3b | NVMe SMART health, short self-test (`-s 1`), and self-test log, for each device. |
| 4a–4b | FIO through PTS: sequential read/write at 2 MiB, then random read/write at 4 KiB, on both targets. Libaio, direct I/O, queue depth 64, one job, 1 GiB file; four configurations per step, two runs each (about 8 minutes in total). Latency comes from the same runs. |
| 5 | `memtester` 2 GiB, one loop. |
| 6 | stressapptest through PTS: 2000 MB, 32 memory-copy threads, 60 s. |
| 7 | Native stress-ng on all CPUs at 75% load, 60 s. |
| 8 | sysbench CPU through PTS, all logical CPUs, its default 90 s. |
| 9 | STREAM through PTS: Copy, Scale, Add, Triad (100 internal iterations each). |
| 10a–10c | cuda_memtest tests 1, 2, and 10 (stress), one pass each. |
| 11 | gpu-burn with tensor cores, 60 s. After the burn it waits 5 s (`-stts 5`, upstream default 30) for its workers to exit before force-killing them. |
| 12a–12f | nvbandwidth: host↔device copies (each direction, then bidirectional), `device_local_copy`, `device_local_copy_sm`; 1024 MiB, three samples. |
| 13a–13b | stress-ng at 100% and gpu-burn together, 60 s, in separate containers. |

Steps 13a and 13b wait at a shared start barrier (`combined-start`) until both
are ready, then start within its polling interval; the barrier gives up after
120 seconds if its peer never arrives. The output directory must support Docker
bind mounts for this.

FIO uses `FORCE_TIMES_TO_RUN=2` for multi-sample statistics; other PTS steps use
1. PTS runs with `PTS_DISPLAY_MODE=BASIC`, so the underlying tool output is kept.
Memtester, cuda_memtest, STREAM, and NVMe self-test runtimes depend on the hardware.

### Timeouts

Each step has a timeout (`timeout_seconds` in the results). Steps with a set
duration get that duration plus 600 seconds; the rest get 1800 seconds. A step
that exceeds its timeout is stopped, its container removed, and it is recorded
as `timed_out` with outcome `fail`; the run continues with the next step.

## Results

Each run directory holds `results.json` and `raw/*.stdout.log.gz` /
`raw/*.stderr.log.gz`. Keep the whole directory even when the runner reports
issues. `results.json` follows `src/benchsuite/results.schema.json` and is
checkpointed after every step, so an interrupted run still leaves a valid file
(with uncompressed logs for unfinished steps).

It records:

- **Identity:** `system.workstation_name`; `system.host` (OS and kernel);
  `system.hardware` (CPU model, logical CPUs, memory, GPUs with UUID, driver and
  compute capability, and model/serial/firmware for each benchmarked NVMe device);
  and `system.pts_summary`, the first PTS system summary. That summary comes from
  inside Docker, so its OS line describes the container.
- **Provenance:** image ID and digests, runner and parser versions, SHA-256 of
  every runner source file, and a hash of the step list and commands.
- **Per step:** command, timeout, timings, exit code, parsed metrics, warnings,
  and hashes and sizes of both raw logs.

Each step has three separate statuses:

- `execution_status`: whether the command ran (`completed`, `failed`,
  `timed_out`, `interrupted`, `launch_failed`).
- `collection_status`: whether parsing found everything (`complete`, `partial`,
  `parser_error`).
- `outcome`: `pass`, `fail`, or `unknown`. There are no performance thresholds yet.

Execution and parsing are separate: review actual command failures rather than
assuming every reported issue is a parser mismatch. PTS can exit zero without
running a test, so missing-profile notices, PHP fatal errors, and shell syntax
errors in PTS output count as failures; `exit_code` still shows the real exit code.

| Runner exit code | Meaning |
| --- | --- |
| `0` | Every step completed, parsed fully, and passed. |
| `1` | The sequence finished with issues. |
| `2` | Setup or runner error. |
| `130` | Interrupted. |

### Notes on metrics

- **FIO:** throughput is normalized to MiB/s from fio's native output (whole-run
  summaries, falling back to sampled statistics). The PTS value and unit are kept
  as `reported_throughput`, and a mismatch produces a warning.
  `completion_latency` is per run, in microseconds: mean, max, and p50/p95/p99.
  Enabling latency instrumentation can affect throughput.
- **cuda_memtest:** `finished_seconds` is the tool's wall time, rounded to 0.1 s,
  so 0.0 does not mean zero work. Test 10 also reports CUDA event times in
  milliseconds and bandwidth. The tool computes that bandwidth as
  (2 × iterations + 1) × allocated MiB / elapsed ms and labels it GB/s; one MiB/ms is 1.049 GB/s, so the figure
  is about 5% below true GB/s. Allocation warnings, where the largest possible
  allocation is slightly smaller than reported free memory, are kept without
  failing the test.
- **gpu-burn:** a forced kill of workers that ignored SIGTERM is recorded as a
  warning. It does not change the GPU status, which gpu-burn decides from error
  counts alone.
- **gpu-burn:** GFLOPS statistics count each work update once (arithmetic means,
  not time-weighted); temperature and error readings include every progress line.
- **STREAM:** a reported validation failure fails the step.
- **nvbandwidth:** unavailable matrix rows are stored as empty lists with warnings.

## Development

```bash
uv sync
uv run python -m unittest discover -s tests -v
```

`tests/` is not tracked in Git yet. Version history is in [CHANGELOG.md](CHANGELOG.md).
