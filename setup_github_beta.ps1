param(
    [string]$RepoUrl = "https://github.com/ttuesdaylive/rp5filechecker-beta.git",
    [string]$GitUserName = "ttuesdaylive",
    [string]$GitUserEmail = "ehlingjake@gmail.com"
)

$ErrorActionPreference = "Stop"

$RepoRoot = $PSScriptRoot
if (-not $RepoRoot) {
    $RepoRoot = (Get-Location).Path
}
Set-Location -LiteralPath $RepoRoot

function Step($Message) {
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function RunGit($Arguments) {
    & git -C $RepoRoot @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "git $($Arguments -join ' ') failed."
    }
}

& git config --global --add safe.directory $RepoRoot
if ($LASTEXITCODE -ne 0) {
    throw "Failed to mark repository as a safe git directory."
}

& git -C $RepoRoot rev-parse --is-inside-work-tree *> $null
if ($LASTEXITCODE -ne 0) {
    throw "This folder is not a git repository. Script path: $RepoRoot"
}

Step "Configuring local git identity"
RunGit @("config", "user.name", $GitUserName)
RunGit @("config", "user.email", $GitUserEmail)

$hasCommits = $true
$null = & git -C $RepoRoot rev-parse --quiet --verify HEAD 2>$null
if ($LASTEXITCODE -ne 0) {
    $hasCommits = $false
}

Step "Staging project files"
RunGit @("add", "-A")

if (-not $hasCommits) {
    Step "Creating initial commit"
    RunGit @("commit", "-m", "Initial project import")
} else {
    $pending = @(& git -C $RepoRoot status --porcelain)
    if ($pending.Count -gt 0) {
        Step "Committing local changes"
        RunGit @("commit", "-m", "Update project snapshot")
    } else {
        Write-Host "No new changes to commit." -ForegroundColor Yellow
    }
}

$configuredRemotes = @(& git -C $RepoRoot remote)
$hasOrigin = $configuredRemotes -contains "origin"
if ($hasOrigin) {
    Step "Updating origin remote"
    RunGit @("remote", "set-url", "origin", $RepoUrl)
} else {
    Step "Adding origin remote"
    RunGit @("remote", "add", "origin", $RepoUrl)
}

Step "Pushing main to GitHub"
RunGit @("push", "-u", "origin", "main")

Step "Refreshing beta branch/tag"
& powershell -ExecutionPolicy Bypass -File (Join-Path $RepoRoot "publish_beta.ps1") -AllowCleanWorktree
if ($LASTEXITCODE -ne 0) {
    throw "publish_beta.ps1 failed."
}

Write-Host ""
Write-Host "GitHub setup complete." -ForegroundColor Green
