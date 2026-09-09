<#
ragkit universal ingest launcher.

    ingest.ps1 -Root <base dir> -ListFile <utf8 list of PDFs> [-Merged] [-Pages N]

<base dir> is any RAG base: a folder containing lightrag\ with a .env and a
docker-compose. Nothing about a specific base is baked in here -- root, port,
container, API key and embedding dim are all discovered at runtime.

-ListFile is mandatory and always UTF-8. Paths are never passed as bare process
arguments: German filenames (Staeben, Verzerrungszustand) mangle through the
ANSI codepage.

Run it detached, so the run survives the caller:
    Start-Process pwsh -ArgumentList '-NoProfile','-File','<kit>\ingest.ps1',
      '-Root','<base>','-ListFile','<list>' -WindowStyle Hidden
#>
param(
  [Parameter(Mandatory = $true)][string]$Root,
  [Parameter(Mandatory = $true)][string]$ListFile,
  [switch]$Merged,
  [int]$Pages = 10
)

$ErrorActionPreference = 'Stop'

# --- machine facts (the only two per-machine values; see bootstrap.ps1) ------
$machine = Join-Path $PSScriptRoot 'machine.ps1'
if (-not (Test-Path -LiteralPath $machine)) {
  throw "ingest.ps1: $machine is missing - run bootstrap.ps1 once on this machine"
}
. $machine
if (-not $VENV)   { throw "ingest.ps1: machine.ps1 defines no `$VENV - re-run bootstrap.ps1" }
if ($null -eq $HasCUDA) { throw "ingest.ps1: machine.ps1 defines no `$HasCUDA - re-run bootstrap.ps1" }
$python = Join-Path $VENV 'python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw "ingest.ps1: no python at $python" }

# --- guards: everything that can refuse runs BEFORE the container is stopped -
# A guard that threw after the stop would skip the trailing 'docker compose
# start' and leave the base's server down until somebody noticed.
$Root = (Resolve-Path -LiteralPath $Root).Path
$baseLr = Join-Path $Root 'lightrag'
$envFile = Join-Path $baseLr '.env'
if (-not (Test-Path -LiteralPath $envFile)) { throw "ingest.ps1: $envFile not found - -Root must point at a RAG base" }

if (-not (Test-Path -LiteralPath $ListFile)) { throw "ingest.ps1: list file not found: $ListFile" }
$pdfs = @(Get-Content -LiteralPath $ListFile -Encoding UTF8 |
          ForEach-Object { $_.Trim() } |
          Where-Object { $_ -and -not $_.StartsWith('#') })
if ($pdfs.Count -eq 0) { throw "ingest.ps1: list file is empty: $ListFile" }
$missing = @($pdfs | Where-Object { -not (Test-Path -LiteralPath $_) })
if ($missing) { throw "ingest.ps1: unresolvable entries in list file: $($missing -join ', ')" }
if ($Merged -and $pdfs.Count -gt 1) {
  throw "ingest.ps1: -Merged takes exactly one source PDF, got $($pdfs.Count). Run it once per source."
}

function Get-EnvValue([string]$key) {
  $line = Get-Content -LiteralPath $envFile -Encoding UTF8 | Select-String "^$key=" | Select-Object -First 1
  if ($line) { return $line.Line.Split('=', 2)[1].Trim() }
  return $null
}
# ZAI_API_KEY wins; PCM's .env carries both keys, so precedence must be fixed.
$key = Get-EnvValue 'ZAI_API_KEY'
if (-not $key) { $key = Get-EnvValue 'LLM_BINDING_API_KEY' }
if (-not $key) { throw "ingest.ps1: neither ZAI_API_KEY nor LLM_BINDING_API_KEY in $envFile" }
if (-not (Get-EnvValue 'EMBEDDING_DIM')) { throw "ingest.ps1: EMBEDDING_DIM missing from $envFile - check_vectors.py must never guess the dim" }
# Without COMPOSE_PROJECT_NAME, docker compose derives the project from the folder
# name - 'lightrag' for every base - so the stop below would hit another base's
# container. Both existing bases already set it; a new base must too.
if (-not (Get-EnvValue 'COMPOSE_PROJECT_NAME')) { throw "ingest.ps1: COMPOSE_PROJECT_NAME missing from $envFile - refusing to run docker compose against an ambiguous project" }

$LOG = Join-Path $baseLr 'LOG'
New-Item -ItemType Directory -Force -Path $LOG | Out-Null   # a brand-new base has no LOG\
$runLog = Join-Path $LOG 'ingest_run.log'

