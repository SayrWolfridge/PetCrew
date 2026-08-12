param(
    [Parameter(Mandatory = $true)]
    [int]$ProcessId,
    [Parameter(Mandatory = $true)]
    [int]$X,
    [Parameter(Mandatory = $true)]
    [int]$Y,
    [int]$Width = 0,
    [int]$Height = 0
)

$ErrorActionPreference = "Stop"

Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;

public static class PetCrewWindowRecovery {
    public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);

    [DllImport("user32.dll")]
    public static extern bool EnumWindows(EnumWindowsProc callback, IntPtr lParam);

    [DllImport("user32.dll")]
    public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint processId);

    [DllImport("user32.dll")]
    public static extern bool IsWindowVisible(IntPtr hWnd);

    [DllImport("user32.dll")]
    public static extern bool SetWindowPos(
        IntPtr hWnd,
        IntPtr insertAfter,
        int x,
        int y,
        int width,
        int height,
        uint flags
    );
}
"@

$script:windowHandle = [IntPtr]::Zero
[PetCrewWindowRecovery]::EnumWindows(
    {
        param([IntPtr]$handle, [IntPtr]$state)
        [uint32]$owner = 0
        [void][PetCrewWindowRecovery]::GetWindowThreadProcessId($handle, [ref]$owner)
        if ($owner -eq $ProcessId -and [PetCrewWindowRecovery]::IsWindowVisible($handle)) {
            $script:windowHandle = $handle
            return $false
        }
        return $true
    },
    [IntPtr]::Zero
) | Out-Null

if ($script:windowHandle -eq [IntPtr]::Zero) {
    throw "No visible top-level window found for PID $ProcessId"
}

$SWP_NOZORDER = 0x0004
$SWP_SHOWWINDOW = 0x0040
$flags = $SWP_NOZORDER -bor $SWP_SHOWWINDOW
if ($Width -le 0 -or $Height -le 0) {
    $flags = $flags -bor 0x0001
}
if (-not [PetCrewWindowRecovery]::SetWindowPos(
    $script:windowHandle,
    [IntPtr]::Zero,
    $X,
    $Y,
    $Width,
    $Height,
    $flags
)) {
    throw "SetWindowPos failed"
}

[pscustomobject]@{
    process_id = $ProcessId
    handle = $script:windowHandle.ToInt64()
    x = $X
    y = $Y
    width = $Width
    height = $Height
} | ConvertTo-Json -Compress
