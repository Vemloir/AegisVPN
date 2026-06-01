FROM python:3.14-slim AS builder

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Copy uv config
COPY pyproject.toml uv.lock ./

# Install dependencies. Avoid --frozen here so the image still builds if
# pyproject.toml was updated but uv.lock has not been regenerated yet.
RUN uv sync --no-dev --no-install-project

# Final stage
FROM python:3.14-slim

# Install xray and dependencies (curl, unzip, procps for pkill)
RUN apt-get update && \
    apt-get install -y curl unzip procps && \
    curl -L -H "Cache-Control: no-cache" -o xray.zip https://github.com/XTLS/Xray-core/releases/latest/download/Xray-linux-64.zip && \
    unzip xray.zip -d /usr/local/bin/ xray geoip.dat geosite.dat && \
    chmod +x /usr/local/bin/xray && \
    rm xray.zip && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

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
