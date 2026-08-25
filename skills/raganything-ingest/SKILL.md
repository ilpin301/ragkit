---
name: raganything-ingest
description: Ingest non-text documents (scanned PDFs, images, chart/table-heavy PDFs, office docs) into the LightRAG knowledge graph via RAG-Anything + MinerU. Use when the user wants to add a scanned or image-heavy document to the rag.
---

# RAG-Anything Ingest

Pipeline: MinerU parses the document locally (GPU/CUDA when available) -> text/images split ->
GLM-5.2 extracts entities, GLM-4.5V describes images/tables/charts/equations -> bge-m3 embeds ->
merged into the same LightRAG storage the Docker server uses.

This skill is the mechanics of the MinerU/VLM path. For the whole loop - diffing IN\ against the
graph, routing, slicing, verification, bookkeeping - use `il-rag-ingest`, which drives this one.

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

## Source folder rule

**Ingest files ONLY from the base's `IN\`.** Never ingest from or touch files anywhere else
(`FOUND\`, etc.). If the user points at a file elsewhere, ask them to copy it into `IN\` first.

## Run it — always through the kit launcher

`ingest.ps1` is the only entrypoint. It owns the guards, `docker compose stop`, every environment
variable, the `check_vectors.py` gate, the log, the completion sound and `docker compose start`. Do
not reimplement it and do not run `rag_ingest.py` inline.

```powershell
# 1. write the document paths into a UTF-8 list file, one per line
$List = Join-Path $LrDir 'ingest_list.txt'
Set-Content -LiteralPath $List -Encoding UTF8 -Value @(
  "$Root\IN\document.pdf"
)
# 2. launch detached (survives session close)
Start-Process pwsh -ArgumentList '-NoProfile','-File',(Join-Path $Kit 'ingest.ps1'),
  '-Root',$Root,'-ListFile',$List -WindowStyle Hidden
```

Add `-Merged` (exactly one source PDF) to slice a large document and insert it as **one** merged
document instead of one document per slice; `-Pages N` sets the slice size.

`-ListFile` is mandatory and always UTF-8. Never pass bare path arguments: non-ASCII filenames
(Stäben, Verzerrungszustand) get mangled through the ANSI codepage as process args.

The launcher stops the container itself — do not stop it first, and do not skip the launcher to avoid
the stop. The script and the container write the same storage files; concurrent writes corrupt them.

### Self-terminating watch (bash, for the Monitor tool)

`tail -f` never exits, so a monitor built on it idles until timeout. Use a poll loop that exits on
`EXITCODE`, or when the log is deleted (which the launcher does on success):

```bash
cd lightrag
until [ -f LOG/ingest_run.log ]; do sleep 2; done
n=0
while :; do
  if [ -f LOG/ingest_run.log ]; then
    tail -n +$((n+1)) LOG/ingest_run.log | grep -E "EXITCODE|Traceback|RetryError|FAILED|Killed|OOM|Error:"
    n=$(wc -l < LOG/ingest_run.log)
    grep -q EXITCODE LOG/ingest_run.log && break
  else
    echo "EXITCODE=0 (log deleted on success)"; break
  fi
  sleep 5
done
```

Success = `EXITCODE=0`. On success the launcher deletes the log, plays `Ring10.wav` and restarts the
container; on failure it keeps a timestamped `ingest_FAILED_<stamp>.log`, writes `LAST_FAILURE.txt`
with the triage verdict, and plays `Windows Critical Stop.wav`.

## Expect it to be slow

On a 5 GB GPU (Quadro P2000) a ~200-page slide deck takes roughly **3.5 hours** end to end. The
phases, in order, each visible in the log:

1. MinerU layout/formula prediction (GPU, ~30 min)
2. text-phase entity extraction (`Chunk N of M extracted`)
3. multimodal chunk generation — GLM-4.5V describing every figure/table/equation, 2 at a time
   (`Multimodal chunk generation progress: N/M`)
4. entity extraction over those multimodal chunks
5. merge + summary (`LLMmrg:`) and the VDB write

Do not poll the log every 20 s for hours. Set one long-running watcher armed on `^EXITCODE=` — and on
the serial-fallback line below.

## Environment (the launcher sets all of it)

- `TIKTOKEN_CACHE_DIR` = `%TEMP%\data-gym-cache` — without it tiktoken downloads
  `o200k_base.tiktoken` from the Azure CDN and TIMES OUT on this SOCKS-proxy machine. Fatal:
  `LightRAG initialization failed: HTTPSConnectionPool(host='openaipublic.blob.core.windows.net'...)`.
  **Windows temp cleaning wipes this directory.** If it is gone, re-seed it from the kit
  (`ragkit\seed\data-gym-cache`) or pre-warm it:
  `$env:NO_PROXY='*'; & $Py -c "import tiktoken; tiktoken.get_encoding('o200k_base')"`.
- `NO_PROXY='*'` — bypass the SOCKS system proxy for local and z.ai calls.
- `MINERU_DEVICE_MODE` — `cuda` or `cpu`, from `$HasCUDA` in `machine.ps1`. Decided once by
  `bootstrap.ps1` via `torch.cuda.is_available()`, never re-probed; `nvidia-smi` on PATH is not the
  test, since a driver with a cpu-only torch would select `cuda` and crash at parse time.
- `PYTHONIOENCODING='utf-8'`, `PYTHONINTMAXSTRDIGITS='0'`.
- `ZAI_API_KEY` — the launcher reads it from the base's `.env`, accepting either `ZAI_API_KEY` or
  `LLM_BINDING_API_KEY` (the former wins when both are present) and exporting it under the name
  `rag_ingest.py` expects.
- `RAGBASE_ROOT` — the base. The kit's python refuses to run without it rather than guess a path.
- `PYTHONPATH` — pinned to the base's `lightrag\`, so a base that vendors its own `lightrag\lightrag\`
  keeps resolving to it instead of silently switching to the venv's `lightrag_hku`.

## rag_ingest.py contains critical patches — DO NOT regenerate the script

`rag_ingest.py` carries 3 monkey-patches + a VLM semaphore that are REQUIRED (raganything 1.3.1 +
lightrag-hku 1.5.4 compatibility):

1. `asdict` -> `_build_global_config` redirect in `raganything.modalprocessors`
2. `role_llm_funcs` mirrored into the LightRAG instance `__dict__`
3. junk-content filter wrapping `separate_content` in BOTH `raganything.utils` and
   `raganything.processor` (drops page_number/header/footer — ~38% of multimodal items are junk otherwise)
4. `_VLM_SEMAPHORE = asyncio.Semaphore(2)` — the z.ai coding endpoint has a CONCURRENCY limit
   (error 1305); do not raise it above 2

Any edit must preserve all four. `requirements.txt` is pinned for the same reason — these patches hook
private call paths that a minor version bump can move silently.

## z.ai 429 behavior

`ERROR: OpenAI API Rate Limit Error ... code 1305` = z.ai **concurrency** limit, NOT a per-minute
rate. "OpenAI" is the openai python client used as transport for z.ai, not OpenAI the service.
Occasional 429s are absorbed by retry backoff — normal, ignore. Items logging `RetryError` (retries
exhausted) are SKIPPED, leaving graph gaps; re-run later when z.ai load drops.

## Kill/restart is safe and cheap — mid-run

- Parse cache persists after each call — a re-run skips MinerU entirely (the log shows
  `Parsing (native):` instead of a MinerU invocation).
- The LLM response cache only replays free **if the run was checkpointed** — see below.
- After a successful ingest the response cache is deleted by the cleanup rule, so the NEXT re-run of
  that same document pays full extraction again. Kill/restart is cheap mid-run, not after success.
- Multimodal VLM descriptions may NOT hit cache — expect those to re-run.
- **`vdb_*.json` only exist after a clean `EXITCODE=0` finish.** A killed run can leave them missing
  or stale, and queries then return `[no-context]`. Fix: run to clean completion.
- Killed runs leave `dup-*` FAILED stubs in doc status and can leave real docs stuck in `handling`.
  **Prefer deleting the partial doc over flipping its status.** Flipping `handling` -> `processed`
  marks a half-ingested document complete and its missing chunks never come back.

## Re-ingesting a document already in the RAG

LightRAG rejects a same-filename insert as a duplicate and leaves a `dup-*` FAILED stub instead of
replacing anything. Delete the old doc FIRST, with the server UP:

```powershell
$h = @{ 'X-API-Key' = $Key; 'Content-Type' = 'application/json' }
$body = @{ doc_ids = @('doc-XXXX') } | ConvertTo-Json -Compress
Invoke-RestMethod "$Api/documents/delete_document" -Method Delete -Headers $h -Body $body
```

`delete_document` accepts an id that `GET /documents` does not list — get the id from
`kv_store_doc_status.json` when the doc is invisible. Deletion is asynchronous: it rewrites the graph
and LLM-rebuilds every entity shared with other documents. Expect ~6 minutes on a large store. Wait on
`/health` -> `pipeline_busy=False` with a silent until-loop, never a chatty poller, and confirm the
doc is gone from `/documents` before starting the ingest.

**Never verify a deletion from `docker logs`.** A waiter polling
`docker logs --tail 200 ... | grep 'Deletion completed'` hangs forever if the container restarts,
because the line scrolls out of the tail window. Verify from the store files instead.

## Long runs: checkpointing and the serial-fallback trap

**Checkpointing is mandatory.** LightRAG persists `kv_store_llm_response_cache.json` only at the end
of the pipeline, so without a periodic flush a crash loses every extraction since launch — verified:
61 cache saves logged while the on-disk mtime sat unmoved for 54 minutes. `rag_ingest.py` runs
`periodic_cache_flush(rag, every=300)` as a background task, cancelled in a `finally`, wired into both
`rag_ingest.main()` and `ingest_merged.insert_merged()`. Confirm it is still wired before launching:

```sh
grep -nE 'periodic_cache_flush|flusher' "$RAGKIT_HOME/rag_ingest.py" "$RAGKIT_HOME/ingest_merged.py"
```

Expect 5 hits: the definition, plus a create_task/cancel pair in each of the two entry points. Once
live, the log prints `--- llm cache flushed to disk` every 5 minutes and the cache file's mtime
advances. If it does not, stop and fix that before burning hours of extraction.

**The serial-fallback trap.** A brief internet drop does not just retry. One `APITimeoutError` aborts
the async batch multimodal pass, and RAG-Anything restarts the multimodal phase from item 1,
SERIALLY, without async concurrency. Signature:

```
ERROR: Error in multimodal processing: RetryError[C[111/282]: chunk-...: APITimeoutError]
WARNING: Falling back to individual multimodal processing
INFO: Processing item 1/282: page_footnote content
```

Nothing is lost — the text phase is already committed — but the serial path measured **2.7 min/item**
(~12 h for 282 items). Do not let it grind. Confirm the endpoint is back (HTTP 401 without a key means
reachable):

```sh
curl -s --noproxy '*' -o /dev/null -w '%{http_code}\n' --max-time 15 https://api.z.ai/api/paas/v4/
```

then kill the run, delete the partial `handling` doc, and relaunch to get the batch path back.

**Arm waiters on the fallback line, not only on `EXITCODE=`** — otherwise a waiter sits silently
through the entire 12-hour crawl:

```sh
until grep -qE 'Falling back to individual multimodal processing|EXITCODE=' lightrag/LOG/ingest_run.log; do sleep 60; done
```

Hardening note: `rag_ingest.py` passes no `timeout=` or `max_retries=` to `openai_complete_if_cache`,
so the openai client defaults apply. Raising them would widen the outage a run can absorb; untested.

## Sweep orphaned vectors after any delete

`DELETE /documents/delete_document` strands entity vectors on every clean delete, not just after
crashes — observed 278, 275, then 272 across three consecutive deletes, each exactly the
`vdb_entities` minus graph-node gap. With the container stopped:

```powershell
$env:NO_PROXY='*'; $env:RAGBASE_ROOT=$Root
& $Py (Join-Path $Kit 'repairs\repair_vdb.py')            # dry run: expect TO ADD 0/0/0
& $Py (Join-Path $Kit 'repairs\repair_vdb.py') --apply    # writes .bak for all three stores first
& $Py (Join-Path $Kit 'check_vectors.py')                 # rows must equal graph nodes / edges
```

## Benign warning, do not chase it

`LLM output format error; found 3/4 fields on ENTITY ...` means the model emitted a near-miss tuple
delimiter, so the record split short. `_handle_single_entity_extraction` returns `None` — the record
is dropped whole, nothing partial or corrupt is written. Measured 0.12% (5 of 4043 entities, 6 of
~5000 relations). Only investigate if it climbs past a few percent.

## Notes

- First run downloads MinerU models (~a few GB) — slow once, cached after.
- Ollama must be up before ANY ingest: `curl.exe -s http://localhost:11434/api/version`.
- Verify afterwards with `lightrag-status` (documents should show PROCESSED with a realistic chunk
  count), then test one query.
- Any python that prints extracted PDF text needs `PYTHONIOENCODING=utf-8`; the default cp1251 console
  codec raises `UnicodeEncodeError` on the first umlaut and kills the script mid-report.
- `import fitz` warns it is deprecated — use `import pymupdf` in new snippets; both ship in the venv.
- Foreground `sleep` is blocked by the harness. Wait with an `until`-loop in a `run_in_background`
  shell, which also satisfies the silent-monitoring rule.

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
