param(
    [switch]$Development
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$overlayRoot = Join-Path $repoRoot "apps\overlay"
$tauriRoot = Join-Path $overlayRoot "src-tauri"
$targetTriple = "x86_64-pc-windows-msvc"
$gitSafeDirectory = $repoRoot.Replace('\', '/')

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)][string]$Label,
        [Parameter(Mandatory = $true)][string]$WorkingDirectory,
        [Parameter(Mandatory = $true)][scriptblock]$Action
    )
    Write-Host "[$Label]"
    Push-Location -LiteralPath $WorkingDirectory
    try {
        & $Action
        if ($LASTEXITCODE -ne 0) {
            throw "$Label failed with exit code $LASTEXITCODE"
        }
    }
    finally {
        Pop-Location
    }
}

$status = @(git -c "safe.directory=$gitSafeDirectory" -C $repoRoot status --porcelain=v1 --untracked-files=all)
if ($LASTEXITCODE -ne 0) {
    throw "Could not inspect the Git working tree"
}
if ($status.Count -gt 0 -and -not $Development) {
    throw "Release builds require a clean working tree. Use -Development only for a non-publishable local alpha."
}

$package = Get-Content -LiteralPath (Join-Path $overlayRoot "package.json") -Raw -Encoding UTF8 | ConvertFrom-Json
$tauri = Get-Content -LiteralPath (Join-Path $tauriRoot "tauri.conf.json") -Raw -Encoding UTF8 | ConvertFrom-Json
$cargoText = Get-Content -LiteralPath (Join-Path $tauriRoot "Cargo.toml") -Raw -Encoding UTF8
$cargoVersionMatch = [regex]::Match($cargoText, '(?ms)^\[package\].*?^version\s*=\s*"([^"]+)"')
if (-not $cargoVersionMatch.Success) {
    throw "Could not read the Cargo package version"
}
$version = [string]$package.version
if ($version -ne [string]$tauri.version -or $version -ne $cargoVersionMatch.Groups[1].Value) {
    throw "Version mismatch across package.json, tauri.conf.json, and Cargo.toml"
}

$rustHost = (rustc -vV | Where-Object { $_ -like 'host:*' }).Substring(5).Trim()
if ($LASTEXITCODE -ne 0 -or $rustHost -ne $targetTriple) {
    throw "The alpha installer requires the standard $targetTriple Rust host"
}

$sourceCommit = git -c "safe.directory=$gitSafeDirectory" -C $repoRoot rev-parse HEAD
if ($LASTEXITCODE -ne 0) {
    throw "Could not resolve the source commit"
}
$dirty = $status.Count -gt 0

Invoke-Checked "Repository verification" $repoRoot {
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\tools\verify.ps1 -PublicAudit
}

Invoke-Checked "Standalone Core" $tauriRoot {
    cargo build --locked --release --bin petcrew-core
}

$coreSource = Join-Path $tauriRoot "target\release\petcrew-core.exe"
if (-not (Test-Path -LiteralPath $coreSource -PathType Leaf)) {
    throw "The standalone Core build did not produce petcrew-core.exe"
}

$licenseReport = Join-Path $tauriRoot "target\release\THIRD-PARTY-LICENSES.txt"
Invoke-Checked "Release license report" $repoRoot {
    python .\tools\generate_release_notices.py --output $licenseReport --source-commit $sourceCommit --target $targetTriple
}

Invoke-Checked "NSIS installer" $overlayRoot {
    npm run tauri -- build --bundles nsis --config src-tauri\tauri.alpha.conf.json
}

$bundleDirectory = Join-Path $tauriRoot "target\release\bundle\nsis"
$installers = @(Get-ChildItem -LiteralPath $bundleDirectory -Filter "*.exe" -File)
if ($installers.Count -ne 1) {
    throw "Expected exactly one NSIS installer, found $($installers.Count)"
}
$installer = $installers[0]

