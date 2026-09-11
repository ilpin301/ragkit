<#
Create a new LightRAG base. One command, one argument.

    .\new_base.ps1 -Name MECH_RAG [-Path <parent dir>] [-Port N]
                   [-From <existing base>] [-ApiKey <llm key>] [-Start]

The base folder holds ONLY the base: its .env, its compose file, its store, its
inbox and its ledger. Every script, the venv and the skills stay here in the kit
and are shared by every base on the machine.

-Path    where the base folder is created. Default: the kit's parent directory.
-Port    host port. Default: the first free port from 9621 that no sibling base
         already claims in its .env.
-From    seed the .env from an existing base (keeps its LLM keys and bindings)
         instead of template\env.template. PORT, LIGHTRAG_API_KEY,
         COMPOSE_PROJECT_NAME and WEBUI_TITLE are always regenerated; every
         other key is copied through untouched.
-ApiKey  LLM provider key. Falls back to $env:ZAI_API_KEY, then to the key in
         -From. Without any of them the .env is written with a placeholder and
         the script warns - the base will not ingest until it is filled in.
-Start   run 'docker compose up -d' and wait for the server to answer.
#>
param(
  [Parameter(Mandatory = $true)][string]$Name,
  [string]$Path,
  [int]$Port = 0,
  [string]$From,
  [string]$ApiKey,
  [switch]$Start
)

$ErrorActionPreference = 'Stop'
$kit = $PSScriptRoot

if ($Name -notmatch '^[A-Za-z0-9_.-]+$') {
  throw "new_base.ps1: -Name '$Name' must be a plain folder name (letters, digits, _ . -)"
}
if (-not $Path) { $Path = Split-Path -Parent $kit }
$root = Join-Path $Path $Name
$baseLr = Join-Path $root 'lightrag'
if (Test-Path -LiteralPath $baseLr) {
  throw "new_base.ps1: $baseLr already exists - refusing to touch an existing base"
}

# --- port -------------------------------------------------------------------
# Two guards, because either alone is not enough: a sibling base that is merely
# stopped holds no socket, and a foreign process can hold a port no .env names.
function Test-PortFree([int]$p) {
  try {
    $l = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Any, $p)
    $l.Start(); $l.Stop(); return $true
  } catch { return $false }
}
$claimed = @(Get-ChildItem -LiteralPath $Path -Directory -ErrorAction SilentlyContinue |
  ForEach-Object { Join-Path $_.FullName 'lightrag\.env' } |
  Where-Object { Test-Path -LiteralPath $_ } |
  ForEach-Object { (Get-Content -LiteralPath $_ | Select-String '^PORT=' | Select-Object -First 1) } |
  ForEach-Object { [int]($_.Line.Split('=', 2)[1].Trim()) })
if ($Port -gt 0) {
  if ($claimed -contains $Port) { throw "new_base.ps1: port $Port is already claimed by a sibling base" }
  if (-not (Test-PortFree $Port)) { throw "new_base.ps1: port $Port is in use" }
} else {
  $Port = 9621..9700 | Where-Object { $claimed -notcontains $_ -and (Test-PortFree $_) } | Select-Object -First 1
  if (-not $Port) { throw "new_base.ps1: no free port in 9621-9700" }
}

# --- identity ---------------------------------------------------------------
# COMPOSE_PROJECT_NAME is mandatory, not cosmetic: every base's compose dir is
# named 'lightrag', so without it docker compose derives the same project for
# all of them and 'up -d' recreates another base's container.
$project = ($Name.ToLower() -replace '[^a-z0-9_]', '_')
$serverKey = -join ((1..32) | ForEach-Object { '0123456789abcdef'[(Get-Random -Maximum 16)] })

# --- .env -------------------------------------------------------------------
if ($From) {
  $srcEnv = Join-Path (Resolve-Path -LiteralPath $From).Path 'lightrag\.env'
  if (-not (Test-Path -LiteralPath $srcEnv)) { throw "new_base.ps1: -From has no lightrag\.env: $srcEnv" }
  $lines = Get-Content -LiteralPath $srcEnv -Encoding UTF8
  if (-not $ApiKey) {
    # Assign through an untyped local: $ApiKey is a [string] param, so storing a
    # MatchInfo in it coerces to text and loses .Line.
    $hit = $lines | Select-String '^(ZAI_API_KEY|LLM_BINDING_API_KEY)=' | Select-Object -First 1
    if ($hit) { $ApiKey = $hit.Line.Split('=', 2)[1].Trim() }
  }
  $envText = ($lines | ForEach-Object {
    switch -Regex ($_) {
      '^PORT='                 { "PORT=$Port"; break }
      '^LIGHTRAG_API_KEY='     { "LIGHTRAG_API_KEY=$serverKey"; break }
      '^COMPOSE_PROJECT_NAME=' { "COMPOSE_PROJECT_NAME=$project"; break }
      '^WEBUI_TITLE='          { "WEBUI_TITLE=$Name"; break }
      default                  { $_ }
    }
  }) -join "`n"
} else {
  if (-not $ApiKey) { $ApiKey = $env:ZAI_API_KEY }
  $envText = (Get-Content -LiteralPath (Join-Path $kit 'template\env.template') -Encoding UTF8 -Raw).
    Replace('__PORT__', "$Port").
    Replace('__SERVER_KEY__', $serverKey).
    Replace('__PROJECT__', $project).
    Replace('__NAME__', $Name).
    Replace('__LLM_API_KEY__', $(if ($ApiKey) { $ApiKey } else { 'PUT_YOUR_LLM_API_KEY_HERE' }))
}

