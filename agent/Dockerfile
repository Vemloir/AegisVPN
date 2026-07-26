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
RUN apt-get update && \
    apt-get install -y curl unzip procps && \
    test "$TARGETARCH" = "amd64" && \
    curl --fail --location --retry 3 -o xray.zip "https://github.com/XTLS/Xray-core/releases/download/${XRAY_VERSION}/Xray-linux-64.zip" && \
    echo "${XRAY_SHA256}  xray.zip" | sha256sum -c - && \
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
