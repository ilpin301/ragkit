---
name: lightrag-upload
description: Upload text documents (txt, md, normal text PDFs) into the local LightRAG knowledge graph. Use when the user says to add/upload/ingest a text document into the rag. NOT for scanned PDFs, images, or chart-heavy docs — use raganything-ingest for those.
---

# LightRAG Upload

## Discovery — run this first, never guess

Nothing about a specific base is baked into this skill. The base is **the current
project directory**, and every value below is derived from it at runtime.

```powershell
$Root = (Get-Location).Path
$LrDir = Join-Path $Root 'lightrag'
if (-not (Test-Path -LiteralPath $LrDir)) { throw "not a RAG base: $Root has no lightrag\ - cd into the base first" }
$EnvF = Join-Path $LrDir '.env'
function Get-EnvValue($k) {
  $m = Get-Content -LiteralPath $EnvF -Encoding UTF8 | Select-String "^$k=" | Select-Object -First 1
  if ($m) { $m.Line.Split('=',2)[1].Trim() }
}
$Port      = Get-EnvValue 'PORT'
$Key       = Get-EnvValue 'LIGHTRAG_API_KEY'      # server auth; never hardcode it
$Dim       = Get-EnvValue 'EMBEDDING_DIM'
$Api       = "http://localhost:$Port"
$Store     = Join-Path $LrDir 'data\rag_storage'
$Ledger    = Join-Path $LrDir 'INGESTED_SOURCES.txt'
$Container = (docker compose --project-directory $LrDir ps -a --format json | ConvertFrom-Json | Select-Object -First 1).Name
$Kit       = $env:RAGKIT_HOME
if (-not $Kit) { throw "RAGKIT_HOME is not set - run ragkit\bootstrap.ps1, then restart this session" }
. (Join-Path $Kit 'machine.ps1')                  # $VENV, $HasCUDA
$Py = Join-Path $VENV 'python.exe'
```

Bash snippets in this skill assume the same working directory, so they use paths
relative to the base (`lightrag/data/rag_storage/...`). Never write an absolute
drive path into this skill or into anything it generates.

If `lightrag\` is absent, stop and say so. Do not guess a base.

## Upload

```powershell
curl.exe -s -X POST "$Api/documents/upload" -H "X-API-Key: $Key" -F "file=@$Root\IN\document.pdf"
```

One curl per file. After upload, processing (chunking, entity extraction via GLM, embedding via
Ollama bge-m3) runs in the background and can take minutes per document. Check progress with the
`lightrag-status` skill. Warn the user that large documents take a while and consume z.ai tokens.

**Upload from PowerShell, never from bash.** The same `curl.exe ... -F "file=@<windows path>"` run
through the Bash tool returns an EMPTY response and uploads nothing — bash mangles the Windows path
in the `-F` argument and the server never sees a file. PowerShell returns
`{"status":"success",...,"track_id":"upload_..."}`. An empty response body is the tell: always confirm
the doc count actually rose before waiting on a pipeline that was never given any work.

Only text-shaped documents belong here. If the PDF has images or vector figures, route it to
`il-rag-ingest` / `raganything-ingest` instead — see the probe in `il-rag-ingest` Step 2.

## Keep the machine awake (mandatory)

Any long-running RAG shell process — ingest, parse, insert, delete, enrich, audit — must run with sleep blocked, or the box suspends mid-run and the job dies.

Start this BEFORE launching the process (`keepawake.ps1` is a per-base script, in the base's `lightrag\`):

```powershell
$ka = Start-Process pwsh -ArgumentList '-NoProfile','-File',(Join-Path $LrDir 'keepawake.ps1') -PassThru -WindowStyle Hidden
```

Verify it registered (needs an elevated shell to read):

```powershell
powercfg /requests | Select-String -Pattern 'SYSTEM:' -Context 0,2
```

Expect `SYSTEM: [PROCESS] ...pwsh.exe`. If it says `None.`, the block is NOT active — do not start the run.

`keepawake.ps1` holds `SetThreadExecutionState(ES_CONTINUOUS|ES_SYSTEM_REQUIRED|ES_AWAYMODE_REQUIRED)` for as long as it lives, so killing the process releases the block automatically — there is no persistent power setting to restore. Display sleep is deliberately still allowed; only system sleep is blocked.

Stop it as part of the cleanup below, once no RAG process is still running:

```powershell
Stop-Process -Id $ka.Id -Force
```

Gotcha: PowerShell parses `0x80000000` as a signed Int32, so building the flags inline throws on the P/Invoke and the keepawake silently does nothing while still looking alive. Use the script, which casts to `[uint32]`.

## Cleanup after success (mandatory)

When the run finishes SUCCESSFULLY — `EXITCODE=0` and the verification steps passed — do both of these before reporting done:

1. **Kill every shell process started for this run.** Background waiters, `tail -f` tails, monitors, poll loops, and the `keepawake.ps1` process — the user's and yours. Use `TaskStop` on each background task id. Leave nothing running.
2. **Delete the logs the run produced.** `lightrag\LOG\ingest_run.log`, `LOG\*.log` for this run, and any scratchpad task-output files.

Order matters: kill the tails BEFORE deleting the logs, or a live `tail -f` holds the handle.

NEVER do either of these before success. While a run is in flight the log is the only evidence of progress, and on a FAILED or killed run both the logs and the shells must be KEPT for diagnosis.

**Delete the LLM response cache.** Only after `EXITCODE=0` AND every verification step has passed:

```powershell
docker stop $Container     # never delete while the server holds its own in-memory copy
Remove-Item (Join-Path $Store 'kv_store_llm_response_cache.json')
```

LightRAG recreates the file empty on the next run. This is deliberate, not housekeeping: the cache
exists so a crashed or interrupted run can be replayed without paying for extraction twice, and once
a run has succeeded and verified there is nothing left to replay. It reached ~69 MB / 35k extraction
entries on one base before the first cleanup.

Accepted cost: re-ingesting that document later pays full LLM extraction again, and the first queries
after cleanup run cold while the query-mode cache refills.

**Never delete it on failure, and never before verification.** A killed run's cache is the only thing
that makes the relaunch cheap — that is the whole point of [[project_ingest_cache_flush]]. Deleting
early converts a 15-minute relaunch into a full re-extraction.
