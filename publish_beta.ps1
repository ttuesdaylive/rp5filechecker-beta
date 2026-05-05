param(
    [string]$BetaBranch = "beta",
    [string]$BaseBranch = "main",
    [string]$BetaTag = "beta",
    [switch]$AllowCleanWorktree
)

$ErrorActionPreference = "Stop"

$RepoRoot = $PSScriptRoot
if (-not $RepoRoot) {
    $RepoRoot = (Get-Location).Path
}
Set-Location -LiteralPath $RepoRoot

function Fail($Message) {
    Write-Error $Message
    exit 1
}

function RunGit($Arguments) {
    & git -C $RepoRoot @Arguments
    if ($LASTEXITCODE -ne 0) {
        Fail "git $($Arguments -join ' ') failed."
    }
}

& git config --global --add safe.directory $RepoRoot
if ($LASTEXITCODE -ne 0) {
    Fail "Failed to mark repository as a safe git directory."
}

& git -C $RepoRoot rev-parse --is-inside-work-tree *> $null
if ($LASTEXITCODE -ne 0) {
    Fail "This folder is not a git repository."
}

$remoteNames = @(& git -C $RepoRoot remote)
if (-not $remoteNames -or $remoteNames.Count -eq 0) {
    Fail "No git remote is configured. Add a GitHub remote named 'origin' first."
}

$statusLines = @(& git -C $RepoRoot status --porcelain)
$needsCommit = $statusLines.Count -gt 0
if (-not $needsCommit -and -not $AllowCleanWorktree) {
    Write-Output "No local changes detected. Beta remains unchanged."
    exit 0
}

$currentBranch = (& git -C $RepoRoot branch --show-current).Trim()
if (-not $currentBranch) {
    Fail "Could not determine the current branch."
}

if ($needsCommit) {
    RunGit @("add", "-A")
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss zzz"
    $commitMessage = "Update beta from PC snapshot $timestamp"
    RunGit @("commit", "-m", $commitMessage)
}

$betaBranchOutput = @(& git -C $RepoRoot branch --list $BetaBranch)
$hasBetaBranch = $betaBranchOutput.Count -gt 0
if ($hasBetaBranch) {
    RunGit @("branch", "-f", $BetaBranch, "HEAD")
} else {
    RunGit @("branch", $BetaBranch, "HEAD")
}

RunGit @("tag", "-f", $BetaTag, "HEAD")
RunGit @("push", "origin", "HEAD:$BaseBranch")
RunGit @("push", "origin", "--force", "$BetaBranch:$BetaBranch")
RunGit @("push", "origin", "--force", "$BetaTag")

if ($currentBranch -ne $BaseBranch) {
    RunGit @("checkout", $currentBranch)
}

if ($needsCommit) {
    Write-Output "Published commit to '$BaseBranch' and refreshed '$BetaBranch'/'$BetaTag'."
} else {
    Write-Output "Published current HEAD to '$BetaBranch'/'$BetaTag' from a clean working tree."
}
