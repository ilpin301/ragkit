# ragkit

Portable RAG tooling for LightRAG bases. One copy of the scripts, any number of
bases, any number of machines.

A **base** is any folder containing `lightrag\` with a `.env` and a
docker-compose — e.g. `PCM_RAG`, `MECH_RAG`. Nothing about a specific base is
baked into the kit: root, port, container name, API key and embedding dim are
all discovered at runtime.

## Docs

| File | What |
|---|---|
| `QUICKSTART.md` | **start here** — zip to working base in three commands |
| `NEW_BASE.md` | machine prerequisites and the manual base layout |
| `OPERATING.md` | how to run, verify and recover an ingest — the reasoning behind the skills |

Both are base-independent; per-base state belongs in that base's own notes.

## Running an ingest

```powershell
& $env:RAGKIT_HOME\ingest.ps1 -Root <base dir> -ListFile <utf8 list> [-Merged] [-Pages 10]
```

Detached (how the skills launch it):

```powershell
Start-Process pwsh -ArgumentList '-NoProfile','-File',"$env:RAGKIT_HOME\ingest.ps1",
  '-Root','<base>','-ListFile','<list>' -WindowStyle Hidden
```

`-ListFile` is mandatory and always UTF-8. Paths are never passed as bare
process arguments — German filenames (`Stäben`, `Verzerrungszustand`) mangle
through the ANSI codepage. `-Merged` ingests one source PDF as a single merged
document and takes exactly one list entry.

`ingest.ps1` owns the whole run: guards, `docker compose stop lightrag`, the environment,
the entrypoint, the `check_vectors.py` gate, the log, the sound, and
`docker compose start lightrag`. It runs every guard **before** stopping the container,
so a rejected invocation can never leave a base's server down. Only the `lightrag` service is
stopped — a base's `qdrant` is a server the host ingest talks to, so stopping the whole compose
project would break the ingest the stop exists to protect.

## Scripts

| File | What |
|---|---|
| `new_base.ps1` | creates a base: port, keys, layout, `.env`, compose, optional start |
| `pack.ps1` | zips the kit for another machine (no venv, no git, no keys) |
| `bootstrap.ps1` | run once per machine; writes `machine.ps1`, installs the skills |
| `ingest.ps1` | the only entrypoint; universal launcher |
| `notify.ps1` | `Play-RagSound -Success\|-Failure`; fired once by `ingest.ps1` |
| `ragbase.py` | resolves `RAGBASE_ROOT` into root/storage/data + reads `<root>\lightrag\.env` |
| `rag_ingest.py` | RAG-Anything + MinerU ingest; carries the three monkey-patches and the periodic LLM-cache flush |
| `ingest_merged.py` | slices a large PDF, parses each slice, inserts **one** merged document |
| `check_vectors.py` | post-ingest NaN/zero-vector gate, backend-aware (nano's `vdb_*.json` or the Qdrant collections); dim read from `EMBEDDING_DIM`, never defaulted |
| `ingest_triage.py` | classifies why a failed run failed |
| `check_ingest_wiring.py` | asserts host and server agree on vector backend and collection naming; exit 0 agree, 3 do not |
| `migrate_nano_to_qdrant.py` | copies stored vectors from nano to Qdrant verbatim, smallest store first; dry run by default, `--apply` to write |
| `verify_qdrant_recall.py` | recall@k against exact cosine from the nano matrix; run before deleting `vdb_*.json` |
| `bench_vector_search.py` | times Qdrant vs. nano's brute-force scan on the same vectors |
| `purge_query_cache.py` | drops cached query answers from the LLM response cache, keeps `default:*` and `<mode>:keywords`; dry run by default |
| `repairs\repair_vdb.py` | re-embeds graph nodes/edges and chunks with no vector — nano only |
| `repairs\cleanup_processed_slices.ps1` | deletes slice PDFs whose whole family is processed |

The python scripts take their base from the `RAGBASE_ROOT` environment
variable, which `ingest.ps1 -Root` sets. They refuse to run without it rather
than guess a path.

`repairs\resync_chunks_count.py` was **deliberately not adopted**: it repairs a
`chunks_count` field nothing reads at query time, so the drift is cosmetic. It
stays in PCM_RAG. `cleanup_leftovers.ps1`, `delete_dup_stub.ps1` and
`refresh_drive_snapshot.ps1` are one-off PCM incident scripts and stayed there too.

## Vector backend

`LIGHTRAG_VECTOR_STORAGE` in a base's `lightrag\.env` selects where vectors live:

| Value | Where vectors live |
|---|---|
| `NanoVectorDBStorage` | `vdb_chunks.json`, `vdb_entities.json`, `vdb_relationships.json` in `rag_storage` |
| `QdrantVectorDBStorage` | a per-base `qdrant` compose service, storage in the docker named volume `<project>_qdrant_storage` |

Why Qdrant: nano rewrites all three `vdb_*.json` files on every document flush — roughly 1536 MB
per document on a base PCM_RAG's size — while Qdrant writes only the rows that changed. On a box
with an unfixed RAM fault, the time spent writing is the window in which a crash destroys the
store. Measured on PCM_RAG (126,750 vectors): recall@10 vs. exact cosine 1.0000/0.9980/0.9970,
retrieval identical to the nano baseline on a 10-question set, search 17.4 ms -> 8.5 ms on the
93,852-row store.

`new_base.ps1` writes `LIGHTRAG_VECTOR_STORAGE=NanoVectorDBStorage` for a new base and derives
`QDRANT_PORT = 6333 + (PORT - 9621)` plus `QDRANT_URL`, applied on both the template path and
`-From <existing base>` — a base seeded from a Qdrant base would otherwise inherit its
`QDRANT_PORT` and the two containers would fight over one host port.

An empty, new base can be flipped to `QdrantVectorDBStorage` in `.env` before its first ingest,
nothing else to do. An existing base must run `migrate_nano_to_qdrant.py` first — flipping the
value alone points the server at an empty index and queries return `[no-context]`. See
`OPERATING.md`.

Host-side URLs always use `127.0.0.1`, never `localhost`: Windows resolves `localhost` to `::1`
first and docker publishes on `127.0.0.1` only, so the failed IPv6 attempt costs about 2 s per
request (measured 2055 ms -> 8.2 ms per search).

## Adding a machine

Three facts are per-machine. Everything else derives.

1. `git clone` this kit, or unzip the archive `pack.ps1` produced.
2. `.\bootstrap.ps1` — verifies Python 3.13.x, creates or reuses a venv,
   installs `requirements.txt`, decides CUDA via `torch.cuda.is_available()`
   (not `nvidia-smi`), writes **`machine.ps1`** (`$VENV`, `$HasCUDA`, `$DRIVE`), installs
   the skills into `~\.claude\skills`, seeds the tiktoken cache, and checks
   Docker and the wav files.
   Use `-Venv <path>` to reuse an existing venv, `-Drive <path>` to set the
   Drive snapshot root explicitly (otherwise the mounted drive letters are
   probed), `-Dev` to junction the skills instead of copying them.
3. Set **`RAGKIT_HOME`** to the clone, then restart every shell and every
   running Claude session.
4. Copy each base's `lightrag\.env` by hand. Those files hold API keys, are
   gitignored, and deliberately never enter this repo.

`machine.ps1` is gitignored: a venv cannot be copied between machines, because
`pyvenv.cfg` and the console-script shims embed absolute interpreter paths.

## Adding a base

```powershell
.
ew_base.ps1 -Name MY_RAG -Start
```

Nothing to register in the kit. `new_base.ps1` produces exactly what follows;
build it by hand only if the generated layout does not fit. The base needs:

- `lightrag\.env` with `PORT`, `EMBEDDING_DIM`, `COMPOSE_PROJECT_NAME`, and `ZAI_API_KEY` or
  `LLM_BINDING_API_KEY` (`ZAI_API_KEY` wins when both are present). `COMPOSE_PROJECT_NAME` is
  not optional: without it docker compose derives the project from the folder name, which is
  `lightrag` for every base, and commands land on whichever base was started last.
- a docker-compose in `lightrag\`
- `IN\` for incoming PDFs and `lightrag\INGESTED_SOURCES.txt` as the ledger

`LOG\` is created by the launcher. If the base vendors its own
`lightrag\lightrag\` package, `ingest.ps1` pins `PYTHONPATH` to it so today's
import resolution is preserved exactly; if it does not, the venv's
`lightrag_hku` is used. Either way the launcher logs which one won.

## Pinning

`requirements.txt` is a `pip freeze` from the proven venv. Do not unpin: the
three monkey-patches in `rag_ingest.py` hook private call paths
(`_rebuild_role_llm_funcs`, the `modalprocessors` `asdict` redirect,
`separate_content`) that a minor version bump can move silently.
