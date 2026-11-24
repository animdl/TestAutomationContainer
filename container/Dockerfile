# Base image: the tag is for readability; the digest is what is actually used.
# Changing CUDA_VERSION requires supplying the matching CUDA_IMAGE_DIGEST.
ARG CUDA_VERSION=12.6.3
ARG CUDA_IMAGE_DIGEST=sha256:392c0df7b577ecae17a17f6ba7f2009c217bb4422f8431c053ae9af61a8c148a
FROM nvidia/cuda:${CUDA_VERSION}-devel-ubuntu24.04@${CUDA_IMAGE_DIGEST}

ARG DEBIAN_FRONTEND=noninteractive
ARG CUDA_ARCH=89
ARG PTS_TESTS="pts/fio-1.15.0 pts/stream-1.3.4 pts/stressapptest-1.0.1 pts/sysbench-1.1.0"
# Upstream default-branch commits resolved on 2026-11-03.
ARG PTS_COMMIT=1bc6fc9c831f5941157541e46c5658d994cab110
ARG GPU_BURN_COMMIT=0d19c94dcb2e9b6858b7586a4c327674cd67113b
ARG NVBANDWIDTH_COMMIT=82fc4e8c6afa0babb8687793678f615b3b8d793e
ARG CUDA_MEMTEST_COMMIT=e94e1ee54e0689c9f154a8a08202554452552ec6

# Ubuntu packages come from a dated archive snapshot, so every rebuild resolves
# the same versions. The explicit versions below are the benchmark tools and the
# compiler that builds every source-built test; the build fails if the snapshot
# does not provide exactly these.
ARG UBUNTU_SNAPSHOT=20261103T000000Z
ARG STRESS_NG_VERSION=0.17.06-1build1
ARG MEMTESTER_VERSION=4.6.0-1
ARG NVME_CLI_VERSION=2.8-1ubuntu0.1
ARG GCC_VERSION=13.3.0-6ubuntu2~24.04.1

