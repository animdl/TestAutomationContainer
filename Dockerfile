ARG CUDA_VERSION=12.6.3
FROM nvidia/cuda:${CUDA_VERSION}-devel-ubuntu24.04

ARG DEBIAN_FRONTEND=noninteractive
ARG CUDA_ARCH=89
ARG PTS_TESTS="pts/fio pts/sysbench pts/stream"

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        php-cli php-xml php-gd php-bz2 php-sqlite3 php-curl \
        git curl wget unzip ca-certificates \
        pciutils util-linux sudo \
        build-essential gfortran cmake \
        autoconf automake libtool \
        libaio-dev libssl-dev zlib1g-dev \
        libmysqlclient-dev \
        libboost-program-options-dev \
        pkg-config \
        fio \
        sysbench \
        nvme-cli \
        memtester \
        stressapptest \
        stress-ng \
	coreutils grep vim \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt
RUN set -eux; \
    git clone --depth=1 https://github.com/phoronix-test-suite/phoronix-test-suite.git; \
    printf '#!/bin/sh\nexec /opt/phoronix-test-suite/phoronix-test-suite "$@"\n' > /usr/local/bin/phoronix-test-suite; \
    chmod +x /usr/local/bin/phoronix-test-suite

ENV PTS_USER_PATH_OVERRIDE=/var/lib/phoronix-test-suite

RUN set -eux; \
    mkdir -p \
        /var/lib/phoronix-test-suite \
        /results; \
    phoronix-test-suite enterprise-setup; \
    phoronix-test-suite batch-install ${PTS_TESTS}

RUN set -eux; \
    git clone --depth=1 https://github.com/wilicc/gpu-burn.git /opt/gpu-burn; \
    make -C /opt/gpu-burn COMPUTE="${CUDA_ARCH}"; \
    printf '#!/bin/sh\ncd /opt/gpu-burn\nexec ./gpu_burn "$@"\n' > /usr/local/bin/gpu-burn; \
    chmod +x /usr/local/bin/gpu-burn; \
    ln -s /usr/local/bin/gpu-burn /usr/local/bin/gpu_burn

RUN set -eux; \
    git clone --depth=1 https://github.com/NVIDIA/nvbandwidth.git /opt/nvbandwidth-src; \
    cmake -S /opt/nvbandwidth-src -B /opt/nvbandwidth-src/build \
        -DCMAKE_CUDA_ARCHITECTURES="${CUDA_ARCH}"; \
    cmake --build /opt/nvbandwidth-src/build -j "$(nproc)"; \
    install -m 0755 /opt/nvbandwidth-src/build/nvbandwidth /usr/local/bin/nvbandwidth

RUN set -eux; \
    git clone --depth=1 https://github.com/ComputationalRadiationPhysics/cuda_memtest.git /opt/cuda_memtest-src; \
    mkdir -p /opt/cuda_memtest-src/build; \
    cd /opt/cuda_memtest-src/build; \
    cmake -DCMAKE_CUDA_ARCHITECTURES="${CUDA_ARCH}" ..; \
    make -j "$(nproc)"; \
    chmod +x /opt/cuda_memtest-src/build/cuda_memtest; \
    install -m 0755 /opt/cuda_memtest-src/build/cuda_memtest /usr/local/bin/cuda_memtest

WORKDIR /root
CMD ["/bin/bash"]
