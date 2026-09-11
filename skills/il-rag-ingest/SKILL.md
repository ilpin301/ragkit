---
name: il-rag-ingest
description: Run the full PDF ingest loop for a LightRAG base end to end - diff IN/ against the graph, probe each PDF for images AND vector figures, slice oversized PDFs, launch detached via the ragkit launcher, wait silently, verify, clean up. Use when the user says "ingest new PDFs from IN", "run the ingest", "/il-rag-ingest", or a close variant.
---

# il-rag-ingest

Orchestrator for the whole ingest procedure, for ANY LightRAG base. Reads/uses the other skills
rather than duplicating them: `lightrag-status` (server + doc list), `lightrag-upload` (text-only
path), `raganything-ingest` (MinerU/VLM mechanics, env vars, failure recovery), `lightrag-query`
(final sanity query).

Hard rules also live in the base's `CLAUDE.md` (`## RAG Ingest Rules`) and the global
`## Background Monitoring`. Do not violate them; this skill is their executable form.

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
$VecStore  = (Get-EnvValue 'LIGHTRAG_VECTOR_STORAGE')      # nano or QdrantVectorDBStorage - Step 6 differs
$Api       = "http://localhost:$Port"
$Store     = Join-Path $LrDir 'data\rag_storage'
$Ledger    = Join-Path $LrDir 'INGESTED_SOURCES.txt'
$Project   = Get-EnvValue 'COMPOSE_PROJECT_NAME'
if (-not $Project) { throw "COMPOSE_PROJECT_NAME is missing from $EnvF - without it docker compose derives the project from the folder name ('lightrag' for every base) and resolves another base's container" }
$Container = (docker compose --project-directory $LrDir ps -a --format json lightrag | ForEach-Object { $_ | ConvertFrom-Json } | Select-Object -First 1).Name
# name the service: a Qdrant base has more than one container (lightrag + qdrant)
$Kit       = $env:RAGKIT_HOME
if (-not $Kit) { throw "RAGKIT_HOME is not set - run ragkit\bootstrap.ps1, then restart this session" }
. (Join-Path $Kit 'machine.ps1')                  # $VENV, $HasCUDA
$Py = Join-Path $VENV 'python.exe'
```

Bash snippets in this skill assume the same working directory, so they use paths
relative to the base (`lightrag/data/rag_storage/...`). Never write an absolute
drive path into this skill or into anything it generates.

If `lightrag\` is absent, stop and say so. Do not guess a base.

Derived layout used below: source folder `$Root\IN\` (**ingest ONLY from here**), launcher
`$Kit\ingest.ps1`, log `$LrDir\LOG\ingest_run.log`, storage `$Store`, ledger `$Ledger`, server `$Api`.

## Step 0 — preflight

```powershell
curl.exe -s "$Api/health" -H "X-API-Key: $Key"
curl.exe -s http://localhost:11434/api/version    # Ollama MUST be up before any ingest
Get-ChildItem (Join-Path $LrDir 'LOG\ingest_run.log') -ErrorAction SilentlyContinue
Get-Content (Join-Path $LrDir 'LOG\LAST_FAILURE.txt') -ErrorAction SilentlyContinue
```

On a Qdrant base (`$VecStore` = `QdrantVectorDBStorage`), also confirm the qdrant container is up
before launching — the ingest writes to it:

```powershell
curl.exe -s (Get-EnvValue 'QDRANT_URL')/healthz
```

`ingest.ps1` stops only the `lightrag` service (`docker compose stop lightrag`), not the whole
project, so qdrant is expected to stay running through the launch.

- `ingest_run.log` already present + a live python process => a run is IN FLIGHT. Do not start
  another; attach to it at Step 4 instead.
- `LAST_FAILURE.txt` present => the previous run failed. Read it (it already contains
  `ingest_triage.py` output with a verdict + hint) and report before doing anything new.
- **Never wipe `ingest_run.log` before a run.** On `EXITCODE=0` the launcher now ARCHIVES it as
  `ingest_run.<yyyyMMdd_HHmmss>.ok.log` instead of deleting it — that archived log is the required
  input to the Step 6 error-correction pass and must survive until that pass reports zero permanent
  losses.

`GET /documents` **hides `handling` rows**, so a half-ingested doc is invisible there. Cross-check the
store's own count before trusting a clean bill:

```powershell
$fileCount = ((Get-Content (Join-Path $Store 'kv_store_doc_status.json') -Raw | ConvertFrom-Json).PSObject.Properties | Measure-Object).Count
$apiCount  = (($docs.statuses.PSObject.Properties.Value) | Measure-Object).Count
"file=$fileCount api=$apiCount"   # a positive difference = a hidden 'handling' doc
```

That file is only authoritative with the container STOPPED (LightRAG flushes doc_status on shutdown);
while the server runs it can be stale in the other direction. When the two disagree, stop the
container and re-read before deciding anything.

## Step 1 — diff IN/ against the graph

Two filters, both required. A filename check alone is NOT enough: slices are often ingested under
RENAMED short names (`Electromechanical_Hysteresis_Sb2S3-01-10.pdf` for a source called
`Electromechanical_Hysteresis_in_Phase_Change_Material_Sb2S3.pdf`), so the un-sliced source still
sitting in `IN\` looks new and gets ingested a second time. Content hashing does not help — a slice
is not byte-identical to its source.

```powershell
$docs = (curl.exe -s "$Api/documents" -H "X-API-Key: $Key" | ConvertFrom-Json)
$known = $docs.statuses.PSObject.Properties.Value | ForEach-Object { $_.file_path } | Sort-Object -Unique
# ledger of sources already ingested (possibly under renamed slice names)
$ledger = if (Test-Path -LiteralPath $Ledger) {
  Get-Content -LiteralPath $Ledger | ForEach-Object { ($_ -replace '#.*$','').Trim() } | Where-Object { $_ }
} else { @() }
Get-ChildItem (Join-Path $Root 'IN\*.pdf') |
  Where-Object { $known -notcontains $_.Name -and $ledger -notcontains $_.Name }
