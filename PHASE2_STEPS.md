# Phase 2 implementation steps

Use [PHASE2_PLAN.md](PHASE2_PLAN.md) as the source of truth. Each numbered section is one agent prompt and one suggested commit. Complete and review each step before starting the next. The dependency lines identify which earlier changes a step uses.

Copy this preamble into each prompt, followed by the chosen step.

> Read `PHASE2_PLAN.md` and the selected step in `PHASE2_STEPS.md`. Earlier steps are already committed. Read the code they added and follow its conventions, including reader return shapes, naming and how missing readings are reported. Implement only this step. Change only the files it lists. If another file needs changing, stop and explain why instead of editing it. Leave both planning files unchanged. Keep any local fixtures under the ignored `tests/` directory. Run the applicable checks. **Do not stage or commit.** End with a hand-back containing (a) a commit message: an imperative subject of 72 characters or fewer, with an optional body; and (b) bullets on what changed, which checks ran and their results, and anything that needs workstation1.

Phase 2 targets Intel CPUs, discrete NVIDIA GPUs, and Ubuntu 24.04. Keep the host package standard-library only and build the collector in its own image. Installation automation, drive auto-detection, and REST orchestration belong to Phase 2.5. Acceptance thresholds, the timeline frontend, and decoding values for display belong to Phase 3.

## 1. Allow the collector to start before the runner

**Files** — `src/benchsuite/runner.py`.

Adjust `prepare_output_directory()` so the runner accepts a new directory, an empty directory, or a directory containing only `sensors.jsonl`. Keep the existing creation of `raw/` and the standalone `run-playbook` entry point. Keep this as the only runner change in Phase 2.

**Review and verify** — Use temporary directories to exercise all three accepted cases. Check rejection of unrelated files, existing `raw/` or `results.json`, and `sensors.jsonl` alongside any other entry.

## 2. Define and write the sensor JSONL format

**Files** — `sensor-container/collector/__init__.py` and `sensor-container/collector/record.py`.

Add the writer for uncompressed `sensors.jsonl`. Open the file in exclusive-create mode (`"x"`) so an existing `sensors.jsonl` is never overwritten. Flush each line as it is written. Write one `meta` header with the interval, sources, units, required readings, interval-average semantics, ambient temperature, collector version, and device identities matching `system.hardware` identifiers (CPU model, GPU UUID, NVMe serial). Represent omitted ambient temperature as unavailable with reason `not provided`.

Define `SENSOR_COLLECTOR_VERSION = 1` in `collector/__init__.py`, and write it to `meta` as `sensor_collector_version`. Don't reuse the name `collector_version`, which belongs to the host capture code.

Write one record per source and device per sample with `seq`, `at`, `source`, `device`, `readings`, and `missing`. Keep each source's own ISO-8601 UTC time with `Z` and microseconds. Describe turbostat values as interval averages ending at `at`, with the `at` of the previous turbostat record that has readings marking the interval start. Stale turbostat records (empty `readings`, written by Step 7) are stamped at the tick and don't start an interval. Treat other sources as read-time values. NVML utilization is a driver-window average (about ⅙ to 1 second, depending on the product) reported at poll time.

**Review and verify** — Check that opening an existing `sensors.jsonl` fails and leaves it unchanged. Read the output while the writer is still open. Check the header, per-source timestamps, numeric zero values, and explicit missing reasons on independently parseable lines.

## 3. Read best-effort host sensors

**Files** — `sensor-container/collector/host.py`.

Add readers for `cpu0/thermal_throttle` package throttle count, RAM and swap from `/proc/meminfo`, and PSI from `/proc/pressure`. Discover hwmon `temp*_input` and `fan*_input` generically and include chip names and labels. The generic hwmon reader includes NVMe chips. The required Composite temperature comes from the drives source (Step 4), and the copy here is intentional. Accept a root path for fixture use. Give unavailable readings explicit missing reasons.

**Review and verify** — Use a fake procfs/sysfs tree with multiple chips and inputs, missing labels, unavailable files, and malformed values. Check that one unavailable reading leaves other readings usable.

## 4. Resolve benchmark mounts and read NVMe Composite temperatures

