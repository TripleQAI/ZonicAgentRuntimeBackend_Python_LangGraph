# Apply db/runtime/run_all.sql with psql in Docker (no local psql needed).
#
#   $env:AGENTRUNTIME_DSN = 'postgresql://...'; scripts/init_db.ps1
#
# The DSN is read from the environment only, never from an argument, so it does
# not land in shell history.
$ErrorActionPreference = 'Stop'

if (-not $env:AGENTRUNTIME_DSN) {
    Write-Error 'AGENTRUNTIME_DSN is not set'
    exit 2
}

$root = Split-Path -Parent $PSScriptRoot
if (-not (Test-Path (Join-Path $root 'db/runtime/005_procedures.sql'))) {
    Write-Error 'db/runtime/005_procedures.sql is missing; run_all.sql needs it'
    exit 2
}

docker run --rm -i `
    -e AGENTRUNTIME_DSN `
    -v "$(Join-Path $root 'db'):/db:ro" `
    postgres:17 `
    sh -c 'psql "$AGENTRUNTIME_DSN" -v ON_ERROR_STOP=1 -f /db/runtime/run_all.sql'
exit $LASTEXITCODE