```

`$Ledger` (`lightrag\INGESTED_SOURCES.txt`) holds one original source filename per line, `#` comments
ignored. Its comment block also records PARTIAL documents (slice families with a failed or missing
range) — those are deliberately NOT listed as done, and its notes say which page ranges are missing.
If the base has no ledger yet, create one; a base without it will re-ingest renamed slice families.

Report the genuinely new list. Before ingesting anything from it, sanity-check each candidate against
the ledger comments and against `/documents` for a *renamed* slice family covering the same paper —
if you find one, it is not new; add it to the ledger instead of ingesting it.

A file already PROCESSED is not new. A file in `FAILED` or stuck `handling` is a *repair* case, not a
new ingest — say so and stop for the user's call.

### ONE SOURCE PER RUN (mandatory, every base)

If the new list holds more than one file, **do NOT put them all in one list file.** Ingest them
**one at a time**, in a full Step 2 → Step 7 cycle per source: probe, slice if needed, launch,
wait, verify all five, run the error-correction pass, then do the COMPLETE cleanup and bookkeeping
(ledger, slice deletion, archived log, LLM cache, keepawake, background shells, project memory).
Only when that source is finished and verified clean does the next one start.

Report the queue up front (`3 new: A, B, C — ingesting one at a time, A first`), then one summary
per source as it lands.

**Back up to Drive between sources.** The per-source cleanup is not done until `rag_sync.ps1 push`
has run and reported success — that push is what makes the just-ingested document survivable, and it
is far cheaper to redo one source than a whole queue. Only then does the next source launch.