$generatedNsi = @(Get-ChildItem -LiteralPath (Join-Path $tauriRoot "target\release\nsis") -Recurse -Filter "installer.nsi" -File)
if ($generatedNsi.Count -ne 1) {
    throw "Expected exactly one generated NSIS script, found $($generatedNsi.Count)"
}
$nsiText = Get-Content -LiteralPath $generatedNsi[0].FullName -Raw -Encoding UTF8
$coreInstallCount = ([regex]::Matches($nsiText, '/oname=petcrew-core\.exe')).Count
$coreDeleteCount = ([regex]::Matches($nsiText, 'Delete "\$INSTDIR\\petcrew-core\.exe"')).Count
if ($coreInstallCount -ne 1 -or $coreDeleteCount -ne 1) {
    throw "Generated NSIS must install and delete petcrew-core.exe exactly once"
}
if ($nsiText -notmatch 'NSIS_HOOK_POSTINSTALL' -or $nsiText -notmatch 'NSIS_HOOK_PREUNINSTALL') {
    throw "Generated NSIS did not include the PetCrew lifecycle hooks"
}

$archiveListing = & "C:\Program Files\7-Zip\7z.exe" l -slt -- $installer.FullName
if ($LASTEXITCODE -ne 0) {
    throw "7-Zip could not inspect the NSIS installer"
}
foreach ($requiredEntry in @('PetCrew.exe', 'petcrew-core.exe', 'LICENSE.txt', 'THIRD-PARTY-LICENSES.txt')) {
    if (-not ($archiveListing | Select-String -SimpleMatch $requiredEntry -Quiet)) {
        throw "Installer archive is missing $requiredEntry"
    }
}

if ($Development) {
    $releaseName = "v$version-dev-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
}
else {
    $releaseName = "v$version"
}
$releaseDirectory = Join-Path $repoRoot "artifacts\releases\$releaseName"
if (Test-Path -LiteralPath $releaseDirectory) {
    throw "Release directory already exists: $releaseDirectory"
}
New-Item -ItemType Directory -Path $releaseDirectory | Out-Null

$installerDestination = Join-Path $releaseDirectory $installer.Name
Copy-Item -LiteralPath $installer.FullName -Destination $installerDestination
Copy-Item -LiteralPath (Join-Path $repoRoot "LICENSE") -Destination (Join-Path $releaseDirectory "LICENSE.txt")
Copy-Item -LiteralPath (Join-Path $repoRoot "THIRD_PARTY_NOTICES.md") -Destination $releaseDirectory
Copy-Item -LiteralPath $licenseReport -Destination $releaseDirectory

$releaseFiles = @(Get-ChildItem -LiteralPath $releaseDirectory -File | Sort-Object Name)
$checksums = foreach ($file in $releaseFiles) {
    $hash = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    "$hash  $($file.Name)"
}
$checksums | Set-Content -LiteralPath (Join-Path $releaseDirectory "SHA256SUMS.txt") -Encoding ASCII

$installerHash = (Get-FileHash -LiteralPath $installerDestination -Algorithm SHA256).Hash.ToLowerInvariant()
$manifest = [ordered]@{
    schema_version = 1
    product = "PetCrew"
    version = $version
    channel = "windows-alpha"
    target = $targetTriple
    source_commit = $sourceCommit
    working_tree_dirty = $dirty
    publishable = -not $dirty
    signed = $false
    installer = [ordered]@{
        file = $installer.Name
        bytes = (Get-Item -LiteralPath $installerDestination).Length
        sha256 = $installerHash
        archive_entries_verified = $true
    }
}
$manifest | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $releaseDirectory "release-manifest.json") -Encoding UTF8

Write-Host "WINDOWS_ALPHA_OK"
Write-Host "Directory: $releaseDirectory"
Write-Host "Installer: $($installer.Name)"
Write-Host "SHA-256: $installerHash"
if ($dirty) {
    Write-Warning "Development artifact: the working tree was dirty and this package must not be published."
}
