# With no arguments, opens the terminal UI. All other arguments pass to the CLI.
$ErrorActionPreference = 'Stop'
$cliArguments = @($args)
if ($cliArguments.Count -eq 0) { $cliArguments = @('tui') }
$execArguments = @('exec')
if (-not [Console]::IsInputRedirected -and -not [Console]::IsOutputRedirected) {
    $execArguments += '-it'
}
$execArguments += @('openclaw-host-client', 'openclaw')
& docker @execArguments @cliArguments
if ($LASTEXITCODE -ne 0) { throw "OpenClaw exited with code $LASTEXITCODE." }