# --- environment ------------------------------------------------------------
$env:Path = "$VENV;$env:Path"
$env:NO_PROXY = '*'
$env:PYTHONIOENCODING = 'utf-8'
# unbuffered: a hard hang must not swallow the last KBs of the run log
$env:PYTHONUNBUFFERED = '1'
$env:PYTHONINTMAXSTRDIGITS = '0'
$env:MINERU_DEVICE_MODE = $(if ($HasCUDA) { 'cuda' } else { 'cpu' })
$env:TIKTOKEN_CACHE_DIR = "$env:TEMP\data-gym-cache"
$env:ZAI_API_KEY = $key
$env:RAGBASE_ROOT = $Root
# Each base vendors its own lightrag\lightrag\ package, which shadowed the venv
# only because the old launcher ran from the clone root. Running from the kit
# would silently swap in the venv's lightrag_hku -- a third library variant the
# three monkey-patches were never tested against.
$env:PYTHONPATH = $baseLr
$vendored = Join-Path $baseLr 'lightrag'
$pkgSource = if (Test-Path -LiteralPath $vendored) { $vendored } else { "$VENV (venv lightrag_hku - base has no vendored package)" }

# --- committed to running: stop the container -------------------------------
Set-Location -LiteralPath $baseLr
# rag_ingest.py:4-5 -- shared JSON storage, concurrent writes corrupt it.
# "already down" is the desired state, so a stop failure is only fatal if
# something is still up afterwards.
try { docker compose stop 2>&1 | Out-Null } catch { }
$stillUp = & { try { docker compose ps --status running -q 2>$null } catch { $null } }
if ($stillUp) { throw "ingest.ps1: container still running after 'docker compose stop' - refusing to write to a live store" }

"=== ragkit ingest $(Get-Date -Format 's') root=$Root merged=$($Merged.IsPresent) files=$($pdfs.Count)" |
  Add-Content -LiteralPath $runLog -Encoding UTF8
"=== lightrag package resolves from: $pkgSource" | Add-Content -LiteralPath $runLog -Encoding UTF8

if ($Merged) {
  & $python (Join-Path $PSScriptRoot 'ingest_merged.py') $pdfs[0] --pages $Pages *>> $runLog
} else {
  & $python (Join-Path $PSScriptRoot 'rag_ingest.py') @pdfs *>> $runLog
}
$ec = $LASTEXITCODE

# An ingest can exit 0 while writing NaN / all-zero vectors (corrupt embedding
# model). check_vectors.py turns that silent failure into a non-zero EXITCODE.
if ($ec -eq 0) {
  & $python (Join-Path $PSScriptRoot 'check_vectors.py') *>> $runLog
  if ($LASTEXITCODE -ne 0) { $ec = $LASTEXITCODE }
}

# A z.ai 429 burst (code 1302, the per-minute request rate) can exhaust the sub-second retry
# backoffs and permanently drop a single multimodal item - "ERROR: Error generating <kind>
# description: RetryError[...]" - while the run still finishes EXITCODE=0 with no Traceback and no
# ABORT. That item gets no VLM description, so no entities or relations ever enter the graph from
# it, and NOTHING in the store reveals it afterwards: chunks, vectors and doc status all look
# perfectly healthy. The batch-level "Error in multimodal processing" is deliberately NOT counted -
# it self-heals via "Falling back to individual multimodal processing". Failing the run here is what
# keeps the log (the error-correction pass needs it) and stops a lossy run reporting success.
if ($ec -eq 0) {
  $lost = @(Select-String -LiteralPath $runLog -Pattern 'Error generating .+ description: RetryError' -ErrorAction SilentlyContinue).Count
  if ($lost -gt 0) {
    "PERMANENT_MULTIMODAL_LOSSES=$lost" | Add-Content -LiteralPath $runLog -Encoding UTF8
    "$lost multimodal item(s) permanently dropped to z.ai 429. Run the error-correction pass: lower MAX_ASYNC and the per-role extract/vlm limits in .env, then delete + re-ingest the affected document(s), then re-grep." | Add-Content -LiteralPath $runLog -Encoding UTF8
    $ec = 4
  }
}

"EXITCODE=$ec" | Add-Content -LiteralPath $runLog -Encoding UTF8

. (Join-Path $PSScriptRoot 'notify.ps1')
if ($ec -eq 0) {
  # Archive, never delete: this log is the only record of which items the
  # mandatory post-ingest error-correction pass has to check for permanent
  # losses (see OPERATING.md). It must survive until that pass reports zero.
  if (Test-Path -LiteralPath $runLog) {
    $okStamp = Get-Date -Format 'yyyyMMdd_HHmmss'
    Move-Item -LiteralPath $runLog -Destination (Join-Path $LOG "ingest_run.$okStamp.ok.log") -Force
  }
  Remove-Item -LiteralPath (Join-Path $LOG 'LAST_FAILURE.txt') -Force -ErrorAction SilentlyContinue
  Play-RagSound -Success
} else {
  $stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
  $failLog = Join-Path $LOG "ingest_FAILED_$stamp.log"
  Move-Item -LiteralPath $runLog -Destination $failLog -Force
  $triage = & $python (Join-Path $PSScriptRoot 'ingest_triage.py') --exitcode $ec $failLog @pdfs 2>&1 | Out-String
  "EXITCODE=$ec`nWHEN=$stamp`nLOG=$failLog`n$triage" |
    Set-Content -LiteralPath (Join-Path $LOG 'LAST_FAILURE.txt') -Encoding UTF8
  Play-RagSound -Failure
}

docker compose start
exit $ec
