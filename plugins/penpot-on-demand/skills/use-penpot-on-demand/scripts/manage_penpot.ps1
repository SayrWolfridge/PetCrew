[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("status", "start", "stop")]
    [string]$Mode,

    [ValidateRange(1, 300)]
    [int]$TimeoutSeconds = 120
)

$ErrorActionPreference = "Stop"
$taskName = "Tribunska Penpot MCP"
$mcpPort = 4401
$relatedPorts = @(4400, 4401, 4402)
$expectedVersion = "2.15.4"
$expectedEntrypointFragment = "@penpot\mcp\bin\mcp-local.js"

function Get-ListenerRows {
    $rows = @()
    foreach ($port in $relatedPorts) {
        $open = $false
        $connectedAddress = $null
        foreach ($loopbackAddress in @("127.0.0.1", "::1")) {
            $client = [System.Net.Sockets.TcpClient]::new()
            try {
                $attempt = $client.ConnectAsync($loopbackAddress, $port)
                $open = $attempt.Wait(300) -and $client.Connected
                if ($open) {
                    $connectedAddress = $loopbackAddress
                    break
                }
            }
            catch {
                $open = $false
            }
            finally {
                $client.Dispose()
            }
        }
        if ($open) {
            $rows += [ordered]@{
                address = $connectedAddress
                port = [int]$port
            }
        }
    }
    return @($rows)
}

function Find-PenpotMcpEntrypoint {
    $searchPath = Join-Path $env:LOCALAPPDATA "npm-cache\_npx\*\node_modules\@penpot\mcp\bin\mcp-local.js"
    $candidates = Get-ChildItem -Path $searchPath -File -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending
    foreach ($candidate in $candidates) {
        $packageRoot = Split-Path -Parent (Split-Path -Parent $candidate.FullName)
        $manifestPath = Join-Path $packageRoot "package.json"
        if (-not (Test-Path -LiteralPath $manifestPath)) {
            continue
        }
        $manifest = Get-Content -Raw -LiteralPath $manifestPath -Encoding UTF8 | ConvertFrom-Json
        if ($manifest.version -eq $expectedVersion) {
            return $candidate.FullName
        }
    }
    return $null
}

function Get-TaskSnapshot {
    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction Stop
    $info = Get-ScheduledTaskInfo -TaskName $taskName -ErrorAction Stop
    return [ordered]@{
        task_name = $taskName
        task_state = [string]$task.State
        last_task_result = [int64]$info.LastTaskResult
        listeners = @(Get-ListenerRows)
    }
}

function Write-Result {
    param([System.Collections.IDictionary]$Result)
    $Result | ConvertTo-Json -Depth 6
}

if ($Mode -eq "status") {
    $snapshot = Get-TaskSnapshot
    Write-Result ([ordered]@{
        mode = "status"
        ready = @($snapshot.listeners | Where-Object { $_.port -eq $mcpPort }).Count -gt 0
        snapshot = $snapshot
    })
    return
}

if ($Mode -eq "start") {
    $before = Get-TaskSnapshot
    $existing = @($before.listeners | Where-Object { $_.port -eq $mcpPort })
    if ($existing.Count -gt 0) {
        Write-Result ([ordered]@{
            mode = "start"
            state = "already_running"
            started_by_request = $false
            ready = $true
            snapshot = $before
        })
        return
    }

    if ($null -eq (Find-PenpotMcpEntrypoint)) {
        Write-Result ([ordered]@{
            mode = "start"
            state = "cache_missing"
            started_by_request = $false
            ready = $false
            expected_version = $expectedVersion
            snapshot = $before
        })
        return
    }

    Start-ScheduledTask -TaskName $taskName
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        Start-Sleep -Milliseconds 500
        $listeners = @(Get-ListenerRows)
        $listener = @($listeners | Where-Object { $_.port -eq $mcpPort })
        if ($listener.Count -gt 0) {
            $snapshot = Get-TaskSnapshot
            Write-Result ([ordered]@{
                mode = "start"
                state = "started"
                started_by_request = $true
                ready = $true
                snapshot = $snapshot
            })
            return
        }
    } while ((Get-Date) -lt $deadline)

    Write-Result ([ordered]@{
        mode = "start"
        state = "timeout"
        started_by_request = $true
        ready = $false
        snapshot = (Get-TaskSnapshot)
    })
    return
}

$beforeStop = Get-TaskSnapshot
if ($beforeStop.task_state -eq "Running") {
    Stop-ScheduledTask -TaskName $taskName
}

$deadline = (Get-Date).AddSeconds([Math]::Min($TimeoutSeconds, 30))
do {
    Start-Sleep -Milliseconds 500
    $listeners = @(Get-ListenerRows)
    $remaining = @($listeners | Where-Object { $_.port -eq $mcpPort })
    if ($remaining.Count -eq 0) {
        $afterStop = Get-TaskSnapshot
        Write-Result ([ordered]@{
            mode = "stop"
            state = "stopped"
            ready = $false
            snapshot = $afterStop
        })
        return
    }
} while ((Get-Date) -lt $deadline)

$afterStop = Get-TaskSnapshot
$survivorListeners = Get-NetTCPConnection -State Listen -LocalPort $mcpPort -ErrorAction SilentlyContinue
$verified = @()
foreach ($listener in $survivorListeners) {
    $process = Get-CimInstance Win32_Process -Filter "ProcessId = $($listener.OwningProcess)" -ErrorAction SilentlyContinue
    $verified += [ordered]@{
        pid = [int]$listener.OwningProcess
        command_matches_penpot = (
            $null -ne $process -and
            [string]$process.CommandLine -like "*$expectedEntrypointFragment*"
        )
    }
}

Write-Result ([ordered]@{
    mode = "stop"
    state = "listener_survived"
    ready = $true
    survivor = $verified
    snapshot = $afterStop
})
return
