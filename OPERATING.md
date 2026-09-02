# Operating a LightRAG base

Rules that hold for every base. Per-base state — document counts, backlog, corpus quirks — lives in
that base's own project memory, not here. Setup is in `NEW_BASE.md`.

Day to day this is driven through the ragkit skills: `/il-rag-ingest` (the whole loop),
`/il-check-rag-base` (read-only audit), `/lightrag-status`, `/lightrag-query`, `/lightrag-upload`,
`/raganything-ingest`. This file is why they do what they do.

## Before any run

- `docker info` and `curl http://localhost:11434/api/version`. Docker Desktop and Ollama are both
  usually down after boot. A missing embedder does not fail loudly — it writes broken vectors.
- Block system sleep for long runs (`keepawake.ps1`) and **verify** with `powercfg /requests` in an
  elevated shell: it must show `SYSTEM: [PROCESS] ...pwsh.exe`. `None.` means nothing is armed and
  the script can print a success line while blocking nothing.
- Confirm the periodic LLM-cache flush is wired in the ingest scripts. LightRAG persists
  `kv_store_llm_response_cache.json` only at the end of the pipeline, so without the flusher a crash
  loses every extraction since launch.

## Running an ingest

```powershell
& $env:RAGKIT_HOME\ingest.ps1 -Root <base> -ListFile <utf8 list> [-Merged] [-Pages N]
```

Nothing else. `-ListFile` is mandatory and always UTF-8, one path per line — a path passed as a bare
process argument mangles non-ASCII names through the ANSI codepage.

**Route each PDF first.** `page.get_images()` alone is wrong: it counts embedded raster XObjects
only, so vector figures (plots, schematics, TikZ) read as 0 and an image-rich PDF gets sent down the
text path with no figure entities and no VLM equation handling. Probe both signals per page — raster
count, `get_drawings()` count, char count — and route to MinerU/RAG-Anything if either is
non-trivial (>~50 draw ops on a page is a real figure; a handful is rules and underlines).
Otherwise the text path, `/documents/upload`.

**Size ceiling.** MinerU cannot parse a large PDF in one job on this hardware. Past roughly 20 pages
Predict throughput collapses and RSS climbs into swap thrash; raganything swallows MinerU's real
exception, so the log never names OOM and the parse output directory is simply left empty. Slice to
≤10 pages. For a single oversized source prefer `-Merged`: it parses per slice but inserts **one**
document, so there are no lost seams and no slice files to clean up afterwards.

An interrupted run is cheap to re-run — the parse cache and LLM cache mean only unprocessed items
cost API calls. **Never wipe the store to "fix" a run**; that throws away exactly the caches that
make the retry free.

**429 / error code 1305** from the z.ai coding endpoint is a *concurrency* limit, not a rate limit.
Per-minute backoff does not help; capping simultaneous VLM calls (semaphore of 2) does. "openai" in
the log is the python client library used as a compatible transport, not the service.

## Verifying — EXITCODE=0 is not proof

The exit code describes the python process, not the embedding backend. An ingest can exit 0, report
a correct chunk count and a `processed` status while every vector it wrote is NaN — which poisons
similarity for the whole base, with no error anywhere. Check all four:

1. `EXITCODE=0` in the log.
2. `check_vectors.py` exits 0 — rows == matrix rows, 0 non-finite, 0 zero. Its dimension comes from
   `EMBEDDING_DIM` and is never defaulted.
3. The document is listed in `/documents` as `processed`.
4. One real query returns topically correct content with citations.
5. **The mandatory error-correction pass** (see below). `EXITCODE=0` proves neither working
   embeddings nor complete coverage — z.ai's per-minute request-rate limit (code 1302, distinct from
   the concurrency limit 1305) can burst past every retry and drop an item permanently while the run
   still exits clean.

`kv_store_doc_status.json` is authoritative only with the container stopped and the file re-read.
The server does not shut down gracefully, so a normal stop leaves `Exited (137)` — that is the
process ignoring SIGTERM, not evidence of an OOM kill or a damaged store.

