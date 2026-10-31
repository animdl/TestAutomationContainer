ARG CUDA_VERSION=12.6.3
FROM nvidia/cuda:${CUDA_VERSION}-devel-ubuntu24.04

ARG DEBIAN_FRONTEND=noninteractive
ARG CUDA_ARCH=89
ARG PTS_TESTS="pts/fio-1.15.0 pts/stream-1.3.4 pts/stressapptest-1.0.1 pts/sysbench-1.1.0"

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
    git clone --depth=1 https://github.com/phoronix-test-suite/phoronix-test-suite.git; \
    printf '#!/bin/sh\nexec /opt/phoronix-test-suite/phoronix-test-suite "$@"\n' > /usr/local/bin/phoronix-test-suite; \
    chmod +x /usr/local/bin/phoronix-test-suite

# PTS concatenates this value directly with installed-tests/ and test-profiles/.
ENV PTS_USER_PATH_OVERRIDE=/var/lib/phoronix-test-suite/

COPY pts-profile /usr/local/bin/pts-profile
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
    pts-profile verify; \
    rm -rf /var/lib/apt/lists/*

COPY combined-start /usr/local/bin/combined-start
RUN chmod +x /usr/local/bin/combined-start

RUN set -eux; \
    git clone --depth=1 https://github.com/wilicc/gpu-burn.git /opt/gpu-burn; \
    make -C /opt/gpu-burn COMPUTE="${CUDA_ARCH}"; \
    printf '#!/bin/sh\ncd /opt/gpu-burn\nexec ./gpu_burn "$@"\n' > /usr/local/bin/gpu-burn; \
    chmod +x /usr/local/bin/gpu-burn

RUN set -eux; \
    git clone --depth=1 https://github.com/NVIDIA/nvbandwidth.git /opt/nvbandwidth-src; \
    cmake -S /opt/nvbandwidth-src -B /opt/nvbandwidth-src/build \
        -DCMAKE_CUDA_ARCHITECTURES="${CUDA_ARCH}"; \
    cmake --build /opt/nvbandwidth-src/build -j "$(nproc)"; \
    install -m 0755 /opt/nvbandwidth-src/build/nvbandwidth /usr/local/bin/nvbandwidth

RUN set -eux; \
    git clone --depth=1 https://github.com/ComputationalRadiationPhysics/cuda_memtest.git /opt/cuda_memtest-src; \
    cmake -S /opt/cuda_memtest-src -B /opt/cuda_memtest-src/build \
        -DCMAKE_CUDA_ARCHITECTURES="${CUDA_ARCH}"; \
    cmake --build /opt/cuda_memtest-src/build -j "$(nproc)"; \
    install -m 0755 /opt/cuda_memtest-src/build/cuda_memtest /usr/local/bin/cuda_memtest

WORKDIR /root
CMD ["/bin/bash"]
