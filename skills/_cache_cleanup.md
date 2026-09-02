
**Delete the LLM response cache.** Only after `EXITCODE=0` AND every verification step has passed
AND the post-ingest error-correction pass reports a permanent-loss count of zero:

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

**Never delete it while a permanent loss is still open.** A z.ai 429 burst (code 1302, the
per-minute request rate; 1305 is the separate concurrency limit) can exhaust the sub-second retry
backoffs and drop a single multimodal item with
`ERROR: Error generating equation description: RetryError[...]`, while the run still reports
`EXITCODE=0` with zero `Traceback` and zero `ABORT`. Check before deleting:

```bash
grep -c '^ERROR' lightrag/LOG/ingest_run.<stamp>.ok.log
grep -n 'RetryError' lightrag/LOG/ingest_run.<stamp>.ok.log
```

Count only PERMANENT losses (`Error generating .* description`, and `Error in multimodal processing`
NOT followed by a `Falling back to individual multimodal processing` that then succeeds) - transient
429s the client retried and won are noise. If any permanent loss is open, the cache is exactly what
makes the corrective re-ingest cheap: succeeded items are cache hits, and a dropped item has no cache
entry so it is genuinely retried. Full procedure in the `il-rag-ingest` and `raganything-ingest`
skills.

**Never delete it on failure, and never before verification.** A killed run's cache is the only thing
that makes the relaunch cheap — that is the whole point of [[project_ingest_cache_flush]]. Deleting
early converts a 15-minute relaunch into a full re-extraction.
