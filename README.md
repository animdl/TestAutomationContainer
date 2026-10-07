# Workstation Benchmark Container

## Misc Docker commands

```bash
# list images
docker images

# list running containers
docker ps

# list all containers
docker ps -a

# build/normal image rebuild
docker build -t <image_name>:latest .

# Force clean image rebuild
docker build --no-cache -t <image_name>:latest .

# open shell in already running container
docker exec -it <container_name> bash

# stop/remove container
docker rm -f <container_id>

# remove image
docker rmi <image_id>

# clean stopped containers
docker container prune

# clean dangling images
docker image prune
```

## Run Container

Run interactive shell with GPU access, privileged hardware access, and NVMe devices exposed:

```bash
docker run -it --name <container_name> --gpus all --privileged \
  --mount type=bind,src=/mnt/hdd/pts-bench-nvme0,dst=/mnt/bench/nvme0 \
  --mount type=bind,src=/mnt/pts-bench-nvme1,dst=/mnt/bench/nvme1 \
  <image_name>:latest
```

## Run Benchmark Playbook

By default, the playbook expects two writable benchmark directories:

- `/mnt/hdd/pts-bench-nvme0` on the first NVMe filesystem
- `/mnt/pts-bench-nvme1` on the second NVMe filesystem

Build the default image and run the benchmark sequence:

```bash
docker build -t bench:latest .
chmod +x ./verify-docker-mappings.sh
./verify-docker-mappings.sh
python3 ./run-playbook
```

The mapping verification script confirms that the two host directories resolve
to different filesystem devices, checks the Docker bind mounts, and verifies
that both container targets are writable.

To use a different image or host mount paths:

```bash
BENCH_IMAGE=<image_name>:latest \
NVME0_BENCH_PATH=/different/nvme0/directory \
NVME1_BENCH_PATH=/different/nvme1/directory \
python3 ./run-playbook
```

Step 5 runs `pts/fio-1.15.0` against both mounted filesystems. It
creates or reuses a 1 GiB file named `fiofile` under each benchmark
directory and includes write workloads. Use dedicated benchmark
directories.

Step 10 runs all four `pts/stream` operations: Copy, Scale, Add, and
Triad.

Results are written to:

```text
benchmark-results/<timestamp>/
├── results.json
└── raw/
    ├── 02a.stdout.log.gz
    ├── 02a.stderr.log.gz
    └── ...
```

`results.json` contains compact, structured metrics rather than complete command
output. Every test retains its raw stdout and stderr as deterministic gzip
artifacts under `raw/`. Artifact paths, compressed and uncompressed sizes, and
SHA-256 hashes are recorded in the JSON. The output contract is documented by
`results.schema.json`.

Execution, collection, and test outcome are reported separately:

- `execution_status` describes whether the command completed.
- `collection_status` describes whether its expected output was parsed fully.
- `outcome` is `pass`, `fail`, or `unknown` based on explicit health or
  completion markers. Benchmark values are not assigned regression thresholds.

The collector implementation is split across:

- `run-playbook` for the benchmark definitions and orchestration
- `benchmark_capture.py` for raw artifact capture and atomic result writes
- `benchmark_parsers.py` for tool-specific filtering

Keep all three Python files together when copying the runner. Their hashes are
recorded in every result file.

An alternative output directory can be supplied as the first argument:

```bash
python3 ./run-playbook /path/to/output-directory
```

The supplied output directory must be new or empty.

The FIO workload remains pinned to `pts/fio-1.15.0` (FIO 3.29). Newer major or
minor Phoronix profile versions change workload behavior and must start a new
performance baseline rather than being compared directly with these results.

## Verify Result Parsers

Run the parser and collection tests against the checked-in captured run:

```bash
python3 -m unittest discover -s tests -v
```

## Verify Tools Installed

Verify commands exist in image:

```bash
docker run --rm --gpus all <image_name>:latest bash -lc '
set -e
command -v phoronix-test-suite
command -v nvme
command -v memtester
command -v stressapptest
command -v stress-ng
command -v gpu-burn
command -v nvbandwidth
command -v cuda_memtest
'
```

Verify Phoronix tests were installed:

```bash
docker run --rm <image_name>:latest phoronix-test-suite list-installed-tests
```

Verify GPU visible inside container:

```bash
docker run --rm --gpus all <image_name>:latest nvidia-smi
```
