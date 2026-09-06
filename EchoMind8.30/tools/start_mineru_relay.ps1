param([int]$Port = 18789)

$relayRoot = Split-Path -Parent $PSScriptRoot
$healthUrl = "http://127.0.0.1:$Port/health"

try {
    $health = Invoke-WebRequest -Uri $healthUrl -UseBasicParsing -TimeoutSec 2
    if ($health.StatusCode -eq 200) {
        Write-Output "MinerU Relay is already running: $healthUrl"
        exit 0
    }
} catch {
    # Start the relay when no healthy instance is listening.
}

$process = Start-Process -FilePath python -ArgumentList @(
    "`"$PSScriptRoot\mineru_relay.py`"", "--port", "$Port"
) -WorkingDirectory $relayRoot -WindowStyle Hidden -PassThru

Start-Sleep -Seconds 1
try {
    Invoke-WebRequest -Uri $healthUrl -UseBasicParsing -TimeoutSec 2 | Out-Null
    Write-Output "MinerU Relay started (PID: $($process.Id)): $healthUrl"
} catch {
    throw "MinerU Relay failed to start. Confirm that Python is available."
}
