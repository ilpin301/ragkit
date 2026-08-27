# Creating a LightRAG base

Base-independent. Nothing here is specific to any one base — per-base facts live in that base's
`lightrag\.env` and in its own project memory.

## What a base is

A folder containing:

- `lightrag\` — a clone of hkuds/lightrag with its `docker-compose.yml`
- `lightrag\.env` — the only place base-specific config lives (gitignored, holds keys)
- `IN\` — incoming source documents
- `lightrag\INGESTED_SOURCES.txt` — the ingest ledger
- `LOG\` — created by the launcher

No base holds ingest scripts or skills. All tooling lives in ragkit (`%RAGKIT_HOME%`), one copy
for every base on the machine.

## Machine prerequisites (once per machine)

1. **Docker Desktop.** Usually not running after boot — check `docker info` first and allow 1–2 min.
2. **Ollama** with the embedding model pulled (`ollama pull bge-m3`), and `OLLAMA_HOST=0.0.0.0:11434`
   set user-level so containers reach it via `host.docker.internal`. Also usually down after boot.
3. **Python 3.13.x.**
4. **ragkit**: `git clone`, run `.\bootstrap.ps1`, set `RAGKIT_HOME`, then restart every shell and
   every Claude session. Bootstrap builds or reuses the venv, decides CUDA via
   `torch.cuda.is_available()`, writes `machine.ps1`, installs the six skills, seeds the tiktoken
   cache and checks Docker.
5. GPU is optional — MinerU uses CUDA automatically when torch sees it.

## The short way

```powershell
& $env:RAGKIT_HOME
ew_base.ps1 -Name MY_RAG -Start
```

Picks a free port, generates the server key and `COMPOSE_PROJECT_NAME`, writes `.env` from
`template\env.template` (or from `-From <existing base>`), lays out `IN\`, `lightrag\data\`,
`LOG\` and the ledger, and starts the container. The rest of this file is what it does, for the
cases it does not fit.

## Steps (manual)

1. **Create `<BASE>\lightrag`.** Copy `template\docker-compose.yml` into it. A full clone of
   hkuds/lightrag is only needed if the base must vendor its own `lightrag\lightrag\` package —
   the shipped compose pulls `ghcr.io/hkuds/lightrag:latest`.

2. **Write `.env`** — copy `template\env.template` or an existing base's file and change exactly
   three values:
   - `PORT` — any free port.
   - `LIGHTRAG_API_KEY` — a fresh random hex value, never reused between bases.
   - `COMPOSE_PROJECT_NAME` — unique per base.

   Leave every binding identical (LLM host and model, vision model, `EMBEDDING_MODEL`,
   `EMBEDDING_DIM`).

   - `COMPOSE_PROJECT_NAME` is **mandatory, not cosmetic**: every base's compose directory is named
     `lightrag`, so without it docker compose derives the same project name for all of them and
     `up -d` recreates another base's container.
   - Edit `.env` **append-only**. Never rewrite the file and never touch an existing key unless
     explicitly asked to change it. The server auth key and the LLM provider key are different
     values and must not be conflated.
   - `EMBEDDING_MODEL` and `EMBEDDING_DIM` can never change after the first ingest — existing
     vectors become incompatible.

3. **No compose edit needed.** The shipped compose maps the host port from `.env`
   (`${PORT:-9621}:9621`).

4. **Start**: `docker compose up -d` from `lightrag\`. UI and API at `http://localhost:<PORT>`,
   authenticated with the `X-API-Key` header.

5. **Create `IN\`** and an empty `lightrag\INGESTED_SOURCES.txt`.

6. **If the base is a git repo**, add the store path to `.gitignore` before the first commit. Never
   commit a store; never `git add -A`.

7. **Smoke test**: POST a short text to `/documents/text`, poll
   `GET /documents/track_status/<track_id>` (that exact path — `/track_status/<id>` returns 404),
   run one hybrid query, then clear with `DELETE /documents`.

8. **Backup**: `rag_sync.ps1 push` writes `<DRIVE>\<BASE>\rag_storage.tgz`, where `$DRIVE` comes
   from the kit's `machine.ps1`.

## Rotating the server key

Change `LIGHTRAG_API_KEY` in `.env`, then `docker compose up -d --force-recreate`.
`docker restart` reuses the baked-in environment and silently keeps the old key. Never write the
key into a tracked file, a skill, or a doc — read it from `.env` at runtime.

See `OPERATING.md` for how to run and verify an ingest.
