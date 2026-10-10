param(
  [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path,
  [string]$Destination = "",
  [switch]$FromGitHub,
  [string]$GitHubRepo = "",
  [string]$GhPath = ""
)

$ErrorActionPreference = "Stop"

# Resolve the GitHub CLI: explicit -GhPath, else PATH, else the Windows default install.
if ([string]::IsNullOrWhiteSpace($GhPath)) {
  $onPath = Get-Command gh -ErrorAction SilentlyContinue
  if ($onPath) { $GhPath = $onPath.Source }
  elseif (Test-Path "C:\Program Files\GitHub CLI\gh.exe") { $GhPath = "C:\Program Files\GitHub CLI\gh.exe" }
  else { throw "GitHub CLI (gh) not found. Install it, or pass -GhPath <path to gh.exe>." }
}

if ([string]::IsNullOrWhiteSpace($GitHubRepo)) {
  $owner = (& $GhPath api user --jq .login).Trim()
  if (-not $owner) { throw "Cannot determine GitHub login. Pass -GitHubRepo owner/name." }
  $GitHubRepo = "$owner/research-brief-daily"
}


$configPath = Join-Path $RepoRoot "automation\research_brief_config.json"
if (-not $Destination) {
  $config = Get-Content -Raw -LiteralPath $configPath | ConvertFrom-Json
  $Destination = $config.local_reference_archive_dir
}
if (-not $Destination) {
  throw "Destination is not set. Pass -Destination or set local_reference_archive_dir in automation\research_brief_config.json."
}

$tempClone = $null
$source = Join-Path $RepoRoot "reference_push_archive"
if ($FromGitHub) {
  if (-not (Test-Path -LiteralPath $GhPath)) {
    $GhPath = "gh"
  }
  $tempClone = Join-Path ([System.IO.Path]::GetTempPath()) ("research-brief-daily-" + [System.Guid]::NewGuid().ToString("N"))
  & $GhPath repo clone $GitHubRepo $tempClone -- --depth 1 | Out-Null
  if ($LASTEXITCODE -ne 0) {
    throw "Failed to clone $GitHubRepo with GitHub CLI."
  }
  $source = Join-Path $tempClone "reference_push_archive"
}
if (-not (Test-Path -LiteralPath $source)) {
  throw "No reference_push_archive folder exists yet. Run the brief workflow once with email enabled first, or pass -FromGitHub after the workflow commits the archive."
}

New-Item -ItemType Directory -Force -Path $Destination | Out-Null
Get-ChildItem -LiteralPath $source -Force | ForEach-Object {
  Copy-Item -LiteralPath $_.FullName -Destination $Destination -Recurse -Force
}
Write-Host "Synced weekly literature archive to $Destination"
if ($tempClone -and (Test-Path -LiteralPath $tempClone)) {
  Remove-Item -LiteralPath $tempClone -Recurse -Force
}
