# Changelog

Versions are the results schema, collector, and parser versions recorded in each
`results.json`. They will be reset before the production release. Existing
captures are never rewritten; each keeps the versions it was produced with.

## Unreleased: first target-run fixes (schema 10, collector 9, parser 10)

Treat the next run as a new baseline for steps 10c, 11, and 13b.

- cuda_memtest test 10 (step 10c) now covers the whole allocation. The upstream
  kernels truncated the size to 32 bits, so the first target run (RTX 4080,
  15,124 MiB allocated) stressed only 2,836 MiB and reported an impossible
  2,778 GB/s. The guarded build-time patch `benchmark-cuda-memtest-test10-size-v1`
  widens the size and per-block offset. Tests 1 and 2 were not affected.
- gpu-burn (steps 11 and 13b) runs with `-stts 5`. It always slept its full
  SIGTERM threshold (default 30 s) after the burn, so a 60 s burn took about 92 s.
- Parser 10: a gpu-burn SIGKILL fallback is recorded as a warning.

## Phase 1 cleanup (schema 10, collector 9, parser 9)

Treat the first run after this change as a new baseline: the compiler, package
set, and step ids changed.

**Layout and runtime**
- Moved files into `container/` (in-image helpers), `src/benchsuite/` (host
  Python package), and `scripts/`. The suite definition moved from the runner to
  `src/benchsuite/playbook.py`.
- The runner is a uv project on pinned Python 3.12.16: `uv run run-playbook`.
  `jsonschema` is a uv dev dependency, replacing `requirements-dev.txt`.
- All text files use LF line endings in every checkout.
- The Docker build context is only `container/`, and the helpers are copied
  after the tool builds, so editing them no longer reinstalls the PTS profiles.

**Image**
- The base image is pinned by digest, and Ubuntu packages come from archive
  snapshot `20261103T000000Z`. stress-ng (0.17.06-1build1), memtester (4.6.0-1),
  nvme-cli (2.8-1ubuntu0.1), and GCC 13 (13.3.0-6ubuntu2~24.04.1) are pinned to
  exact versions.
- Removed unused packages: `gfortran`, `libmysqlclient-dev` (sysbench is built
  `--without-mysql`), `libjpeg-dev`, `libssl-dev`, `sudo`, `mesa-utils`,
  `vulkan-tools`, `apt-file`. The last three are PTS optional extras: `apt-file`
  only helps resolve missing dependencies, and `glxinfo`/`vulkaninfo` only fed its
  system summary and fail in the headless container. Every benchmark binary
  links the same libraries as before.
  `gfortran` was what pulled GCC up to 13.3.0; that version is now pinned
  explicitly instead. `pkg-config` stays because sysbench needs it.

**Playbook**
- Steps renumbered from 1 (the old idle-baseline step 1 was dropped). Commands
  are unchanged; old step N maps to new step N-1 (for example, 5a→4a, 14b→13b).
  The suite hash changed because of the step ids.
- Per-step timeouts: duration + 600 s for fixed-duration steps, otherwise 1800 s.
  New `execution_status` `timed_out` and per-step `timeout_seconds`.

**Results**
- `system.workstation_name` (from `WORKSTATION_NAME`, falling back to the hostname
  with a warning) and `system.hardware` (CPU, memory, GPUs, NVMe model/serial/firmware).
- stress-ng metrics no longer include the PTS `profile` and
  `phoronix_test_suite_version` fields, which were always null for native runs.

## Schema 9, parser 8

- Parser 8: NVMe `valid_report_count` excludes unused self-test history slots
  (result code 15). Latest-result outcomes are unchanged.
- Schema 9: replaced the detailed hardware inventory with a compact `system`
  record: host OS label and kernel, plus the first PTS system summary.

## Schema and parser 7

- Per-run FIO `completion_latency`: mean, max, and p50/p95/p99, normalized to
  microseconds. Percentiles stay per repetition and are not averaged. New stages
  require all of them; missing latency makes collection partial.
- The parser checks each FIO stage's expected workload/block-size/target
  combinations.

## Schema and parser 6

- Added cuda_memtest `test_id`, and replaced the ambiguous
  `reported_elapsed_times` with `reported_elapsed_milliseconds`. Test 10's
  `elapsedtime` is the raw CUDA event measurement in milliseconds; tests 1 and 2
  report only wall time.

## Earlier

- FIO `throughput_source` (`native_summary`, `native_statistics`, `pts`) and
  per-run `summary_throughput`.
- STREAM `solution_validated`; STREAM pinned to 1.3.4 to avoid the broken
  exit-status line in the 1.3.5 launcher.
- gpu-burn `sample_count` counts work updates once; `progress_record_count`
  counts all display records.
