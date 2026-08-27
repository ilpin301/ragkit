<#
Zip this kit for another machine.

    .\pack.ps1 [-Out <path to .zip>]

Excludes what must not travel: the venv (absolute paths baked into it),
machine.ps1 (per-machine facts), git history, caches, and every .env (keys).
#>
param([string]$Out)

$ErrorActionPreference = 'Stop'
$kit = $PSScriptRoot
if (-not $Out) { $Out = Join-Path (Split-Path -Parent $kit) "RAG-kit-$(Get-Date -Format yyyyMMdd).zip" }

$skip = '^\.venv$|^\.git$|^__pycache__$|^machine\.ps1$|^\.claude$|\.pyc$|^\.env$|^LOG$'
$stage = Join-Path ([IO.Path]::GetTempPath()) "ragkit-pack-$(Get-Random)"
New-Item -ItemType Directory -Force -Path $stage | Out-Null
try {
  Get-ChildItem -LiteralPath $kit -Force |
    Where-Object { $_.Name -notmatch $skip } |
    Copy-Item -Destination $stage -Recurse -Force
  # a nested __pycache__ or .env survives the top-level filter
  Get-ChildItem -LiteralPath $stage -Recurse -Force |
    Where-Object { $_.Name -match $skip } |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

  if (Test-Path -LiteralPath $Out) { Remove-Item -LiteralPath $Out -Force }
  Compress-Archive -Path (Join-Path $stage '*') -DestinationPath $Out -CompressionLevel Optimal
  $mb = [math]::Round((Get-Item -LiteralPath $Out).Length / 1MB, 1)
  Write-Host "wrote $Out ($mb MB)"
  Write-Host "on the target machine: unzip, .\bootstrap.ps1, set RAGKIT_HOME, restart shells"
} finally { Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue }
