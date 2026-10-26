#!/usr/bin/env sh
# Apply db/runtime/run_all.sql with psql in Docker (no local psql needed).
#
#   AGENTRUNTIME_DSN='postgresql://...' scripts/init_db.sh
#
# The DSN is read from the environment only, never from an argument, so it does
# not land in shell history.
set -eu

if [ -z "${AGENTRUNTIME_DSN:-}" ]; then
    echo "AGENTRUNTIME_DSN is not set" >&2
    exit 2
fi

root=$(cd "$(dirname "$0")/.." && pwd)
if [ ! -f "$root/db/runtime/005_procedures.sql" ]; then
    echo "db/runtime/005_procedures.sql is missing; run_all.sql needs it" >&2
    exit 2
fi

exec docker run --rm -i \
    -e AGENTRUNTIME_DSN \
    -v "$root/db:/db:ro" \
    postgres:17 \
    sh -c 'psql "$AGENTRUNTIME_DSN" -v ON_ERROR_STOP=1 -f /db/runtime/run_all.sql'
