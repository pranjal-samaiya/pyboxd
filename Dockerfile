# syntax=docker/dockerfile:1

# ---- Stage 1: build nsjail from source (pinned tag) --------------------------
FROM debian:trixie-slim AS nsjail-builder
ARG NSJAIL_VERSION=3.4
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        autoconf bison flex gcc g++ git ca-certificates libprotobuf-dev \
        libnl-route-3-dev libtool make pkg-config protobuf-compiler \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /nsjail
RUN git clone --depth 1 --branch "${NSJAIL_VERSION}" https://github.com/google/nsjail.git . \
    && make -j"$(nproc)"

# ---- Stage 2: runtime --------------------------------------------------------
FROM debian:trixie-slim
ARG NSJAIL_VERSION=3.4
ARG VERSION=0.1.0
ARG COMMIT=unknown
ENV DEBIAN_FRONTEND=noninteractive

# nsjail runtime libraries + Python for the service itself.
RUN apt-get update && apt-get install -y --no-install-recommends \
        bash ca-certificates python3 python3-venv \
        libnl-route-3-200 libprotobuf32t64 \
    && rm -rf /var/lib/apt/lists/*

# Language toolchains: one script per language, so adding a language needs no
# Dockerfile change.
COPY scripts/lang_install/ /tmp/lang_install/
RUN apt-get update \
    && for f in /tmp/lang_install/*.sh; do echo "== $f"; bash "$f" || exit 1; done \
    && rm -rf /var/lib/apt/lists/* /tmp/lang_install

COPY --from=nsjail-builder /nsjail/nsjail /usr/sbin/nsjail

# Install the service into its own venv (not visible inside the jail).
WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN python3 -m venv /opt/pyboxd \
    && /opt/pyboxd/bin/pip install --no-cache-dir . \
    && rm -rf /src

COPY languages.yaml /etc/pyboxd/languages.yaml
RUN mkdir -p /var/lib/pyboxd/jails && chmod 0711 /var/lib/pyboxd/jails

ENV NSJAIL_PATH=/usr/sbin/nsjail \
    NSJAIL_VERSION=${NSJAIL_VERSION} \
    PYBOXD_CONFIG=/etc/pyboxd/languages.yaml \
    PYBOXD_JAIL_DIR=/var/lib/pyboxd/jails \
    PYBOXD_VERSION=${VERSION} \
    PYBOXD_COMMIT=${COMMIT} \
    PYTHONUNBUFFERED=1

# Fail the build if any toolchain is missing.
RUN python3 --version && gcc --version | head -1 && g++ --version | head -1 \
    && javac -version && node --version && iverilog -V 2>&1 | head -1 \
    && rustc --version && test -x /usr/sbin/nsjail

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["/opt/pyboxd/bin/python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3)"]
ENTRYPOINT ["/opt/pyboxd/bin/pyboxd"]