```powershell
# Google Drive must be mounted first - J:\ is a virtual drive, it is absent when Drive is not running
if (-not (Get-Process GoogleDriveFS -ErrorAction SilentlyContinue)) {
  $gdrive = Get-ChildItem 'C:\Program Files\Google\Drive File Stream' -Directory |
            Sort-Object Name | Select-Object -Last 1
  Start-Process (Join-Path $gdrive.FullName 'GoogleDriveFS.exe')
}
# wait for the mount, do not assume it is instant
$deadline = (Get-Date).AddMinutes(3)
while (-not (Test-Path 'J:\My Drive') -and (Get-Date) -lt $deadline) { Start-Sleep -Seconds 5 }
if (-not (Test-Path 'J:\My Drive')) { throw 'Google Drive did not mount - stop the queue and report' }
& (Join-Path $Root 'rag_sync.ps1') push
```

Three traps here:

- **Run it from native PowerShell, never the Bash tool.** Under Git Bash `tar` resolves to the msys
  build, which reads `C:\...` as a remote host and dies with `Cannot connect to C: resolve failed`.
  Git Bash also cannot see the `J:` mount at all, so an empty `ls J:` there is NOT evidence that
  Drive is down — check from PowerShell.
- **A missing `J:` means Drive is not running**, not that the backup is gone. Launch it, wait for the
  mount, then push.
- `rag_sync.ps1 push` stops the LightRAG container for the duration and restarts it if it was
  running; on a Qdrant base it also exports a snapshot per collection into
  `lightrag\data\qdrant_snapshots\` and tars it alongside `rag_storage`. A backup taken any other
  way contains no vectors. Confirm the push printed its archive path before moving on.

If the push FAILS, the queue stops — same as a failed ingest. Report and wait for the user's call.

A source that FAILS, or finishes with a nonzero permanent-loss count, STOPS the queue. Report it and
wait for the user's call — do not move on to the next file and do not silently skip the broken one.

Why: a batched run makes a failure unattributable (which source poisoned the graph?), and it makes
the delete/re-ingest repair far more expensive, since deleting one document from a large store takes
minutes and rewrites the graph. Slices of ONE source stay in ONE run — they are one document; this
rule is about separate SOURCES.

## Step 2 — probe each new PDF (routing)

Full image detection: `get_images()` alone MIS-ROUTES vector-figure PDFs. Always check
`get_drawings()` too.

```powershell
& $Py -c @'
import sys, pymupdf
for p in sys.argv[1:]:
    d = pymupdf.open(p)
    imgs = sum(len(pg.get_images()) for pg in d)
    maxdraw = max((len(pg.get_drawings()) for pg in d), default=0)
    chars = sum(len(pg.get_text()) for pg in d)
    route = "MINERU" if (imgs or maxdraw > 50 or chars/max(d.page_count,1) < 100) else "TEXT"
    print(f"{route} pages={d.page_count} images={imgs} maxdraw={maxdraw} chars/pg={chars//max(d.page_count,1)} {p}")
'@ <pdf paths...>
```

- `MINERU` => this skill's detached path (below).
- `TEXT` => the `lightrag-upload` skill instead; far cheaper. Do not push text-only PDFs through MinerU.

## Step 3 — slice oversized PDFs

MinerU on a 5 GB / 32 GB box fails above roughly 20 pages. Slice anything over **10 pages** into
`<=10`-page parts, named `<stem>-01-07.pdf` style, written into `IN\`.

```powershell
& $Py -c @'
import sys, pymupdf
src = sys.argv[1]; step = 10
d = pymupdf.open(src)
for a in range(0, d.page_count, step):
    b = min(a+step, d.page_count) - 1
    out = pymupdf.open(); out.insert_pdf(d, from_page=a, to_page=b)
    name = src[:-4] + "-%02d-%02d.pdf" % (a+1, b+1)
    out.save(name); print(name)
