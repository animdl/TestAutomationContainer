# Phase 2: Sensor collection — implementation plan

## Context

Phase 1 runs 25 benchmark steps in privileged Docker containers (`run-playbook`, `src/benchsuite/runner.py`) and writes `results.json`. It records no host telemetry, so CPU/GPU overlap, thermal throttling and drive heat can't be seen (the 15-minute idle baseline was dropped in favour of this). Phase 2 adds a 1 Hz collector of whole-system readings. It runs for the whole playbook, blocks startup until the required readings work, and stops the playbook if they're lost. The first target is Intel CPUs, discrete NVIDIA GPUs and Ubuntu 24.04 (workstation1: i9-14900K hybrid, RTX 4080, two NVMe drives).

**Out of scope:** installation automation, drive auto-detection, and headless orchestration from a single REST call (Phase 2.5); acceptance thresholds, the frontend, and decoding values for display (Phase 3).

## Decisions (2026-11-09, revised 2026-11-23)

- **One privileged sensor container holds the whole collector**: Python, nvidia-ml-py and turbostat. The user is already in the docker group (root-equivalent) and every benchmark container already runs `--privileged`, so an unprivileged host process wouldn't add real protection.
  - It's a separate image from the benchmark image, so that image's digest and baseline aren't affected.
  - The host needs only Docker and the NVIDIA Container Toolkit, which it already needs.
  - The host package stays standard-library only.
- **Whole-system granularity.** CPU readings come from one turbostat summary row per interval; there's nothing per core. The average clock mixes P- and E-cores on hybrid CPUs, so a shift in load between them moves it without any throttling. The README documents this.
- **Required (blocks startup; loss stops the run):**
  - CPU: package power (`PkgWatt`), effective clock (`Bzy_MHz`), package temperature (`PkgTmp`) and utilization (`Busy%`), all from turbostat;
  - GPU: temperature, utilization, graphics/memory clocks, board power and VRAM used/total, from NVML;
  - each benchmark drive's NVMe Composite temperature, from hwmon.
  
  `Busy%` is hardware active-state residency, not OS-scheduled time. They're usually close, and the README notes the difference.
- **Best-effort**, each with an explicit missing reason:
  - `package_throttle_count`;
  - the raw NVML clock-event-reasons bitmask;
  - the GPU enforced power limit;
  - every hwmon temperature and fan input (generic, no special cases per chip, so NVMe chips are included and the required Composite temperature also appears here);
  - `/proc/pressure`;
  - RAM/swap from `/proc/meminfo`.
- **Stop rule:** stop if any required reading is missing for **N consecutive samples** (default 5, configurable). Isolated misses are recorded and tolerated.
- **Drives:** resolve each bench path to its physical device and monitor those devices. Phase 2 passes today's `BENCH_MOUNTS`, but the resolver accepts any list of paths, so the Phase 2.5 drive-detection script plugs in without collector changes. No drive count or `nvme0`/`nvme1` naming is hard-coded in the collector.
- **Storage:** `sensors.jsonl` in the run directory (`benchmark-results/<timestamp>/` by default), next to `results.json` and `raw/`, kept uncompressed under that name for the Phase 3 timeline chart. The runner sets `run_id` from its own clock, so under `run-suite` it can differ from the directory name by the startup delay. One line per source and device per sample. `results.json` gets a `sensors` block with the file name, SHA-256 and summary.
- **Dependencies:** only nvidia-ml-py, pinned by exact version and hash inside the sensor image. There's no psutil; `/proc` and hwmon are read directly.

## Architecture

```
run-suite (supervisor, host, stdlib only)
 ├─ docker run --privileged --gpus all SENSOR_IMAGE collect-sensors …
 │    collector process: NVML, hwmon, /proc
 │    └─ turbostat --Summary subprocess (same container)
 └─ run-playbook <run dir>   (existing runner; still runnable standalone)
```

1. The supervisor reserves the run directory before any Docker call (new, or existing and empty; otherwise exit 2), records a (wall, monotonic) clock pair, and starts the sensor container. The container gets:
   - the run directory mounted read-write, so it can write `sensors.jsonl`;
   - each bench path bind-mounted read-only at the same path with `--mount`, so it can resolve the drives and a missing path fails;
   - `/lib/modules` mounted read-only, so turbostat can load `msr`.
