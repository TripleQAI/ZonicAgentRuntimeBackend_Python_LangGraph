# Apply db/runtime/run_all.sql and db/observability/run_all.sql with psql in Docker
# (no local psql needed).
#
#   $env:AGENTRUNTIME_DSN = 'postgresql://...'; scripts/init_db.ps1
#
# AGENTOBSERVABILITY_DSN defaults to AGENTRUNTIME_DSN (both schemas in one database).
# DSNs are read from the environment only, never from an argument, so they do not land
# in shell history. The agent_builder schema must already be installed (see run_all.sql).
$ErrorActionPreference = 'Stop'

if (-not $env:AGENTRUNTIME_DSN) {
    Write-Error 'AGENTRUNTIME_DSN is not set'
    exit 2
}
if (-not $env:AGENTOBSERVABILITY_DSN) {
    $env:AGENTOBSERVABILITY_DSN = $env:AGENTRUNTIME_DSN
}

$root = Split-Path -Parent $PSScriptRoot

docker run --rm -i `
    -e AGENTRUNTIME_DSN -e AGENTOBSERVABILITY_DSN `
    -v "$(Join-Path $root 'db'):/db:ro" `
    postgres:17 `
    sh -c 'psql "$AGENTRUNTIME_DSN" -v ON_ERROR_STOP=1 -f /db/runtime/run_all.sql && psql "$AGENTOBSERVABILITY_DSN" -v ON_ERROR_STOP=1 -f /db/observability/run_all.sql'
exit $LASTEXITCODE
