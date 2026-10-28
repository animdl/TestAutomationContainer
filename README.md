# Workstation Benchmark Container

Runs 21 sequential NVMe, RAM, CPU, and NVIDIA GPU checks in Docker. Results and
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

FIO uses the pinned `pts/fio-1.15.0` profile. STREAM and downloaded Git sources
are unpinned, so rebuilding can change tools. Keep the image and workloads
consistent when comparing runs.

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
Fixed-duration stress tests take about 25 minutes, plus the other tests. Tool
output appears after each command finishes. Individual test failures generally
allow later steps to run; cancellation and runner errors stop the sequence.

| Steps | Checks |
| --- | --- |
| 2a–4b | SMART health, short self-tests, and self-test logs for both NVMe devices. |
| 5 | FIO: random/sequential reads and writes, direct I/O, 4 KiB/2 MiB blocks, both filesystems; 16 configurations. |
| 6–7 | `memtester`: 2 GiB, five loops; `stressapptest`: 2000 MB, 300 seconds. |
| 8–10 | CPU stress at 75% for five minutes, a five-second sysbench CPU test, and STREAM Copy/Scale/Add/Triad. |
| 11–12 | CUDA memory stress: five iterations, two passes; GPU burn: five minutes. |
| 13a–13e | Five host/device and device-local bandwidth tests. |
| 14a–14b | CPU stress at 100% for five minutes, then GPU burn for five minutes. |

All workloads are sequential, including the final CPU and GPU stress tests.

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