'@ "$Root\IN\big.pdf"
```

Keep the source PDF. Slices are what gets ingested.

Alternative for a single large source: `ingest.ps1 -Merged -Pages N` slices, parses each slice and
inserts **one** merged document instead of one per slice — no slice files to clean up afterwards, and
no renamed-slice-family problem in the ledger.

## Step 4 — launch detached, then wait silently

Write this run's file list into a UTF-8 list file and hand it to the kit launcher. **One source per
list file** — see ONE SOURCE PER RUN in Step 1; multiple entries are only ever the slices of a single
source. Never edit a launcher script, never pass bare path arguments, never run `rag_ingest.py` inline: the launcher owns
the guards, `docker compose stop`, the env block, the `EXITCODE=` marker, the `check_vectors.py` gate,
the failure triage, the completion sound and `docker compose start`.

```powershell
$List = Join-Path $LrDir 'ingest_list.txt'
Set-Content -LiteralPath $List -Encoding UTF8 -Value @(
  "$Root\IN\slice-01-10.pdf"
)
Start-Process pwsh -ArgumentList '-NoProfile','-File',(Join-Path $Kit 'ingest.ps1'),
  '-Root',$Root,'-ListFile',$List -WindowStyle Hidden
```

Confirm every listed path exists before launching — a list file left over from the previous run
usually points at slices that have since been deleted. The launcher refuses an unresolvable entry, but
catching it here saves a round trip.

Wait with ONE silent one-shot waiter that terminates by itself. Never `tail -f | grep`, never a poller
that emits progress notifications:

```bash
cd lightrag
until [ -f LOG/ingest_run.log ]; do sleep 2; done
while [ -f LOG/ingest_run.log ] && ! grep -q EXITCODE LOG/ingest_run.log; do sleep 20; done
if [ -f LOG/LAST_FAILURE.txt ]; then cat LOG/LAST_FAILURE.txt; else echo "EXITCODE=0"; fi
```

`EXITCODE=0` line + no `LAST_FAILURE.txt` = success (the launcher archives the log to
`ingest_run.<stamp>.ok.log` on `EXITCODE=0`, it no longer deletes it), and the
success wav plays. On a long multimodal run, arm the waiter on the serial-fallback line too — see
`raganything-ingest`.

**Checkpointing is mandatory for any long run.** Confirm `periodic_cache_flush` is still wired before
launching:

```sh
grep -nE 'periodic_cache_flush|flusher' "$RAGKIT_HOME/rag_ingest.py" "$RAGKIT_HOME/ingest_merged.py"
```

Expect 5 hits: the definition, plus a create_task/cancel pair in each of the two entry points. Once
live the log prints `--- llm cache flushed to disk` every 5 minutes and the cache file's mtime
advances.

## Step 5 — on failure: triage once, then escalate

`LAST_FAILURE.txt` already holds the verdict from `ingest_triage.py`. Known verdicts and the single
fix to attempt before escalating to the user:

| verdict | fix to try once |
|---|---|
| `MINERU_PARSE_FAILED` / `CUDA_OOM` / `HOST_OOM` | slice smaller (5-7 pages) and re-run |
| `LLM_RATE_LIMIT` | wait, re-run as-is (resumable, caches replay); do NOT raise the VLM semaphore above 2 |
| `ENDPOINT_UNREACHABLE` | start Ollama / make z.ai reachable, re-run |
| `MULTIMODAL_SERIAL_FALLBACK` | net dropped mid-run — kill and relaunch, do NOT let the serial path grind |
| `INTERRUPTED` | re-run as-is |
| `UNKNOWN` | do NOT guess a fix — report the log tail and stop |

The serial-fallback trap, the `dup-*` stub trap and the delete/re-ingest procedure are all documented
once in `raganything-ingest`. Reuse them; do not invent new recovery moves.

### The dangerous case: `EXITCODE=0` with no work done

If the doc is already registered (typically stuck `handling` from an earlier killed run),
`rag_ingest.py` writes a fresh `dup-*` FAILED stub and exits **0** in under a minute. The launcher
archives the log, the waiter reports success, the success wav plays — and nothing was ingested.
Signature: finished far too fast, graph node/edge counts unchanged, vdb files byte-identical.

Deleting the `dup-*` stub does NOT fix this — the stub is a shadow of the real doc and comes back.
Repair the underlying doc:

```powershell
# the stuck doc will NOT appear in GET /documents - get its id from the store file
$full = (Get-Content (Join-Path $Store 'kv_store_doc_status.json') -Raw | ConvertFrom-Json).PSObject.Properties |
        Where-Object { $_.Value.file_path -eq 'slice.pdf' } | ForEach-Object { $_.Name }