`ingest_triage.py <failed log>` classifies a non-zero exit (parse failure, CUDA OOM, host OOM, rate
limit, unreachable endpoint, interrupted).

### The error-correction pass (mandatory, every ingest)

Sources here are often technical content where an equation or figure IS the content — a silently
dropped one is real data loss, not a rounding error. `openai` client retries (0.4-0.98s backoff) are
too short to clear a per-minute rate window, so a burst can exhaust every retry. The failure then
cascades through three stages and only the last one loses data:

1. A request hits 429 (code 1302, rate; or 1305, concurrency) and the client retries — usually wins.
2. A whole chunk's batch multimodal call exhausts its retries (`RetryError`), logs `WARNING: Falling
   back to individual multimodal processing`, and reprocesses that chunk's items one at a time. This
   stage **self-heals** — do not count it as loss.
3. Inside that fallback, a single item can still fail permanently: `ERROR: Error generating equation
   description: RetryError[...]`. That item gets no VLM description — no entities/relations from it
   enter the graph — but the chunk's surrounding text is still chunked and embedded and stays
   retrievable.

Run against the archived log:

```
grep -c '^ERROR' <log>
grep -n 'RetryError' <log>
```

Then separate the three stages instead of treating the `ERROR` count as loss:

- Count only `Error generating .* description` lines, and `Error in multimodal processing` lines
  that are **not** followed by a `Falling back to individual multimodal processing` that itself
  succeeds — those are the permanent losses.
- Everything else (bare 429s that were retried and won, batch failures that fell back and
  succeeded) is noise: expected, self-healed, not a finding.

The pass is done only when this count is zero. On a nonzero count:

1. In that base's `lightrag\.env`, lower `MAX_ASYNC` and the per-role extract/VLM concurrency limits
   **first** — otherwise the re-run reproduces the same burst and drops the same (or a different)
   item again.
2. Delete the affected document(s) (see Deleting documents, below) and re-ingest just those.
3. Re-grep the new log the same way. Repeat until clean.

The LLM response cache makes this loop cheap: everything that already succeeded is a cache hit on
the retry, so only the actually-dropped item does real work against the API.

## After success — and only after

`EXITCODE=0` is not "finished" — an ingest is finished only when the error-correction pass above
reports zero permanent losses. Nothing below (run log, archived log, LLM response cache) may be
deleted before that.

The launcher no longer deletes the run log on success: it archives it in place as
`ingest_run.<yyyyMMdd_HHmmss>.ok.log` in the same `LOG` folder. That archived file is the input to
the correction pass — do not delete it until the pass is clean.

1. Append the finished **source** filename to `lightrag\INGESTED_SOURCES.txt`. The ledger is
   load-bearing: slices ingested under renamed short names make a filename diff of `IN\` against
   `/documents` report already-ingested sources as new, and content hashing cannot help because a
   slice is not byte-identical to its source. Record partial documents with their exact missing
   page ranges.
2. If **every** slice of a source is processed, delete the slice PDFs from `IN\` and keep the source
   — the slices are build artifacts, the source is the asset. If any slice failed, keep them all so
   the failure can be retried without re-slicing.
3. Delete the archived `ingest_run.<stamp>.ok.log` **only after the correction pass reports zero
   permanent losses**. The live `ingest_run.log` is opened in append mode, so a stale `EXITCODE=0`
   line from a previous run makes the next watcher report "done" while the new run is still
   parsing — deleting the archive before a run does not satisfy this; the next run starts its own
   fresh `ingest_run.log`.
4. Kill the background shells started for the run (waiters, tails, keepawake) — before deleting the
   logs, or a live tail holds the handle.
5. With the container stopped, drop `kv_store_llm_response_cache.json` — only once the correction
   pass is clean; it is what makes a correction re-run cheap.
6. Update that base's ingest-state memory in the same turn the run is verified.

On a failed or killed run, keep everything: the logs and shells are the diagnosis.

## Monitoring

One silent background until-loop on the terminal marker the launcher always writes, on both success
and failure:

```
until grep -q '^EXITCODE=' "<log>"; do sleep 15; done; grep '^EXITCODE=' "<log>"
```

Never a progress watcher that streams events — every event forces a reply turn, and narrowing the
filter does not fix it.

## Deleting documents

`DELETE /documents/{id}` does not exist and answers 404. Use `DELETE /documents/delete_document`
with `{"doc_ids":[...],"delete_file":false,"delete_llm_cache":false}` plus the API-key header. It is
**async**: budget ~6 minutes, and poll until the doc is gone and `/health` reports
`pipeline_busy=false` and `pipeline_destructive_busy=false`. Confirm by the graph node and edge
counts dropping, not by the response body.

It accepts ids that `GET /documents` will not show you — that listing omits `handling` rows, so read
the id from `kv_store_doc_status.json` with the container stopped. Re-ingesting reuses the same doc
id, which is hashed from the file path; that is expected, not a duplicate. Always delete the old
copies **before** re-ingesting the same pages, or the entities double.

Full wipe: `DELETE /documents`. Deleting `rag_storage` from the filesystem may be blocked by policy.

Check `/openapi.json` before guessing any route shape.

## Backup and recovery

- `rag_sync.ps1 push|pull` moves the whole store to and from `<DRIVE>\<BASE>\rag_storage.tgz`.
  `pull` is a full overwrite, not a merge.
- Run it from a **native PowerShell**, never a `pwsh` launched out of Git Bash: that inherits
  `/usr/bin` on PATH, so `tar` resolves to the msys build, which parses `C:\...` as `host:path` and
  tries to open an SSH connection. The same applies to any command handing a Windows absolute path
  to a tool with a GNU twin.
- **Verify every push**: `tar -tzvf <tgz>` must exit 0. Listing a gzip stream decompresses it and
  checks the CRC, so exit 0 means the whole archive is intact — a truncated archive with a complete
  table of contents is only caught this way. A push that overlaps a live ingest truncates silently.
- Keep `.corrupt` and `.bak` copies out of `data\rag_storage\` — anything sitting there is tarred
  into every snapshot.
- Google Drive may still be uploading after the local file looks complete.
- After a BSOD mid-ingest, the graphml and `vdb_entities.json` can be 100% NUL bytes while every
  other store file parses fine — NTFS allocated the size and never flushed the data, and the
  container then crash-loops on a `ParseError` at line 1, column 0. Detect with a NUL-byte scan of
  those two files, not a JSON parse of the directory. Recover by extracting the snapshot into a
  *staging* directory, validating it there (graphml parses, rows == matrix rows, 0 orphans in both
  directions), and only then swapping directories.

## Embeddings gone bad

`norm == 0` on a returned vector is the tell. A corrupt Ollama model blob keeps answering HTTP 200
with a correctly shaped all-zero vector, and `ollama pull` does not fix it — it trusts existing blobs
by filename and only re-downloads missing layers. Hash the blob and compare with its own filename;
on a mismatch, `ollama rm` then pull. Any ingest run during the corrupt window wrote NaN vectors
that have to be re-embedded from each record's stored content.

After repairing vectors, purge the poisoned answers: in `kv_store_llm_response_cache.json` delete
only the mode-prefixed keys (`hybrid:`, `local:`, `global:`, `naive:`, `mix:`) and keep every
`default:` key — those are the entity extractions, worth hours of LLM calls, while a keyword
extraction costs one cheap call to regenerate. Re-test the **exact** query string that failed; a
paraphrase misses the cache and hides the problem.

## Repo hygiene

Track the tooling, never a base's store — the stores are derived data, they are enormous, and they
already have a real backup on Drive. Add the store path to `.gitignore` before the first commit and
never use `git add -A`; these repos carry large untracked backlogs of sources and junk. Before
deleting anything under `lightrag\data\`, run `git status --porcelain | grep -v '^??'` or
`git check-ignore -v` on a sample file — whether parse output is disposable depends on that base's
`.gitignore`. On a `FOUND\` → `IN\` move, stage both halves so git records a rename rather than a
deletion.
