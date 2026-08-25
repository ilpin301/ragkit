---
name: il-check-rag-base
description: Parallel read-only integrity audit of a LightRAG store - orphaned vectors, stuck docs, page-coverage gaps, chunk consistency, Drive-snapshot cross-check, leftover slices. Produces one ranked report plus dry-run repair scripts, executes nothing. Use when the user says "check the rag base", "audit the rag", "/il-check-rag-base", after a crash/BSOD, or after a killed ingest.
---

# il-check-rag-base

Read-only. **This skill never repairs anything.** It fans six independent invariant checks out to
subagents, reconciles their findings into one ranked report, and writes idempotent repair scripts with
a dry-run mode for the user to approve separately.

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

Two more values this skill needs, also derived:

```powershell
$BaseName = Split-Path $Root -Leaf
$Snapshot = "J:\My Drive\RAG\$BaseName\rag_storage.tgz"   # J: is the streaming Drive mount, not G:/H:
```

## Step 0 — preflight (READ THIS, two views of the truth disagree)

Confirm no ingest is in flight: no `LOG\ingest_run.log`, no `rag_ingest.py` python process. If one is
running, STOP and tell the user — the JSON stores are mid-write and every finding would be noise.

**Neither the API nor the JSON files alone tell the truth. Both have been wrong here:**

| view | blind spot |
|---|---|
| `GET /documents` | **omits `handling` rows entirely.** It once showed 66 clean docs while one was stuck half-ingested. A doc can be broken and simply not appear. |
| `kv_store_*.json` read while the container is UP | **stale.** LightRAG holds doc_status in memory and flushes on shutdown, so the file shows the pre-flush state. |

So, before auditing:

```powershell
docker compose --project-directory $LrDir stop   # forces the flush; the JSON is only authoritative once stopped
```

Audit the stopped store, and treat any doc count that differs from `/documents` as a finding, not
noise (the difference IS the hidden `handling` row). Restart with `docker compose start` when done. If
the user will not allow stopping the container, say so in the report and mark invariants B, D and E
`severity:info — unverified (stale store)`; do not present their numbers as fact.

## Step 1 — dispatch the auditors in parallel

Spawn six subagents in ONE message (see `superpowers:dispatching-parallel-agents`). Each owns exactly
one invariant, is told **read-only, propose nothing, fix nothing**, and returns structured JSON:

```json
{"invariant":"A","severity":"critical|warning|info","findings":[{"what":"...","evidence":"...","count":N}]}
```

- **A — orphaned vectors.** Every id in `vdb_chunks.json` / `vdb_entities.json` /
  `vdb_relationships.json` must trace to a live parent in `kv_store_text_chunks.json` /
  `kv_store_full_entities.json` / `kv_store_full_relations.json`. Report ids with no parent, and the
  reverse (parents with no vector).
  Complementary second probe, also required:

  ```powershell
  $env:NO_PROXY='*'; $env:RAGBASE_ROOT=$Root; & $Py (Join-Path $Kit 'check_vectors.py')
  ```

  Exit `0` = healthy, `3` = problems; one line per store with row count, matrix row count, nonfinite
  count and zero count. The two probes see different damage and neither replaces the other: the
  set-difference above finds records **missing** a vector or **stale** vectors whose parent is gone;
  `check_vectors.py` finds vectors that **exist but are poisoned** (NaN / all-zero) or a matrix
  **misaligned** with the record list. A corrupt embedding model returns all-zero vectors,
  normalizing yields NaN, and one NaN poisons the whole nano-vectordb matrix at load time — retrieval
  then silently returns nothing while every id still traces to a live parent. Run both.
- **B — stuck / broken doc status.** In `kv_store_doc_status.json` (container STOPPED — see Step 0):
  anything in `handling`, `pending`, `processing`, or `failed`; every `dup-*` stub; docs present in
  `doc_status` but absent from `kv_store_full_docs.json` (and vice versa). Also diff the doc count in
  that file against `GET /documents` — a positive difference is a hidden `handling` doc.
  **A `dup-*` stub is never harmless.** It is the shadow of a real doc stuck `handling`: deleting the
  stub alone regenerates it on the next run. Report the underlying doc id, not the stub.
