# Changelog

Versions are the results schema, collector, and parser versions recorded in each
`results.json`. Existing captures are never rewritten; each keeps the versions it
was produced with. Bump the relevant version whenever the results format,
capture, or parsing changes, and treat the first run after a change to an
image pin or step command as a new baseline.

## 1: Phase 1 base (schema 1, collector 1, parser 1)

The base state of the suite, validated by two full runs on workstation1 in which
all 25 steps completed, parsed fully, and passed. Captures made during
development carry earlier, higher version numbers and are not comparable with
this version.

- Pinned image: base image digest, Ubuntu archive snapshot, exact stress-ng,
  memtester, nvme-cli, and GCC versions, Git commits for PTS, gpu-burn,
  nvbandwidth, and cuda_memtest, and the PTS test profiles.
- Two guarded build-time changes: `benchmark-fio-latency-v1` (FIO completion
  latency and percentiles) and `benchmark-cuda-memtest-test10-size-v1`
  (cuda_memtest test 10 tests the whole allocation).
- A uv-managed host runner (`src/benchsuite`) that runs the 25-step playbook in
  separate containers, with per-step timeouts and a start barrier for the
  combined CPU/GPU load.
- `results.json` with workstation name, hardware identity, provenance, and
  per-step execution, collection, and outcome statuses, validated by
  `results.schema.json`.
