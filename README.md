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
docker rm -f <container_name>

# clean stopped containers
docker container prune

# clean dangling images
docker image prune
```

## Run Container

Run interactive shell with GPU access, privileged hardware access, and NVMe devices exposed:

```bash
docker run -it --name <container_name> --gpus all --privileged \
  --device=/dev/nvme0n1 \
  --device=/dev/nvme1n1 \
  <image_name>:latest
```

If only one NVMe device exists, remove second `--device` line.

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
