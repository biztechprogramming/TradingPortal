#!/bin/sh
# Entrypoint: dispatch to the CLI so every subcommand works naturally.
#
#   docker compose up -d                     -> serve (the CMD default)
#   docker compose run --rm app status       -> cli status
#   docker compose run --rm app accounts     -> cli accounts
#   docker compose run --rm -it app schwab-login --login personal
#
# Anything that is not a known subcommand is executed verbatim, so
# `docker compose run --rm app sh` still gets you a shell.
set -eu

# The state directory must be writable or tokens cannot be refreshed and the
# execution lock cannot be taken. Fail loudly at start rather than midway
# through a command.
STATE_DIR="${STATE_DIR:-/app/var/state}"
LOG_DIR="${LOG_DIR:-/app/var/log}"
mkdir -p "$STATE_DIR" "$LOG_DIR" 2>/dev/null || true
if [ ! -w "$STATE_DIR" ]; then
    echo "FATAL: $STATE_DIR is not writable by uid $(id -u)." >&2
    echo "The /app/var volume must be owned by uid 10001, or run with" >&2
    echo "  docker compose run --user \$(id -u):\$(id -g) ..." >&2
    exit 1
fi

case "${1:-serve}" in
    serve|status|holdings|performance|accounts|long|short|flat|acknowledge|schwab-login)
        exec python -m trading.cli "$@"
        ;;
    -h|--help)
        exec python -m trading.cli --help
        ;;
    *)
        exec "$@"
        ;;
esac