2. The collector resolves the drives and starts turbostat. It polls its other sources each interval and writes JSONL. Once a full sample has every required reading, with at least one GPU and a drive for every bench path, it prints one `ready` line to stdout, with the resolved devices. Miss counting starts after `ready`. If that doesn't happen within a startup timeout (default 30 s), it exits non-zero; the supervisor then exits 2 and never starts the runner.
3. The supervisor compares the resolved devices with `NVME_DEVICES` (see Open choice), then launches `run-playbook <dir>` as a subprocess, with no added delay.
4. The collector enforces the N-miss rule itself and exits non-zero with a reason on stderr. If the container exits while the runner is still going, the supervisor treats it as a failure: it SIGTERMs the runner, which already turns that into `interrupted` and removes its containers.
5. When the runner exits, the supervisor stops the sensor container and removes it (`capture.remove_container`). It records a second clock pair and hashes `sensors.jsonl`. It then adds the `sensors` block to `results.json` via `capture.atomic_save`. The block includes `stop_reason`, any clock-step or drive mismatch warning, the sensor image ID and digest (`capture.inspect_image`), and `sensor_collector_version` from the JSONL `meta` header. The supervisor returns the runner's exit code, or 2 for a sensor failure.
6. SIGINT and SIGTERM to the supervisor are passed on to the runner. The supervisor then waits for the runner and stops the sensor container.

## Timestamps

- **Same clock everywhere.** Docker uses no time namespace, so the runner's `utc_now()`, the collector's `time.time()` and turbostat all read the same kernel wall clock. Times use the runner's format: ISO-8601 UTC with a `Z` suffix and microseconds.
- **One `at` per record.**
  - turbostat records are stamped when their line arrives. The file header says their values are averages over the interval ending at `at`; the interval start is the `at` of the previous turbostat record that has readings. Stale records (written at a tick when no row arrived within 1.5 intervals, with empty `readings`) are stamped at the tick and don't start an interval.
  - Everything else is stamped when it's read. NVML utilization is a driver-window average reported at poll time. NVIDIA documents a product-dependent window of about ⅙ to 1 second, so it can span a whole 1 Hz sample.
  - Each source keeps its own times. Records are never merged into one row per second; the frontend can resample.
- **Detect clock jumps instead of correcting them.** The supervisor compares the (wall − monotonic) offset at the start and end of the run. If it changed by more than 100 ms, it adds a clock-step warning to the `sensors` block. There are no monotonic fields in the records and no runner change for them. Ubuntu's NTP normally adjusts the clock gradually rather than jumping it after boot.
- **Output buffering.** Confirm that turbostat flushes each interval when piped. If it doesn't, run it under `stdbuf -oL`; arrival-time stamping depends on this. The collector writes and flushes its own stdout line by line.
- **Known skew, documented but not corrected in Phase 2.** A step's `started_at` is taken just before `docker run`, so container startup (roughly 0.3–1 s) counts as part of the step. That's about one 1 Hz sample of idle at each step start. The `combined-start` barrier's log line already gives a precise start for steps 13a/13b.

## Components and files

- **`src/benchsuite/runner.py`**: one change. `prepare_output_directory()` accepts a directory whose only entry is `sensors.jsonl`, so the collector can start first. `run-playbook` keeps working standalone.
- **`src/benchsuite/supervisor.py`** (new, entry `run-suite`, stdlib only):
  - Args: `[output_dir] --interval 1.0 --miss-limit 5 --startup-timeout 30 --ambient-c <float>`, plus `SENSOR_IMAGE` (default `bench-sensors:latest`).
  - Ambient temperature is optional and recorded as unavailable ("not provided") when omitted.
  - Reuses `playbook.BENCH_MOUNTS`/`NVME_DEVICES` and `capture.remove_container`, `inspect_image`, `atomic_save`, `sha256_path` and `timestamp`.