- **C — page-coverage gaps.** For each PDF-derived doc, compare pages actually represented against the
  source PDF page count in `IN\`. Distinguish **benign** blanks (front matter, cover, blank verso,
  pure-image plate with no text) from a **genuine** un-ingested tail (e.g. a slice that never
  finished). Only genuine gaps are `critical`; say explicitly which category each gap is.
- **D — chunk-count consistency.** Chunks per doc vs page count; flag docs whose ratio is a strong
  outlier versus the corpus median (truncated ingest), and docs with zero chunks.
- **E — Drive snapshot cross-check.** Compare the live store against `$Snapshot` — doc list and
  per-doc chunk counts. Report docs present in the backup but missing live (possible data loss) and
  live-only docs (expected: ingested since the snapshot). Extract the tgz to the scratchpad, never
  over the live store. If `J:` is not mounted, return `severity:info` saying the check was skipped —
  do not guess. `J:` only exists while Google Drive File Stream is running.
- **F — filesystem leftovers.** Leftover slice PDFs (`stem-NN-MM.pdf`) in `IN\` whose source is
  already ingested, stale `ingest_FAILED_*.log` / `ingest_CRASHED_*.log` / `LAST_FAILURE.txt`,
  orphaned `data\mineru_output` folders with no matching doc.

**Baselines are per base, not global.** Some stores carry a standing population of graph entities or
edges with no vector, and of `kv_store_text_chunks` entries with no `vdb_chunks` vector, left by past
incidents and since accepted. Read the project memory of THIS base for its recorded baseline before
ranking anything: report the current numbers and whether they MOVED relative to that baseline, and
only rank a large jump as a finding. When a base has no recorded baseline, record the numbers as the
new baseline and rank them `info` — do not assume they are damage, and never carry numbers over from
a different base.

Give each agent the concrete file paths and the venv python from Discovery. Big JSON stores (the vdb
files run to hundreds of MB) must be streamed or read with `ijson` / a targeted `python -c` — never
cat into context.

## Step 2 — reconcile

As coordinator, merge the six JSON payloads into ONE markdown report ranked by severity:

- `critical` — real data loss or an unqueryable store: missing vdb files, docs in the backup but not
  live, genuine un-ingested page ranges, zero-chunk docs.
- `warning` — self-inflicted but harmless-to-queries: stuck `handling` rows, `dup-*` stubs, orphaned
  vectors, outlier chunk ratios.
- `info` — cosmetic: leftover slices, stale logs, skipped checks.

Each row: invariant, what, evidence (file + id/count), severity. Deduplicate findings that two agents
report from different angles (a killed run shows up in B, C and D at once — say so once, note the
corroboration).

## Step 3 — write dry-run repair scripts (do NOT execute)

One idempotent script per genuine issue, written into `lightrag\repairs\` inside the BASE — a repair
aimed at damage in one store is base-specific and does not belong in the kit. Each defaults to
`-DryRun` and prints exactly what it would change. Reuse the recovery moves already documented in the
`raganything-ingest` skill (delete the partial doc via `DELETE /documents/delete_document` with a
`doc_ids` body, re-ingest to regenerate the vdb files) instead of inventing new ones. Direct edits to
`kv_store_doc_status.json` require the container stopped.

Deleting a doc is slow and asynchronous: `delete_document` returns `deletion_started` immediately,
then rewrites the graph and the relationship vdb. Expect ~6 minutes on a large store. Wait on
`/health` giving `pipeline_busy=False` with a silent until-loop, never a chatty poller.

Hand the user the report + the script list and stop. Applying a repair is a separate, explicit go.

**Orphaned entity vectors after a delete are EXPECTED, not damage.** `DELETE /documents/delete_document`
strands entity vectors on every clean delete — observed 278, 275, then 272 across three consecutive
deletes on one base, each exactly the `vdb_entities` minus graph-node gap. Do not rank this as
corruption or crash wreckage when a document was recently deleted; rank it `warning` and say plainly
that a delete happened.

**Do not write a new repair script for it.** The kit already ships `repairs\repair_vdb.py`, which
defaults to a dry run and writes `.bak` files for all three stores before touching anything. Point the
user at it instead:

```powershell
$env:NO_PROXY='*'; $env:RAGBASE_ROOT=$Root
& $Py (Join-Path $Kit 'repairs\repair_vdb.py')            # dry run
& $Py (Join-Path $Kit 'repairs\repair_vdb.py') --apply
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
