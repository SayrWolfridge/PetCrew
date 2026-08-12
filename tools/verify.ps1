param(
    [switch]$PublicAudit
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$gitSafeDirectory = $repoRoot.Replace('\', '/')

function Invoke-RepositoryStep {
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

Invoke-RepositoryStep "UI tests" "$repoRoot\apps\overlay" { npm test -- --run }
Invoke-RepositoryStep "Web build" "$repoRoot\apps\overlay" { npm run build }
Invoke-RepositoryStep "Rust format" "$repoRoot\apps\overlay\src-tauri" { cargo fmt --check }
Invoke-RepositoryStep "Rust tests" "$repoRoot\apps\overlay\src-tauri" { cargo test }
Invoke-RepositoryStep "Codex bridge tests" $repoRoot { python -m unittest discover -s plugins\petcrew\tests }
Invoke-RepositoryStep "OpenCode adapter tests" $repoRoot { node --test adapters\opencode\petcrew.test.mjs }

Write-Host "[JSON syntax]"
$jsonFiles = Get-ChildItem -LiteralPath $repoRoot -Recurse -File -Filter *.json |
    Where-Object {
        $_.FullName -notmatch '\\node_modules\\' -and
        $_.FullName -notmatch '\\target\\' -and
        $_.FullName -notmatch '\\dist\\' -and
        $_.FullName -notmatch '\\artifacts\\backups\\' -and
        $_.FullName -notmatch '\\tmp\\' -and
        $_.FullName -notmatch '\\_Agents\\'
    }
foreach ($jsonFile in $jsonFiles) {
    $null = Get-Content -LiteralPath $jsonFile.FullName -Raw -Encoding UTF8 | ConvertFrom-Json -AsHashtable
}

Invoke-RepositoryStep "Git whitespace" $repoRoot {
    git -c "safe.directory=$gitSafeDirectory" -C $repoRoot diff --check
}

Write-Host "[Public working-tree hygiene]"
$publicCandidates = git -c "safe.directory=$gitSafeDirectory" -C $repoRoot ls-files --cached --others --exclude-standard
if ($LASTEXITCODE -ne 0) {
    throw "Could not enumerate public working-tree candidates"
}

$forbiddenPaths = $publicCandidates | Where-Object {
    $_ -match '^(artifacts/backups|tmp)/' -or
    $_ -match '^_Agents/.+_card\.json$'
}
if ($forbiddenPaths) {
    throw "Local-only files are visible to Git: $($forbiddenPaths -join ', ')"
}

$textExtensions = @('.css', '.html', '.js', '.json', '.md', '.mjs', '.ps1', '.py', '.rs', '.toml', '.ts', '.tsx', '.yaml', '.yml')
$localPathHits = @()
$sensitiveContentHits = @()
$sensitiveContentPatterns = [ordered]@{
    'private key' = '-----BEGIN ([A-Z0-9 ]+ )?PRIVATE KEY-----'
    'known token prefix' = '(AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16}|github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9_-]{20,}|xox[baprs]-[A-Za-z0-9-]{10,}|AIza[0-9A-Za-z_-]{35}|ya29\.[0-9A-Za-z_-]+)'
    'JWT' = 'eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}'
    'email address' = '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}'
    'machine-specific user path' = 'C:\\Users\\[^\\\s]+'
    'private workspace path' = 'C:\\Work\\'
}
foreach ($relativePath in $publicCandidates) {
    if ($relativePath -match '^_Agents/' -or $relativePath -eq 'PROJECT.md') {
        continue
    }
    $fullPath = Join-Path $repoRoot $relativePath
    if (-not (Test-Path -LiteralPath $fullPath -PathType Leaf)) {
        continue
    }
    if ($textExtensions -notcontains [System.IO.Path]::GetExtension($fullPath).ToLowerInvariant()) {
        continue
    }
    $content = Get-Content -LiteralPath $fullPath -Raw -Encoding UTF8
    if ($content -match 'C:\\Users\\[A-Za-z0-9._-]+') {
        $localPathHits += $relativePath
    }
    foreach ($entry in $sensitiveContentPatterns.GetEnumerator()) {
        if ($content -match $entry.Value) {
            $sensitiveContentHits += "$relativePath ($($entry.Key))"
        }
    }
}
if ($localPathHits) {
    throw "Machine-specific user paths remain in publishable files: $($localPathHits -join ', ')"
}
if ($sensitiveContentHits) {
    throw "Sensitive-looking content remains in publishable files: $($sensitiveContentHits -join ', ')"
}

if ($PublicAudit) {
    $privateTracked = git -c "safe.directory=$gitSafeDirectory" -C $repoRoot ls-files -- '_Agents' 'PROJECT.md' 'artifacts/backups' 'tmp'
    if ($privateTracked) {
        throw "Private operational paths are still tracked: $($privateTracked -join ', ')"
    }

    $publicBranch = 'codex/public-ready'
    git -c "safe.directory=$gitSafeDirectory" -C $repoRoot show-ref --verify --quiet "refs/heads/$publicBranch"
    if ($LASTEXITCODE -ne 0) {
        throw "Missing local publication branch: $publicBranch"
    }

    $publicCommitCount = git -c "safe.directory=$gitSafeDirectory" -C $repoRoot rev-list --count $publicBranch
    if ($LASTEXITCODE -ne 0 -or $publicCommitCount -ne '1') {
        throw "$publicBranch must contain exactly one root commit"
    }

    $currentTree = git -c "safe.directory=$gitSafeDirectory" -C $repoRoot rev-parse 'HEAD^{tree}'
    $publicTree = git -c "safe.directory=$gitSafeDirectory" -C $repoRoot rev-parse "$publicBranch^{tree}"
    if ($LASTEXITCODE -ne 0 -or $currentTree -ne $publicTree) {
        throw "$publicBranch does not match the current verified source tree"
    }

    $expectedPublicEmail = 'noreply' + '@' + 'petcrew.invalid'
    $expectedPublicIdentity = "PetCrew|$expectedPublicEmail|PetCrew|$expectedPublicEmail"
    $publicIdentity = git -c "safe.directory=$gitSafeDirectory" -C $repoRoot show -s --format='%an|%ae|%cn|%ce' $publicBranch
    if ($LASTEXITCODE -ne 0 -or $publicIdentity -ne $expectedPublicIdentity) {
        throw "$publicBranch contains non-public Git author metadata"
    }
}

Write-Host "PetCrew verification passed."
