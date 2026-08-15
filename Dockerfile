# syntax=docker/dockerfile:1
#
# QQQ/PSQ Switch — container image.
#
# Design notes:
#   * Multi-stage, so build tooling never ships in the runtime image.
#   * Runs as a non-root user; the image filesystem can be mounted read-only
#     because every write goes to the /app/var volume.
#   * tzdata is installed explicitly. Without it, zoneinfo cannot resolve
#     America/New_York and the market-hours guard would have no way to tell
#     the US session from UTC. The code fails closed in that case, so a
#     missing tzdata would block all trading rather than mis-time it — but
#     the right answer is to have it present.
#   * No secret is baked in. Credentials arrive at runtime via env_file or a
#     secrets manager, and .dockerignore keeps .env out of the build context.

# ---------- build ----------
FROM python:3.12-slim AS build

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Dependencies first: this layer is cached until requirements.txt changes.
COPY requirements.txt ./
RUN pip install -r requirements.txt

# ---------- runtime ----------
FROM python:3.12-slim AS runtime

# tzdata: required for the market-hours guard (see note above).
# ca-certificates: TLS to api.schwabapi.com.
# curl: the HEALTHCHECK below.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src \
    TZ=America/New_York \
    # Bind to all interfaces *inside* the container. Publish it to
    # 127.0.0.1 on the host so it is still not reachable from the network.
    # Because this is not loopback, config validation now requires API_TOKEN.
    API_HOST=0.0.0.0 \
    API_PORT=8787 \
    LOG_DIR=/app/var/log \
    STATE_DIR=/app/var/state \
    # Safe default: the image itself never trades.
    TRADING_MODE=dry_run

# Token paths are derived from STATE_DIR by the config layer, so they need no
# ENV of their own. (Setting one here also trips the SecretsUsedInArgOrEnv
# build warning, even though a path is not a secret.)

COPY --from=build /opt/venv /opt/venv

WORKDIR /app
COPY src/ ./src/
COPY pyproject.toml README.md ./
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh

RUN chmod +x /usr/local/bin/entrypoint.sh \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin trader \
    && mkdir -p /app/var/log /app/var/state \
    && chown -R trader:trader /app

USER trader

# Tokens, NAV history, the execution lock and command state all live here.
# Without a volume, every restart loses the Schwab refresh token — which
# expires after 7 days and cannot be re-obtained without a browser login.
VOLUME ["/app/var"]

EXPOSE 8787

# /api/health needs no token and exposes no account data.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${API_PORT}/api/health" || exit 1

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["serve"]