# --- vector backend keys ----------------------------------------------------
# Applied to BOTH paths on purpose. -From copies every key through untouched,
# so a base seeded from a Qdrant base would inherit its QDRANT_PORT and the two
# containers would fight over one host port. Derived from $Port, then checked
# against the siblings and the live sockets: bases created before this
# derivation existed picked their QDRANT_PORT by hand, so the derived value
# can still land on one that is already taken (PCM_RAG holds 6333 on port 9622).
$qdrantClaimed = @(Get-ChildItem -LiteralPath $Path -Directory -ErrorAction SilentlyContinue |
  ForEach-Object { Join-Path $_.FullName 'lightrag\.env' } |
  Where-Object { Test-Path -LiteralPath $_ } |
  ForEach-Object { (Get-Content -LiteralPath $_ | Select-String '^QDRANT_PORT=' | Select-Object -First 1) } |
  Where-Object { $_ } |
  ForEach-Object { [int]($_.Line.Split('=', 2)[1].Trim()) })
$qdrantPort = 6333 + ($Port - 9621)
while ($qdrantClaimed -contains $qdrantPort -or -not (Test-PortFree $qdrantPort)) {
  $qdrantPort++
  if ($qdrantPort -gt 6433) { throw "new_base.ps1: no free Qdrant port in 6333-6433" }
}
$envText = $envText -replace '(?m)^QDRANT_PORT=.*$', "QDRANT_PORT=$qdrantPort"
$envText = $envText -replace '(?m)^QDRANT_URL=.*$', "QDRANT_URL=http://127.0.0.1:$qdrantPort"
if ($envText -notmatch '(?m)^QDRANT_PORT=') {
  $envText += "`nQDRANT_PORT=$qdrantPort`nQDRANT_URL=http://127.0.0.1:$qdrantPort"
}
if ($envText -notmatch '(?m)^LIGHTRAG_VECTOR_STORAGE=') {
  $envText += "`nLIGHTRAG_VECTOR_STORAGE=NanoVectorDBStorage"
}

# --- layout -----------------------------------------------------------------
foreach ($d in @(
  $root,
  (Join-Path $root 'IN'),
  $baseLr,
  (Join-Path $baseLr 'LOG'),
  (Join-Path $baseLr 'data\rag_storage'),
  (Join-Path $baseLr 'data\inputs'),
  (Join-Path $baseLr 'data\prompts')
)) { New-Item -ItemType Directory -Force -Path $d | Out-Null }

Set-Content -LiteralPath (Join-Path $baseLr '.env') -Value $envText -Encoding UTF8 -NoNewline
Copy-Item -LiteralPath (Join-Path $kit 'template\docker-compose.yml') -Destination (Join-Path $baseLr 'docker-compose.yml')
if (-not (Test-Path -LiteralPath (Join-Path $baseLr 'INGESTED_SOURCES.txt'))) {
  New-Item -ItemType File -Path (Join-Path $baseLr 'INGESTED_SOURCES.txt') | Out-Null
}

Write-Host "base:    $root"
Write-Host "port:    $Port   (qdrant $qdrantPort)"
Write-Host "project: $project"
Write-Host "api key: $serverKey   (X-API-Key header; also in lightrag\.env)"
if (-not $ApiKey) {
  Write-Warning "no LLM key: set LLM_BINDING_API_KEY in $baseLr\.env before ingesting"
}

# --- start ------------------------------------------------------------------
if ($Start) {
  Push-Location $baseLr
  try {
    docker compose up -d
    $url = "http://localhost:$Port/health"
    foreach ($i in 1..30) {
      Start-Sleep -Seconds 2
      try {
        $r = Invoke-RestMethod -Uri $url -Headers @{ 'X-API-Key' = $serverKey } -TimeoutSec 5
        Write-Host "server:  up at http://localhost:$Port  (status $($r.status))"
        break
      } catch { if ($i -eq 30) { Write-Warning "server did not answer $url within 60s - check 'docker compose logs'" } }
    }
  } finally { Pop-Location }
} else {
  Write-Host ""
  Write-Host "next:    cd '$baseLr'; docker compose up -d"
  Write-Host "         drop PDFs into '$root\IN' then run /il-rag-ingest"
}