**Files** — `sensor-container/collector/drives.py`.

Implement `resolve_bench_drives(paths)` for any supplied list. Use each mounted path's `stat().st_dev` and `/sys/dev/block/MAJ:MIN` to find the partition and parent disk, then locate controller hwmon and read `temp1` as the required Composite temperature. Return the physical device, original host bench path, model, and serial. Report dm, LVM, md, and btrfs anonymous device IDs clearly. Use sysfs and `stat()` for resolution.

**Review and verify** — Use a fixture tree and controlled device IDs to exercise partition-to-disk resolution, arbitrary mount counts and NVMe names, device identity, missing or unreadable `temp1`, and unsupported mappings. Assign the workstation1 path check to final verification.

## 5. Read per-GPU sensors through NVML

**Files** — `sensor-container/collector/gpu.py`.

Read required temperature, utilization, graphics and memory clocks, board power, and VRAM used and total for each GPU. Read the enforced power limit and raw clock-event-reasons bitmask as best-effort values. Use GPU UUIDs and record NVML error names for unsupported fields. Keep `nvidia-ml-py` confined to the sensor image. Import `pynvml` inside the function that initializes NVML, or accept the module as a parameter, so `gpu.py` imports without `nvidia-ml-py` installed. Fixture tests pass a fake module.

**Review and verify** — Use fake NVML responses for multiple GPUs, zero utilization, and failures of individual required and best-effort readings. Check units and preservation of the raw bitmask.

## 6. Collect turbostat summary rows

**Files** — `sensor-container/collector/turbostat.py`.

Run `turbostat --quiet --Summary --interval <n> --show Busy%,Bzy_MHz,PkgTmp,PkgWatt` and parse the tab-separated summary rows. Stamp each row at arrival. Make absent readings and subprocess failure visible to the collector. On workstation1, check that piped output is flushed every interval and use `stdbuf -oL` if needed. Verify how the summary weights `Bzy_MHz` across CPUs on the hybrid target.

**Review and verify** — Use captured or synthetic headers and rows to check column mapping, malformed or missing readings, timestamps, and subprocess exit. Record pipe buffering and hybrid clock weighting as pending until verified on workstation1.

## 7. Wire the collector loop, readiness, and miss limit

**Depends on** — Steps 2–6. **Files** — `sensor-container/collector/collector.py` and `sensor-container/collector/__main__.py`.

Compose the readers and writer into the standalone `collect-sensors` command. Add `collector/__main__.py` so `python -m collector` runs the CLI. Fixture tests run it from the source tree. Resolve drives at startup and poll at 1 Hz by default. Preserve independent source timestamps. Define the CLI inputs the supervisor will pass for output, bench mounts, ambient temperature, interval, miss limit, and startup timeout. Take bench mounts as repeated `--bench PATH`, where each path is the host path, mounted at the same location in the container. Include each path in the `ready` line and the `meta` header. Emit one flushed `ready` line on stdout containing the resolved devices after the first tick that has every required reading, a fresh complete turbostat row, at least one enumerated GPU, and a resolved drive for every `--bench` path. An empty GPU or drive list never counts as ready. If no tick qualifies within the startup timeout (default 30 seconds), exit nonzero with a diagnostic on stderr.

Start the miss counters only after `ready`. During collection, count consecutive misses per required reading and device. Reset that count when the reading returns. Record isolated misses and best-effort gaps. Exit nonzero with a reason on stderr when any required reading reaches the configured threshold (default five samples). Close the writer and stop turbostat on exit.

The sample loop ticks once per interval. Turbostat rows are written as records when they arrive, independent of the loop. At each tick, the turbostat readings count as missing if the latest row is older than 1.5 × interval, or if that row lacked the field. Measure row age with monotonic time, so a wall-clock step can't cause or hide a miss. When a tick finds turbostat stale, write a turbostat record stamped at the tick, with empty `readings` and every required turbostat field in `missing` with reason `no row within 1.5 intervals`, so a stall is explicit in the file rather than a gap. If turbostat exits, nothing is special-cased: its readings go missing on every tick, and the normal N-miss rule stops the collector.

Install SIGTERM and SIGINT handlers. On either signal, stop sampling, terminate turbostat and wait for it, then flush and close the writer, and exit 0 with `stopped by signal` on stderr. The collector runs as PID 1 in its container, so Python's default SIGTERM behaviour can't be relied on.

**Review and verify** — Use fake readers and a controlled clock. Cover delayed readiness, timeout, no readiness with zero GPUs or an unresolved bench path, misses before readiness not counting toward the limit, a recovering miss, a different field missing on successive samples, a required field reaching the limit, and persistent best-effort gaps. Check that jittered turbostat arrival (zero rows in one tick, two in the next) doesn't count as a miss, and that a stalled or dead turbostat writes explicit missing records and reaches the limit after N ticks. Check that SIGTERM during collection gives a clean exit, stops turbostat, and leaves the last line complete. Check the flushed readiness payload and that records before a failure remain readable.

## 8. Build the separate sensor image

**Depends on** — Step 7. **Files** — `sensor-container/Dockerfile`, `sensor-container/Dockerfile.dockerignore`, `sensor-container/requirements.txt` and `sensor-container/collect-sensors`.

Build from the repository root, as `container/Dockerfile` is, with `COPY` paths under `sensor-container/` and a `Dockerfile.dockerignore` (`*`, `!sensor-container/`) that limits the context to that directory. Use a multi-stage Ubuntu 24.04 build with a digest, archive snapshot, and exact `python3` package version, following `container/Dockerfile`'s pinning approach. Build turbostat from a kernel source tarball pinned by version and SHA-256. Include `kmod` for `msr`. Install the current `nvidia-ml-py` release at implementation time with an exact version and hash in a venv using `--require-hashes`. Set `NVIDIA_DRIVER_CAPABILITIES=utility`. Copy `sensor-container/collector/` to `/opt/collector/` and set `ENV PYTHONPATH=/opt`. Install `sensor-container/collect-sensors` into `/usr/local/bin/`. It's a POSIX `sh` script whose only command is `exec /opt/venv/bin/python -m collector "$@"`. Start the collector without a shell between Docker and Python: the wrapper must `exec` Python so that it receives `docker stop`'s SIGTERM.

**Review and verify** — Inspect the pins and build with `docker build -f sensor-container/Dockerfile -t "$SENSOR_IMAGE" .` on a Docker-capable host. Confirm `collect-sensors` resolves in the resulting image. Verify sensor acquisition on workstation1. `container/Dockerfile` and its `Dockerfile.dockerignore` stay as they are.

## 9. Supervise the collector and benchmark processes

**Depends on** — Steps 1 and 7. **Files** — `src/benchsuite/supervisor.py`.

Implement the stdlib-only supervisor and CLI parsing for `[output_dir]`, `--interval` (1.0), `--miss-limit` (5), `--startup-timeout` (30), and optional `--ambient-c`. Use `SENSOR_IMAGE` with default `bench-sensors:latest`. Reserve the run directory before any Docker call: create it with `mkdir(parents=True, exist_ok=False)`, or accept an existing directory only if it is empty. Otherwise exit 2 without starting Docker. This protects a previous run's files and makes a second invocation on the same path, including the same per-second default name, fail. Then start a named sensor container with `--privileged --gpus all`, the run directory mounted read-write, and `/lib/modules` mounted read-only. Mount each bench path read-only at the same path inside the container with `--mount type=bind,src=P,dst=P,readonly`, matching the runner's use of `--mount`, so a missing path fails instead of being created. Do not use `-v`. Pass each one as `--bench P`. Match the `ready` payload's paths to the corresponding `playbook.NVME_DEVICES` entries. Keep the plan's default of warning and continuing on a mismatch.

Launch `run-playbook <dir>` only after readiness, with no added delay. Return 2 on startup failure. If the sensor container exits while the runner is active, capture the reason, send SIGTERM to the runner, wait for its cleanup, and return 2. Forward supervisor SIGINT/SIGTERM to the runner and wait for it. When the runner exits, stop and remove the sensor container using `capture.remove_container` for removal. Return the runner's exit code on normal sensor shutdown. Carry the stop reason and drive warning forward for finalization in Step 10.

