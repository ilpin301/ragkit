---
name: lightrag-query
description: Query the local LightRAG knowledge graph (GraphRAG over ingested documents). Use when the user asks a question about their RAG documents, knowledge base, or says "ask the rag", "query lightrag".
---

# LightRAG Query

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
$Project   = Get-EnvValue 'COMPOSE_PROJECT_NAME'
if (-not $Project) { throw "COMPOSE_PROJECT_NAME is missing from $EnvF - without it docker compose derives the project from the folder name ('lightrag' for every base) and resolves another base's container" }
$Container = (docker compose --project-directory $LrDir ps -a --format json | ForEach-Object { $_ | ConvertFrom-Json } | Select-Object -First 1).Name
$Kit       = $env:RAGKIT_HOME
if (-not $Kit) { throw "RAGKIT_HOME is not set - run ragkit\bootstrap.ps1, then restart this session" }
. (Join-Path $Kit 'machine.ps1')                  # $VENV, $HasCUDA
$Py = Join-Path $VENV 'python.exe'
```

Bash snippets in this skill assume the same working directory, so they use paths
relative to the base (`lightrag/data/rag_storage/...`). Never write an absolute
drive path into this skill or into anything it generates.

If `lightrag\` is absent, stop and say so. Do not guess a base.

## Query

Write the JSON body to a temp file first, to avoid quoting issues:

```powershell
$body = '{"query":"USER QUESTION HERE","mode":"hybrid"}'
$tmp = New-TemporaryFile; Set-Content $tmp $body -NoNewline
curl.exe -s "$Api/query" -H "Content-Type: application/json" -H "X-API-Key: $Key" -d "@$tmp"
```

Modes: `hybrid` (default, best), `local` (entity-focused), `global` (relationship/theme-focused),
`naive` (plain vector search), `mix`.

The response JSON has a `response` field with the answer including reference markers. Summarize it
and list the cited sources. If the user asks for raw output, show `response` verbatim.

If the connection is refused: `docker ps` — `$Container` must be Up; if not, `docker compose start`
in the base's `lightrag\`.

If a query returns `[no-context]` while documents show as PROCESSED, the vector stores are missing or
poisoned, not the query. Run the kit's checker:

```powershell
$env:NO_PROXY='*'; $env:RAGBASE_ROOT=$Root; & $Py (Join-Path $Kit 'check_vectors.py')
```

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
