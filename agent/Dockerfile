ARG PYTHON_IMAGE=python:3.14.6-slim@sha256:cea0e6040540fb2b965b6e7fb5ffa00871e632eef63719f0ea54bca189ce14a6
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.11.32@sha256:df4cae8f3a96d175e2e5f992e597550000edbe78fdc2594d5cd8de1a217f504c

FROM ${UV_IMAGE} AS uv
FROM ${PYTHON_IMAGE} AS builder

# Install uv
COPY --from=uv /uv /uvx /bin/

WORKDIR /app

# Copy uv config
COPY pyproject.toml uv.lock ./

# The lock file is the build contract; dependency drift must fail the build.
RUN uv sync --frozen --no-dev --no-install-project

# Final stage
FROM ${PYTHON_IMAGE}

# Install xray and dependencies (curl, unzip, procps for pkill)
ARG TARGETARCH
ARG XRAY_VERSION=v26.3.27
ARG XRAY_SHA256=23cd9af937744d97776ee35ecad4972cf4b2109d1e0fe6be9930467608f7c8ae
ARG XRAY_ARM64_SHA256=4d30283ae614e3057f730f67cd088a42be6fdf91f8639d82cb69e48cde80413c
RUN apt-get update && \
    apt-get install -y curl unzip procps && \
    case "$TARGETARCH" in \
      "amd64") XRAY_ARCH="64"; XRAY_ARCH_SHA256="$XRAY_SHA256" ;; \
      "arm64") XRAY_ARCH="arm64-v8a"; XRAY_ARCH_SHA256="$XRAY_ARM64_SHA256" ;; \
      *) echo "unsupported target architecture: $TARGETARCH" >&2; exit 1 ;; \
    esac && \
    curl --fail --location --retry 3 -o xray.zip "https://github.com/XTLS/Xray-core/releases/download/${XRAY_VERSION}/Xray-linux-${XRAY_ARCH}.zip" && \
    echo "${XRAY_ARCH_SHA256}  xray.zip" | sha256sum -c - && \
    unzip xray.zip -d /usr/local/bin/ xray geoip.dat geosite.dat && \
    chmod +x /usr/local/bin/xray && \
    rm xray.zip && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

COPY --from=uv /uv /uvx /bin/

WORKDIR /app
COPY --from=builder /app/.venv /app/.venv

# Copy source
COPY app/ app/
COPY template.json .
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh

# Environment
ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8444
EXPOSE 443
EXPOSE 9445

ENTRYPOINT ["./entrypoint.sh"]
