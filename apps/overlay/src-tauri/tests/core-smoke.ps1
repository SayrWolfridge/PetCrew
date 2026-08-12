param(
    [Parameter(Mandatory = $true)]
    [string]$Executable
)

$ErrorActionPreference = "Stop"
$tempBase = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
$testRoot = Join-Path $tempBase ("petcrew-core-smoke-" + [guid]::NewGuid().ToString("N"))
$appData = Join-Path $testRoot "app.petcrew.overlay"
$previousLocalAppData = $env:LOCALAPPDATA
$core = $null
$secondCore = $null

try {
    New-Item -ItemType Directory -Path $testRoot | Out-Null
    $env:LOCALAPPDATA = $testRoot
    $core = Start-Process -FilePath $Executable -PassThru -WindowStyle Hidden

    $runtimePath = Join-Path $appData "hub-runtime.json"
    for ($attempt = 0; $attempt -lt 50 -and -not (Test-Path -LiteralPath $runtimePath); $attempt++) {
        Start-Sleep -Milliseconds 100
    }
    if (-not (Test-Path -LiteralPath $runtimePath)) {
        throw "Core did not publish hub-runtime.json"
    }

    $descriptor = Get-Content -LiteralPath $runtimePath -Encoding UTF8 | ConvertFrom-Json
    $token = (Get-Content -LiteralPath $descriptor.secret_file -Encoding UTF8).Trim()
    $health = Invoke-RestMethod -Uri ($descriptor.endpoint + "/health") -TimeoutSec 2
    if ($health.status -ne "ok") {
        throw "Core health response is not ok"
    }
    $headers = @{ Authorization = "Bearer $token" }
    $snapshot = Invoke-RestMethod -Uri ($descriptor.endpoint + "/v1/snapshot") -Headers $headers -TimeoutSec 2
    if ($null -eq $snapshot.revision -or $null -eq $snapshot.agents) {
        throw "Core snapshot contract is incomplete"
    }

    $secondCore = Start-Process -FilePath $Executable -PassThru -WindowStyle Hidden
    if (-not $secondCore.WaitForExit(3000)) {
        throw "Second Core process did not reject duplicate ownership"
    }

    [pscustomobject]@{
        health = $health.status
        protocol = $health.protocol_version
        revision = $snapshot.revision
        agents = @($snapshot.agents).Count
        duplicate_exit_code = $secondCore.ExitCode
    } | ConvertTo-Json -Compress
}
finally {
    if ($null -ne $secondCore -and -not $secondCore.HasExited) {
        Stop-Process -Id $secondCore.Id -Force
    }
    if ($null -ne $core -and -not $core.HasExited) {
        Stop-Process -Id $core.Id -Force
        $core.WaitForExit(3000) | Out-Null
    }
    $env:LOCALAPPDATA = $previousLocalAppData
    $resolved = [System.IO.Path]::GetFullPath($testRoot)
    if ($resolved.StartsWith($tempBase, [System.StringComparison]::OrdinalIgnoreCase) -and
        (Split-Path -Leaf $resolved).StartsWith("petcrew-core-smoke-")) {
        Remove-Item -LiteralPath $resolved -Recurse -Force -ErrorAction SilentlyContinue
    }
}