$h = @{ 'X-API-Key' = $Key; 'Content-Type' = 'application/json' }
$body = @{ doc_ids = @($full) } | ConvertTo-Json -Compress
Invoke-RestMethod "$Api/documents/delete_document" -Method Delete -Headers $h -Body $body
```

`delete_document` accepts an id that `/documents` does not list. It is async and takes ~6 minutes on a
large store (it rewrites the graph and the relationship vdb) — wait for `/health` giving
`pipeline_busy=False` with a silent until-loop, verify the doc's chunks are gone from
`kv_store_text_chunks.json` and that the graph counts DROPPED, then re-ingest. Re-ingesting reuses the
same doc id (hashed from the file path), which is expected. See [[project_lightrag_delete_endpoint]].

Also check for the rest of the wreckage a killed run leaves: other docs stuck in `handling`, and
missing vdb files.

### The other dangerous `EXITCODE=0` case: silent multimodal data loss

A run can do real work, exit 0, grow the graph and vdb files — and still have silently dropped one or
more equations/figures/tables. z.ai 429s (rate-limit code 1302 per-minute, concurrency code 1305)
trigger a batch-to-serial fallback that self-heals almost every item, but a single item inside that
fallback can exhaust its retries permanently: `ERROR: Error generating equation description:
RetryError[...]`. No VLM description means no entities/relations from that item — everything else in
the run looks completely healthy. This is why Step 6's error-correction grep is non-optional on every
run, not just ones that "look" like they had trouble.

## Step 6 — verify (non-optional, all five)

**`EXITCODE=0` is not evidence that anything was ingested** — see the no-op cases above. Only the
deltas below prove work happened. Record the baseline BEFORE launching (node and edge counts on the
graphml, plus — on nano — the three vdb file sizes and mtimes, or — on Qdrant (`$VecStore` =
`QdrantVectorDBStorage`) — the per-collection point counts; a `check_vectors.py` run before the launch
is the cheapest way to record those).

1. **Doc count delta** — `/documents` count before vs after; every new file PROCESSED, none
   FAILED/handling.
2. **Graph node delta** — the node count in `graph_chunk_entity_relation.graphml` grew:
   `grep -c '<node ' lightrag/data/rag_storage/graph_chunk_entity_relation.graphml`
3. **Vector sanity** — backend-dependent.

   On nano: `vdb_chunks.json`, `vdb_entities.json` and `vdb_relationships.json` all exist, have
   mtimes AFTER the run start, and GREW. Byte-identical sizes = the run did nothing. Missing or stale
   vdb files mean queries return `[no-context]`; the run did not finish cleanly.

   On Qdrant: there are no vdb_*.json files to check — if any are still present they are stale
   leftovers from before the migration and prove nothing. The equivalent delta is the per-collection
   point count growing.

   Either way, run the checker next — file mtime/size and raw point counts say nothing about
   POISONED vectors:

   ```powershell
   $env:NO_PROXY='*'; $env:RAGBASE_ROOT=$Root; & $Py (Join-Path $Kit 'check_vectors.py')
   ```

   Exit `0` = healthy, `3` = problems. `check_vectors.py` is backend-aware. On nano: one line per
   store with row count, matrix row count, nonfinite count, zero count. A corrupt embedding model
   returns all-zero vectors; normalizing those yields NaN, and NaN poisons the WHOLE nano-vectordb
   matrix at load time — retrieval then silently returns nothing while the ingest still exits 0. It
   also catches data/matrix row misalignment. On Qdrant: each collection is checked for existing,
   green status, and non-empty, a 512-point sample is checked for NaN/all-zero and for the right
   dimension, and the chunks collection point count is cross-checked against
   `kv_store_text_chunks.json` — the only count still on disk, which is what exposes a
   half-finished migration. The qdrant container must be UP for this to run. The dimension comes from
   `EMBEDDING_DIM` in the base's `.env` and is never defaulted. `ingest.ps1` runs this itself on the
   success path and folds a non-zero result into `EXITCODE`, so a poisoned store KEEPS its log and
   plays the failure wav. The manual run above is for ad-hoc checks and for verifying a repair.
   **On a Qdrant base, also run `check_ingest_wiring.py` once after any `.env` or backend change.**
   Two silent failure modes produce a run that looks perfect while the store the server serves from
   never changes: LightRAG's core dataclass hardcodes `vector_storage="NanoVectorDBStorage"` and only
   the API server reads `LIGHTRAG_VECTOR_STORAGE`, so the host ingest can keep writing nano JSON while
   the server serves from Qdrant; and `EmbeddingFunc.model_name` feeds the collection suffix, so the
   host can write `lightrag_vdb_entities` while the server reads `lightrag_vdb_entities_bge_m3_1024d`.
   `check_ingest_wiring.py` asserts both, plus that the resolved collection names exist on the server.
   Exit `0` = host and server agree, `3` = they do not. It embeds nothing, calls no LLM, writes
   nothing.
4. **One targeted query** per ingested doc via `lightrag-query`, asking something only that document
   answers. The answer must cite it.
5. **Error-correction pass (mandatory)** — grep the archived log for VLM/multimodal loss, not just
   for the process exit code. Sources here are technical mechanics textbooks: equations and figures
   ARE the content, so a dropped equation is real data loss, not a cosmetic warning.

   ```sh
   grep -c '^ERROR' lightrag/LOG/ingest_run.<stamp>.ok.log
   grep -n 'RetryError' lightrag/LOG/ingest_run.<stamp>.ok.log
   ```

   Classify every hit before drawing a conclusion — most of them are transient or self-healed, not
   loss:

   - **Transient** — `openai._base_client` retrying with sub-second backoff. Ignore.
   - **Self-healed** — `WARNING: Falling back to individual multimodal processing` after a batch
     `RetryError`: the chunk gets reprocessed item-by-item and normally succeeds. Not a loss by
     itself.
   - **Permanent loss** — a `RetryError` INSIDE the serial fallback for a single item, e.g.
     `ERROR: Error generating equation description: RetryError[...]`. That item got no VLM
     description, so it contributed no entities/relations. This is the only count that matters for
     the gate below.

   Only a `Traceback` or an `ABORT` line, or a nonzero permanent-loss count, fails this check — a
   run can log several ERROR/RetryError lines and still be clean if every one of them resolved via
   the self-heal path. If the permanent-loss count is nonzero: lower `MAX_ASYNC` and the per-role
   extract/vlm concurrency limits in `lightrag\.env` (a rerun at the same concurrency reproduces the
   same burst), delete the affected document, re-ingest it, and re-run this grep — repeat until the
   permanent-loss count is zero. See `raganything-ingest` for the full z.ai 429 cascade and the
   delete/re-ingest mechanics.

Report the five numbers (four counts plus the permanent-loss count). Do not claim success without
them.

**Never verify a deletion from `docker logs`** — the line scrolls out of the tail window and the
waiter hangs forever. Verify from the store files:

```sh
python -c "import json;from collections import Counter;d=json.load(open('lightrag/data/rag_storage/kv_store_doc_status.json',encoding='utf-8'));print(Counter(v.get('status') for v in d.values()),len(d))"
grep -c '<node ' lightrag/data/rag_storage/graph_chunk_entity_relation.graphml
```

Expect the deleted doc absent, zero rows in `handling`, and the node count DROPPED.

**Benign warning, do not chase it.** `LLM output format error; found 3/4 fields on ENTITY ...` means
the model emitted a near-miss tuple delimiter, so the record split short and
`_handle_single_entity_extraction` dropped it whole — nothing partial is written. Measured 0.12%
(5 of 4043 entities). Only investigate above a few percent.

## Step 7 — cleanup and bookkeeping

Only after `EXITCODE=0` **and** Step 6 passing **and** the Step 6.5 error-correction pass reports a
permanent-loss count of zero. `EXITCODE=0` alone never gates cleanup — nothing (the archived log, the
LLM response cache) gets deleted while a permanent loss is still open, because the archived log is
the input the correction pass re-checks after every corrective re-ingest.

- delete the slice PDFs (`stem-NN-MM.pdf`); keep the source PDF. The kit has an idempotent,
  dry-run-by-default helper:
  `pwsh -File (Join-Path $Kit 'repairs\cleanup_processed_slices.ps1') -Root $Root` (add `-Apply`).
- append the SOURCE filename (not the slices) to `$Ledger`; if any slice of it failed, add it as a
  commented PARTIAL entry naming the missing page range instead
- update the base's ingest-state project memory with the new doc count/state — do this without asking
- `docker ps` to confirm the container came back up (the launcher runs `docker compose start`, which
  fails silently if Docker Desktop is down)

Emit ONE end-of-run summary: files ingested, the five verification numbers (including the
permanent-loss count), anything skipped and why.

Then run `rag_sync.ps1 push` (launching Google Drive first if `J:` is not mounted — see ONE SOURCE
PER RUN in Step 1) and confirm it succeeded. Only then return to Step 2 for the next queued source.
Cleanup and Drive sync are not deferrable to the end of the queue — each source is fully closed out
before the next launch.

**Always sweep orphaned vectors after a delete.** `DELETE /documents/delete_document` strands entity
vectors that the graph no longer has, on clean deletes too. The procedure is in `raganything-ingest`.

## Windows gotchas that cost time

- Any python that prints extracted PDF text needs `PYTHONIOENCODING=utf-8`; the default cp1251 console
  codec raises `UnicodeEncodeError` on the first umlaut and kills the script mid-report.
- `import fitz` warns it is deprecated — use `import pymupdf`; both ship in the venv.
- Deleted source PDFs are usually still in git: `git show HEAD:FOUND/name.pdf > scratchpad/name.pdf`
  recovers one for page-count or gap classification without touching the worktree.
- **Upload from PowerShell, never from bash.** A `curl.exe -F "file=@..."` upload run through the Bash
  tool returns an EMPTY response and uploads nothing — bash mangles the Windows path in the `-F`
  argument, and the server never sees a file. An empty response body is the tell; always confirm the
  doc count actually rose before waiting on a pipeline that was never given any work.
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

When the run finishes SUCCESSFULLY — `EXITCODE=0`, Step 6 verification passed, AND the Step 6.5
error-correction pass reports zero permanent losses — do both of these before reporting done:

1. **Kill every shell process started for this run.** Background waiters, `tail -f` tails, monitors, poll loops, and the `keepawake.ps1` process — the user's and yours. Use `TaskStop` on each background task id. Leave nothing running.
2. **Delete the archived log** (`ingest_run.<stamp>.ok.log`) and any scratchpad task-output files for this run.

Order matters: kill the tails BEFORE deleting the log, or a live `tail -f` holds the handle.

NEVER do either of these before the correction pass is clean. The archived log is the input the
correction pass re-checks after every corrective re-ingest, so it must survive until that pass
reports zero permanent losses — not merely until `EXITCODE=0`. On a FAILED or killed run, or a run
still carrying a permanent loss, both the log and the shells must be KEPT for diagnosis.

**Delete the LLM response cache.** Only after `EXITCODE=0` AND every verification step has passed
AND the error-correction pass reports zero permanent losses:

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
