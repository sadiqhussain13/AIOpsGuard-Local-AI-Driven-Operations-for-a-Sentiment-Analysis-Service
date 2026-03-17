param(
    [string]$HostName = "host.docker.internal",
    [int]$WaitMs = 5000
)

$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$outDirRelative = "docs/screenshots"
$outDirAbsolute = Join-Path $repoRoot $outDirRelative

if (-not (Test-Path $outDirAbsolute)) {
    New-Item -ItemType Directory -Path $outDirAbsolute | Out-Null
}

$pages = @(
    @{ File = "01-operations-dashboard.png"; Url = "http://${HostName}:5000/ui"; Width = "1920px" },
    @{ File = "02-grafana-home.png"; Url = "http://${HostName}:3000"; Width = "1920px" },
    @{ File = "03-prometheus-home.png"; Url = "http://${HostName}:9090"; Width = "1920px" },
    @{ File = "04-mlflow-home.png"; Url = "http://${HostName}:5001"; Width = "1920px" },
    @{ File = "05-anomaly-health.png"; Url = "http://${HostName}:8080/health"; Width = "1920px" }
)

Write-Host "Using repo root: $repoRoot"
Write-Host "Saving screenshots to: $outDirAbsolute"

$failures = @()

foreach ($page in $pages) {
    $targetInContainer = "/workspace/$outDirRelative/$($page.File)"
    Write-Host "Capturing $($page.Url) -> $($page.File)"

    docker run --rm `
        -v "${repoRoot}:/workspace" `
        lifenz/docker-screenshot `
        "$($page.Url)" `
        "$targetInContainer" `
        "$($page.Width)" `
        "$WaitMs"

    if ($LASTEXITCODE -ne 0) {
        $failures += $page.Url
        Write-Warning "Failed to capture: $($page.Url)"
    }
}

if ($failures.Count -gt 0) {
    Write-Error ("Screenshot capture completed with failures: " + ($failures -join ", "))
}

Write-Host "Screenshot capture complete."