- **`sensor-container/`** (new; runs in the sensor image, built from the repository root like `container/` is for the benchmark image):
  - `Dockerfile`, multi-stage, with `COPY` paths relative to the repository root (`sensor-container/…`):
    - Ubuntu 24.04, pinned by digest and archive snapshot the same way as the benchmark image, with an exact `python3` package version.
    - turbostat built from a pinned kernel source tarball (version + SHA-256, `tools/power/x86/turbostat`).
    - `kmod`, for loading `msr`.
    - A venv installed from `requirements.txt` with `--require-hashes`.
    - `ENV NVIDIA_DRIVER_CAPABILITIES=utility`, so the toolkit injects NVML.
  - `requirements.txt`: exact `nvidia-ml-py==…` with hash, using the current release at implementation time.
  - `collect-sensors`: a POSIX `sh` wrapper installed in `/usr/local/bin/` that runs `exec /opt/venv/bin/python -m collector "$@"`, so Python is PID 1 and receives `docker stop`'s SIGTERM.
  - `collector/` package, standalone with no imports from `benchsuite`, copied to `/opt/collector/` with `PYTHONPATH=/opt`:
    - `__init__.py`: `SENSOR_COLLECTOR_VERSION` (distinct from the host's `collector.version`).
    - `__main__.py`: the CLI, so `python -m collector` runs it, including from the source tree for fixture tests.
    - `turbostat.py`: runs `turbostat --quiet --Summary --interval <n> --show Busy%,Bzy_MHz,PkgTmp,PkgWatt` and parses one tab-separated summary row per interval. Check on the target how the summary row weights `Bzy_MHz` across CPUs.
    - `host.py`: `package_throttle_count` from `cpu0/thermal_throttle`, `/proc/meminfo` (RAM/swap), `/proc/pressure`, and a generic reader of every hwmon `temp*_input`/`fan*_input`, with chip name and label. Readers take a root path so they can be tested against a fake tree.
    - `gpu.py`, per GPU via NVML:
      - required readings: temperature, utilization, graphics/memory clocks, board power and VRAM used/total;
      - best-effort readings: enforced power limit and the raw clock-event-reasons bitmask (decoding is Phase 3).
      
      Each unsupported field is recorded with its NVML error name as the missing reason.
    - `drives.py`: `resolve_bench_drives(paths)`.
      - It runs `stat()` on each mounted path, follows `st_dev` through `/sys/dev/block/MAJ:MIN` to the partition and then its parent disk, and finds the NVMe hwmon under the disk's controller. Only the Composite temperature (`temp1`) is read.
      - This needs no `findmnt` or host mount table.
      - dm, LVM, md and btrfs (anonymous `st_dev`) fail with a clear message for now.
      - Each result carries the host bench path and the drive's model and serial.
    - `record.py`: JSONL writer.
      - A `meta` header holds the interval, sources, reading units, which sources are interval averages, the required set, ambient temperature, `sensor_collector_version`, and device identities keyed like `system.hardware` (CPU model, GPU UUID, NVMe serial).
      - Sample lines look like `{"seq", "at", "source", "device", "readings": {…}, "missing": {name: reason}}`.
      - Lines are flushed as written, so a crash leaves usable data.
    - `collector.py`: the sampling loop, readiness check and miss counters.
      - turbostat readings count as missing at a tick when the latest row is older than 1.5 × interval (measured with monotonic time); such a tick writes a stale turbostat record with the required fields in `missing`.
      - SIGTERM/SIGINT handlers stop turbostat, flush and close the writer, and exit 0.
  - `Dockerfile.dockerignore` (`*`, `!sensor-container/`), so the context holds only this directory.
  - Built with `docker build -f sensor-container/Dockerfile -t "$SENSOR_IMAGE" .`.
- **`src/benchsuite/results.schema.json`**: add an optional `sensors` object. Versions get reset before production, so no compatibility work.
- **`pyproject.toml`**: add the `run-suite` script. There are still no runtime dependencies.
- **README / CHANGELOG**: sensor image build, `run-suite` usage, what is collected, required readings and the stop rule, the JSONL record format, timestamp semantics, and the hybrid-average and `Busy%` caveats.

## Open choice (defaulted, flip if wanted)

- Until Phase 2.5 replaces the hard-coded SMART devices, the supervisor compares each resolved bench-path device with `NVME0_DEVICE`/`NVME1_DEVICE`. On a mismatch it **records a warning and continues** rather than blocking startup. That way current runs aren't blocked while the nvme0 FIO anomaly is still unexplained.

## Verification

The dev machine is Windows with no NVIDIA GPU, so only fixture-based checks can run there. Tests stay in the untracked `tests/` per the earlier decision. They cover:
- the turbostat summary parser;
- fake sysfs/procfs trees for hwmon and drive resolution;
- the miss counter;
- the clock-step check;
- the runner's empty-directory exception.

On workstation1:
1. Build the sensor image. Run the container standalone for about 30 s and check:
   - one line per source and device per second, with all required readings present;
   - the NVMe serials are correct;
   - `/proc/meminfo` values inside the container match the host;
   - turbostat's `PkgTmp` agrees with coretemp's package temperature in hwmon, roughly.
2. Run `uv run run-suite --ambient-c 22`. Check that:
   - the runner starts within a few seconds;
   - `results.json` has a `sensors` block with a matching SHA-256 and no clock-step warning;
   - GPU temperature and power rise during step 11;
   - CPU and GPU are both loaded during 13a/13b.
3. Timestamps:
   - turbostat `at` values are about 1 s apart, so lines arrive promptly with no buffering lag;
   - CPU `Busy%` visibly rises inside step 7's `started_at`/`finished_at` window, about one sample after `started_at`.
4. Fault injection: during a step, `docker kill` the sensor container. The run should stop within about N seconds with `status: interrupted`, `sensors.stop_reason` set, and no leftover containers.
5. Startup failure: unload `msr` and don't mount `/lib/modules` (or hide the GPU). Confirm exit 2 with no runner start.
6. `uv run run-playbook` on its own still works.
7. Check where `NVME0_BENCH_PATH` actually resolves; this probably explains the nvme0 FIO anomaly.