RUN set -eux; \
    printf 'APT::Snapshot "%s";\n' "${UBUNTU_SNAPSHOT}" > /etc/apt/apt.conf.d/50benchmark-snapshot; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        php-cli php-xml php-gd php-bz2 php-sqlite3 php-curl php-zip \
        git curl wget unzip bzip2 xz-utils ca-certificates \
        pciutils util-linux \
        build-essential cmake \
        "gcc-13=${GCC_VERSION}" "g++-13=${GCC_VERSION}" \
        autoconf automake libtool libtool-bin \
        libaio-dev zlib1g-dev \
        libboost-program-options-dev \
        pkg-config \
        "nvme-cli=${NVME_CLI_VERSION}" \
        "memtester=${MEMTESTER_VERSION}" \
        "stress-ng=${STRESS_NG_VERSION}" \
        python3; \
    rm -rf /var/lib/apt/lists/*

WORKDIR /opt
RUN set -eux; \
    git init /opt/phoronix-test-suite; \
    git -C /opt/phoronix-test-suite fetch --depth=1 https://github.com/phoronix-test-suite/phoronix-test-suite.git "${PTS_COMMIT}"; \
    git -C /opt/phoronix-test-suite checkout --detach FETCH_HEAD; \
    test "$(git -C /opt/phoronix-test-suite rev-parse HEAD)" = "${PTS_COMMIT}"; \
    printf '#!/bin/sh\nexec /opt/phoronix-test-suite/phoronix-test-suite "$@"\n' > /usr/local/bin/phoronix-test-suite; \
    chmod +x /usr/local/bin/phoronix-test-suite

# PTS concatenates this value directly with installed-tests/ and test-profiles/.
ENV PTS_USER_PATH_OVERRIDE=/var/lib/phoronix-test-suite/

# PTS evaluates dependencies against APT; keep package lists until installation ends.
# Force the missing integer header only for the older stressapptest source.
RUN set -eux; \
    apt-get update; \
    mkdir -p /var/lib/phoronix-test-suite; \
    mkdir -p /usr/local/share/benchmark; \
    printf '%s\n' ${PTS_TESTS} > /usr/local/share/benchmark/pts-profiles; \
    phoronix-test-suite enterprise-setup; \
    phoronix-test-suite user-config-set \
        SaveResults=FALSE \
        OpenBrowser=FALSE \
        UploadResults=FALSE \
        PromptForTestIdentifier=FALSE \
        PromptForTestDescription=FALSE \
        PromptSaveName=FALSE \
        RunAllTestCombinations=FALSE \
        Configured=TRUE; \
    for profile in ${PTS_TESTS}; do \
        case "$profile" in \
            pts/stressapptest-*) \
                CXXFLAGS='-include cstdint' phoronix-test-suite batch-install "$profile" ;; \
            *) phoronix-test-suite batch-install "$profile" ;; \
        esac; \
    done; \
    rm -rf /var/lib/apt/lists/*

RUN set -eux; \
    git init /opt/gpu-burn; \
    git -C /opt/gpu-burn fetch --depth=1 https://github.com/wilicc/gpu-burn.git "${GPU_BURN_COMMIT}"; \
    git -C /opt/gpu-burn checkout --detach FETCH_HEAD; \
    test "$(git -C /opt/gpu-burn rev-parse HEAD)" = "${GPU_BURN_COMMIT}"; \
    make -C /opt/gpu-burn COMPUTE="${CUDA_ARCH}"; \
    printf '#!/bin/sh\ncd /opt/gpu-burn\nexec ./gpu_burn "$@"\n' > /usr/local/bin/gpu-burn; \
    chmod +x /usr/local/bin/gpu-burn

RUN set -eux; \
    git init /opt/nvbandwidth-src; \
    git -C /opt/nvbandwidth-src fetch --depth=1 https://github.com/NVIDIA/nvbandwidth.git "${NVBANDWIDTH_COMMIT}"; \
    git -C /opt/nvbandwidth-src checkout --detach FETCH_HEAD; \
    test "$(git -C /opt/nvbandwidth-src rev-parse HEAD)" = "${NVBANDWIDTH_COMMIT}"; \
    cmake -S /opt/nvbandwidth-src -B /opt/nvbandwidth-src/build \
        -DCMAKE_CUDA_ARCHITECTURES="${CUDA_ARCH}"; \
    cmake --build /opt/nvbandwidth-src/build -j "$(nproc)"; \
    install -m 0755 /opt/nvbandwidth-src/build/nvbandwidth /usr/local/bin/nvbandwidth

# Test 10 truncates allocations of 2 GiB or more to 32 bits upstream; the guarded
# patch fails the build if the pinned source differs from what it expects.
COPY container/cuda_memtest_patch.py /opt/patches/
RUN set -eux; \
    git init /opt/cuda_memtest-src; \
    git -C /opt/cuda_memtest-src fetch --depth=1 https://github.com/ComputationalRadiationPhysics/cuda_memtest.git "${CUDA_MEMTEST_COMMIT}"; \
    git -C /opt/cuda_memtest-src checkout --detach FETCH_HEAD; \
    test "$(git -C /opt/cuda_memtest-src rev-parse HEAD)" = "${CUDA_MEMTEST_COMMIT}"; \
    python3 /opt/patches/cuda_memtest_patch.py /opt/cuda_memtest-src; \
    cmake -S /opt/cuda_memtest-src -B /opt/cuda_memtest-src/build \
        -DCMAKE_CUDA_ARCHITECTURES="${CUDA_ARCH}"; \
    cmake --build /opt/cuda_memtest-src/build -j "$(nproc)"; \
    install -m 0755 /opt/cuda_memtest-src/build/cuda_memtest /usr/local/bin/cuda_memtest

# The repository's helpers come last so editing them does not rebuild the tools.
# batch-install can return zero after a failed build, so verify every pinned
# profile and apply the guarded FIO latency customization here.
COPY container/pts-profile container/fio_profile.py container/combined-start /usr/local/bin/
RUN set -eux; \
    chmod +x /usr/local/bin/pts-profile /usr/local/bin/combined-start; \
    python3 /usr/local/bin/fio_profile.py; \
    pts-profile verify

WORKDIR /root
CMD ["/bin/bash"]
