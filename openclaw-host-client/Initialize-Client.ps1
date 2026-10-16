[CmdletBinding()]
param(
    [string]$SourceConfig = (Join-Path $env:USERPROFILE '.openclaw/openclaw.json')
)

$ErrorActionPreference = 'Stop'
$composeFile = Join-Path $PSScriptRoot 'compose.yml'
if (-not (Test-Path -LiteralPath $SourceConfig -PathType Leaf)) {
    throw "No client configuration at $SourceConfig. Use -SourceConfig with a remote OpenClaw configuration."
}
$source = Get-Content -LiteralPath $SourceConfig -Raw | ConvertFrom-Json
if (-not $source.gateway.remote.url) {
    throw 'Source configuration has no gateway.remote.url.'
}
# Pass only connection data into Docker. Do not import the Windows device
# identity: the container is a separate Linux client with its own pairing.
$payload = @{ gateway = @{ remote = $source.gateway.remote } } | ConvertTo-Json -Depth 20 -Compress
$previousEncoding = $OutputEncoding
try {
    $OutputEncoding = New-Object System.Text.UTF8Encoding($false)
    $payload | & docker compose --file $composeFile run --rm --no-deps -T --entrypoint node client /opt/host-client/initialize-state.mjs
    if ($LASTEXITCODE -ne 0) { throw 'Client configuration initialization failed.' }
} finally {
    $OutputEncoding = $previousEncoding
    $payload = $null
    $source = $null
}
& docker compose --file $composeFile up -d client
if ($LASTEXITCODE -ne 0) { throw 'Client startup failed.' }
Write-Host 'Client started. Check connectivity with docker exec openclaw-host-client openclaw health.'