**Review and verify** — Use fake processes to check Docker arguments, readiness gating, rejection of a non-empty run directory before any Docker call (leaving its contents untouched), a second invocation on the same path failing, timeout, early exit, drive mismatch, signal forwarding, and cleanup. Cover a collector exit during a run, including exit code zero, and the expected shutdown after runner completion. Exercise the supervisor directly before registering `run-suite` in Step 10.

## 10. Finalize sensor results and register `run-suite`

**Depends on** — Step 9. **Files** — `src/benchsuite/supervisor.py`, `src/benchsuite/results.schema.json`, and `pyproject.toml`.

Record a wall and monotonic clock pair before starting the sensor container and a second pair after stopping it. Warn if the wall-minus-monotonic offset changes by more than 100 ms. After the runner's final save and the sensor container's shutdown, use `capture.sha256_path`, `inspect_image`, `timestamp`, and `atomic_save` as applicable to finalize `results.json`. Add the optional `sensors` block with file name, SHA-256, summary, stop reason, sensor image ID and digest information, the `sensor_collector_version` from the JSONL `meta` header, and any clock-step or drive mismatch warning. The plan leaves the summary fields unspecified. Propose them in this diff and keep the schema and writer in agreement. Register `run-suite` in `pyproject.toml` with no new host runtime dependencies.

**Review and verify** — Check forward and backward clock jumps, including the 100 ms boundary. Validate normal and interrupted results against the schema, confirm the digest matches the final JSONL bytes, and validate a standalone runner result that lacks `sensors`. Exercise a startup failure where the runner never creates `results.json` and confirm exit 2 with the startup diagnostic.

## 11. Document how to build, run, and interpret Phase 2

**Depends on** — Steps 8 and 10. **Files** — `README.md` and `CHANGELOG.md`.

Document the separate sensor image build, `run-suite` options and defaults, and standalone `run-playbook` usage. Describe required and best-effort readings, readiness, the consecutive-miss rule, mount mismatch warnings, and where to find the JSONL file and summary. Explain timestamps and interval averages, the mixed P-core/E-core effective clock, `Busy%` as hardware active-state residency versus OS-scheduled time, and the roughly 0.3–1 second container startup skew. Point to the `combined-start` log line for the precise start of steps 13a/13b.

**Review and verify** — Cross-check example commands, field names, defaults, and caveats against the implemented CLI and output.

## Workstation1 acceptance checklist

Windows verification uses fixtures. The hardware checks below are a separate pass after integration. Any resulting fixes can be reviewed as follow-up commits.

- [ ] Build the image and run `collect-sensors` standalone for about 30 seconds. Check one line per source/device per interval, all required fields, correct NVMe serials, `/proc/meminfo` values matching the host, and `PkgTmp` roughly agreeing with coretemp package temperature.
- [ ] Run `uv run run-suite --ambient-c 22`. Check that the runner starts within a few seconds, the sensor SHA-256 matches, and there is no clock-step warning during an ordinary run. Confirm GPU temperature and power rise in step 11 and CPU and GPU are both loaded during 13a/13b.
- [ ] Confirm turbostat records arrive about a second apart and check hybrid `Bzy_MHz` weighting. Confirm CPU `Busy%` rises inside step 7's `started_at` and `finished_at` window, about one sample after `started_at`.
- [ ] Kill the sensor container during a step. Check that the run stops within about N seconds at the default 1 Hz, with supervisor exit 2, runner `status: interrupted`, populated `sensors.stop_reason`, and no leftover containers. The kill checks process-exit handling. The fixture test in Step 7 checks the N-sample threshold for a collector that remains alive while readings are missing.
- [ ] Verify startup failure using the plan's missing-`msr` setup or by hiding the GPU. Expect exit 2 and no runner launch.
- [ ] Run `uv run run-playbook` standalone and confirm it still produces the usual results.
- [ ] Check which physical device `NVME0_BENCH_PATH` resolves to and record what that establishes about the FIO anomaly. Confirm mismatches warn and allow the benchmark to start.
