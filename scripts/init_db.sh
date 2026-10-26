#!/usr/bin/env sh
# Apply db/runtime/run_all.sql and db/observability/run_all.sql with psql in Docker
# (no local psql needed).
#
#   AGENTRUNTIME_DSN='postgresql://...' [AGENTOBSERVABILITY_DSN='postgresql://...'] scripts/init_db.sh
#
# AGENTOBSERVABILITY_DSN defaults to AGENTRUNTIME_DSN (both schemas in one database).
# DSNs are read from the environment only, never from an argument, so they do not land
# in shell history. The agent_builder schema must already be installed (see run_all.sql).
set -eu

if [ -z "${AGENTRUNTIME_DSN:-}" ]; then
    echo "AGENTRUNTIME_DSN is not set" >&2
    exit 2
fi
AGENTOBSERVABILITY_DSN=${AGENTOBSERVABILITY_DSN:-$AGENTRUNTIME_DSN}
export AGENTRUNTIME_DSN AGENTOBSERVABILITY_DSN

# `pwd -W` gives the Windows form under Git Bash, which Docker Desktop needs for the
# volume; MSYS_NO_PATHCONV stops Git Bash rewriting the in-container paths.
root=$(cd "$(dirname "$0")/.." && (pwd -W 2>/dev/null || pwd))
export MSYS_NO_PATHCONV=1

exec docker run --rm -i \
    -e AGENTRUNTIME_DSN -e AGENTOBSERVABILITY_DSN \
    -v "$root/db:/db:ro" \
    postgres:17 \
    sh -c 'psql "$AGENTRUNTIME_DSN" -v ON_ERROR_STOP=1 -f /db/runtime/run_all.sql &&
           psql "$AGENTOBSERVABILITY_DSN" -v ON_ERROR_STOP=1 -f /db/observability/run_all.sql'
