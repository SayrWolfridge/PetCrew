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
python "$repoRoot\tools\validate_json.py" $repoRoot
if ($LASTEXITCODE -ne 0) {
    throw "JSON syntax validation failed with exit code $LASTEXITCODE"
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

    git -c "safe.directory=$gitSafeDirectory" -C $repoRoot show-ref --verify --quiet 'refs/heads/main'
    if ($LASTEXITCODE -ne 0) {
        throw 'Missing canonical public branch: main'
    }

    $publicRoots = @(git -c "safe.directory=$gitSafeDirectory" -C $repoRoot rev-list --max-parents=0 --all | Sort-Object -Unique)
    if ($LASTEXITCODE -ne 0 -or $publicRoots.Count -ne 1) {
        throw 'All local branches and tags must share exactly one public root commit'
    }

    $privateHistoryPaths = @(
        git -c "safe.directory=$gitSafeDirectory" -C $repoRoot log --all --format= --name-only -- '_Agents' 'PROJECT.md' 'artifacts/backups' 'tmp' |
            Where-Object { $_ } |
            Sort-Object -Unique
    )
    if ($LASTEXITCODE -ne 0 -or $privateHistoryPaths) {
        throw "Private operational paths remain in public history: $($privateHistoryPaths -join ', ')"
    }

    $expectedPublicEmail = 'noreply' + '@' + 'petcrew.invalid'
    $expectedPublicIdentity = "PetCrew|$expectedPublicEmail|PetCrew|$expectedPublicEmail"
    $publicIdentities = @(
        git -c "safe.directory=$gitSafeDirectory" -C $repoRoot log --all --format='%an|%ae|%cn|%ce' |
            Sort-Object -Unique
    )
    if ($LASTEXITCODE -ne 0 -or $publicIdentities.Count -ne 1 -or $publicIdentities[0] -ne $expectedPublicIdentity) {
        throw 'Public history contains non-project Git author metadata'
    }

    $configuredName = git -c "safe.directory=$gitSafeDirectory" -C $repoRoot config --local --get user.name 2>$null
    $configuredNameStatus = $LASTEXITCODE
    $configuredEmail = git -c "safe.directory=$gitSafeDirectory" -C $repoRoot config --local --get user.email 2>$null
    $configuredEmailStatus = $LASTEXITCODE
    if ($configuredNameStatus -gt 1 -or $configuredEmailStatus -gt 1) {
        throw 'Could not inspect repository-local Git author identity'
    }
    $hasConfiguredIdentity = $configuredNameStatus -eq 0 -or $configuredEmailStatus -eq 0
    if ($hasConfiguredIdentity -and "$configuredName|$configuredEmail" -ne "PetCrew|$expectedPublicEmail") {
        throw 'Repository-local Git author identity is not publication-safe'
    }

    $historySensitiveHits = @()
    $historyCommits = @(git -c "safe.directory=$gitSafeDirectory" -C $repoRoot rev-list --all)
    if ($LASTEXITCODE -ne 0) {
        throw 'Could not enumerate public history'
    }
    foreach ($commit in $historyCommits) {
        foreach ($entry in $sensitiveContentPatterns.GetEnumerator()) {
            $paths = @(git -c "safe.directory=$gitSafeDirectory" -C $repoRoot grep -I -l -E -e $entry.Value $commit -- 2>$null)
            if ($LASTEXITCODE -gt 1) {
                throw "History scan failed at $commit ($($entry.Key))"
            }
            foreach ($path in $paths) {
                $relativePath = $path.Substring($path.IndexOf(':') + 1)
                $historySensitiveHits += "$($commit.Substring(0, 12)):$relativePath ($($entry.Key))"
            }
        }
    }
    if ($historySensitiveHits) {
        throw "Sensitive-looking content remains in public history: $($historySensitiveHits -join ', ')"
    }
}

Write-Host "PetCrew verification passed."
