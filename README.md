# ragkit

Portable RAG tooling for LightRAG bases. One copy of the scripts, any number of
bases, any number of machines.

A **base** is any folder containing `lightrag\` with a `.env` and a
docker-compose — e.g. `PCM_RAG`, `MECH_RAG`. Nothing about a specific base is
baked into the kit: root, port, container name, API key and embedding dim are
all discovered at runtime.

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

`ingest.ps1` owns the whole run: guards, `docker compose stop`, the environment,
the entrypoint, the `check_vectors.py` gate, the log, the sound, and
`docker compose start`. It runs every guard **before** stopping the container,
so a rejected invocation can never leave a base's server down.

## Scripts

| File | What |
|---|---|
| `ingest.ps1` | the only entrypoint; universal launcher |
| `notify.ps1` | `Play-RagSound -Success\|-Failure`; fired once by `ingest.ps1` |
| `bootstrap.ps1` | run once per machine; writes `machine.ps1`, installs the skills |
| `ragbase.py` | resolves `RAGBASE_ROOT` into root/storage/data + reads `<root>\lightrag\.env` |
| `rag_ingest.py` | RAG-Anything + MinerU ingest; carries the three monkey-patches and the periodic LLM-cache flush |
| `ingest_merged.py` | slices a large PDF, parses each slice, inserts **one** merged document |
| `check_vectors.py` | post-ingest NaN/zero-vector gate; dim read from `EMBEDDING_DIM`, never defaulted |
| `ingest_triage.py` | classifies why a failed run failed |
| `repairs\repair_vdb.py` | re-embeds graph nodes/edges and chunks with no vector |
| `repairs\cleanup_processed_slices.ps1` | deletes slice PDFs whose whole family is processed |

The python scripts take their base from the `RAGBASE_ROOT` environment
variable, which `ingest.ps1 -Root` sets. They refuse to run without it rather
than guess a path.

`repairs\resync_chunks_count.py` was **deliberately not adopted**: it repairs a
`chunks_count` field nothing reads at query time, so the drift is cosmetic. It
stays in PCM_RAG. `cleanup_leftovers.ps1`, `delete_dup_stub.ps1` and
`refresh_drive_snapshot.ps1` are one-off PCM incident scripts and stayed there too.

## Adding a machine

Exactly two facts are per-machine. Everything else derives.

1. `git clone` this kit.
2. `.\bootstrap.ps1` — verifies Python 3.13.x, creates or reuses a venv,
   installs `requirements.txt`, decides CUDA via `torch.cuda.is_available()`
   (not `nvidia-smi`), writes **`machine.ps1`** (`$VENV`, `$HasCUDA`), installs
   the skills into `~\.claude\skills`, seeds the tiktoken cache, and checks
   Docker and the wav files.
   Use `-Venv <path>` to reuse an existing venv, `-Dev` to junction the skills
   instead of copying them.
3. Set **`RAGKIT_HOME`** to the clone, then restart every shell and every
   running Claude session.
4. Copy each base's `lightrag\.env` by hand. Those files hold API keys, are
   gitignored, and deliberately never enter this repo.

`machine.ps1` is gitignored: a venv cannot be copied between machines, because
`pyvenv.cfg` and the console-script shims embed absolute interpreter paths.

## Adding a base

Nothing to register in the kit. The base needs:

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
