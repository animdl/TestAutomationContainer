ARG CUDA_VERSION=12.6.3
FROM nvidia/cuda:${CUDA_VERSION}-devel-ubuntu24.04

ARG DEBIAN_FRONTEND=noninteractive
ARG CUDA_ARCH=89
ARG PTS_TESTS="pts/fio-1.15.0 pts/stream-1.3.4 pts/stressapptest-1.0.1 pts/sysbench-1.1.0"
# Upstream default-branch commits resolved on 2026-11-03.
ARG PTS_COMMIT=1bc6fc9c831f5941157541e46c5658d994cab110
ARG GPU_BURN_COMMIT=0d19c94dcb2e9b6858b7586a4c327674cd67113b
ARG NVBANDWIDTH_COMMIT=82fc4e8c6afa0babb8687793678f615b3b8d793e
ARG CUDA_MEMTEST_COMMIT=e94e1ee54e0689c9f154a8a08202554452552ec6

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        php-cli php-xml php-gd php-bz2 php-sqlite3 php-curl php-zip \
        git curl wget unzip bzip2 xz-utils ca-certificates \
        pciutils util-linux sudo \
        build-essential gfortran cmake \
        autoconf automake libtool libtool-bin \
        libaio-dev libssl-dev zlib1g-dev \
        libmysqlclient-dev \
        libboost-program-options-dev \
        libjpeg-dev mesa-utils vulkan-tools apt-file \
        pkg-config \
        nvme-cli \
        memtester stress-ng python3 \
    && rm -rf /var/lib/apt/lists/*

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

COPY pts-profile /usr/local/bin/pts-profile
COPY fio_profile.py /usr/local/bin/fio_profile.py
RUN chmod +x /usr/local/bin/pts-profile

# PTS may install additional packages; keep APT lists until installation ends.
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
    python3 /usr/local/bin/fio_profile.py; \
    pts-profile verify; \
    rm -rf /var/lib/apt/lists/*

COPY combined-start /usr/local/bin/combined-start
RUN chmod +x /usr/local/bin/combined-start

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

RUN set -eux; \
    git init /opt/cuda_memtest-src; \
    git -C /opt/cuda_memtest-src fetch --depth=1 https://github.com/ComputationalRadiationPhysics/cuda_memtest.git "${CUDA_MEMTEST_COMMIT}"; \
    git -C /opt/cuda_memtest-src checkout --detach FETCH_HEAD; \
    test "$(git -C /opt/cuda_memtest-src rev-parse HEAD)" = "${CUDA_MEMTEST_COMMIT}"; \
    cmake -S /opt/cuda_memtest-src -B /opt/cuda_memtest-src/build \
        -DCMAKE_CUDA_ARCHITECTURES="${CUDA_ARCH}"; \
    cmake --build /opt/cuda_memtest-src/build -j "$(nproc)"; \
    install -m 0755 /opt/cuda_memtest-src/build/cuda_memtest /usr/local/bin/cuda_memtest

WORKDIR /root
CMD ["/bin/bash"]
