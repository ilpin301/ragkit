"""Ingest non-text documents into LightRAG via RAG-Anything + MinerU.

Usage:  RAGBASE_ROOT=<base> python rag_ingest.py <file1> [file2 ...]
Normally invoked through ragkit/ingest.ps1, which sets the environment.
IMPORTANT: stop the Docker LightRAG container before running (shared storage),
then start it again after:  docker compose stop / docker compose start
"""
import asyncio
import hashlib
import os
import sys
import time

from raganything import RAGAnything, RAGAnythingConfig
from lightrag.llm.openai import openai_complete_if_cache
from lightrag.llm.ollama import ollama_embed
from lightrag.utils import EmbeddingFunc

import ragbase

# Workaround: raganything 1.3.1 builds modal-processor config via plain asdict(),
# which misses runtime keys ('role_llm_funcs' etc.) that lightrag-hku 1.5.4 injects
# in _build_global_config(). Redirect the module-level asdict name it uses.
import raganything.modalprocessors as _mp
from dataclasses import asdict as _std_asdict

def _full_config_asdict(obj):
    if hasattr(obj, "_build_global_config"):
        return obj._build_global_config()
    return _std_asdict(obj)

_mp.asdict = _full_config_asdict

# Workaround #2: raganything's batch multimodal path (processor.py) passes
# lightrag.__dict__ as global_config, which misses the 'role_llm_funcs'
# property. Mirror the property into the instance __dict__ after each rebuild
# so dict-based lookups find it (attribute access still hits the property).
from lightrag import LightRAG as _LightRAG

_orig_rebuild = _LightRAG._rebuild_role_llm_funcs

def _rebuild_and_mirror(self):
    _orig_rebuild(self)
    self.__dict__["role_llm_funcs"] = self.role_llm_funcs

_LightRAG._rebuild_role_llm_funcs = _rebuild_and_mirror

# Workaround #3: raganything's separate_content() routes ALL non-text types
# (image, table, equation, page_number, header, footer, etc.) to LLM multimodal
# processing. page_number, header, and footer are structural metadata that waste
# rate-limited API calls and add noise. Filter them before separate_content()
# processes the content_list.
import raganything.utils as _rg_utils
import raganything.processor as _rg_processor

_orig_separate_content = _rg_utils.separate_content

def _separate_content_filtered(content_list):
    """Wrapper that filters junk multimodal types before separating content."""
    # Filter out junk types that should never reach LLM processing
    junk_types = {"page_number", "header", "footer"}
    filtered_list = [
        item for item in content_list
        if item.get("type") not in junk_types
    ]

    if len(filtered_list) < len(content_list):
        removed_count = len(content_list) - len(filtered_list)
        from lightrag.utils import logger as _logger
        _logger.debug(f"Filtered out {removed_count} junk multimodal items (page_number/header/footer)")

    return _orig_separate_content(filtered_list)

_rg_utils.separate_content = _separate_content_filtered
_rg_processor.separate_content = _separate_content_filtered

# Workaround #4: nano-vectordb's save() base64-encodes the entire matrix and
# then builds the whole JSON document as one string before writing - measured
# 2.2 GB of transient heap for a 744 MB file, and MECH_RAG's vdb_relationships
# is 1.19 GB and gets rewritten on EVERY document flush. That burst is the
# largest freshly-touched-bytes surface in the run, i.e. the biggest crash
# window on a box with a RAM fault. Stream the matrix instead: peak heap ~9 MB,
# ~2x faster, and the JSON parses back identically.
import base64 as _b64
import json as _json
import numpy as _np
from nano_vectordb.dbs import NanoVectorDB as _NanoVectorDB

_orig_nvdb_save = _NanoVectorDB.save
_B64_CHUNK = 3 * 1024 * 1024  # multiple of 3 -> no base64 padding mid-stream


def _streamed_nvdb_save(self):
    """save() that never materializes the matrix or the JSON document."""
    storage = self._NanoVectorDB__storage
    matrix = _np.ascontiguousarray(storage["matrix"])
    with open(self.storage_file, "w", encoding="utf-8") as f:
        f.write('{"matrix": "')
        raw = matrix.reshape(-1).view(_np.uint8)
        for i in range(0, raw.nbytes, _B64_CHUNK):
            f.write(_b64.b64encode(raw[i:i + _B64_CHUNK].tobytes()).decode())
        f.write('"')
        for key, value in storage.items():
            if key == "matrix":
                continue
            f.write(", " + _json.dumps(key, ensure_ascii=False) + ": ")
            _json.dump(value, f, ensure_ascii=False)
        f.write("}")


_NanoVectorDB.save = _streamed_nvdb_save

ZAI_KEY = os.environ["ZAI_API_KEY"]
BASE_URL = "https://api.z.ai/api/coding/paas/v4"
LLM_MODEL = "glm-5.3"
VISION_MODEL = "glm-4.5v"
WORKING_DIR = ragbase.STORAGE

_VLM_SEMAPHORE = asyncio.Semaphore(2)

# --- z.ai 429 hardening -------------------------------------------------------
# z.ai enforces a per-MINUTE request rate (429, code 1302; 1305 is the separate
# concurrency cap). The openai client retries with sub-second backoffs (0.4-1.0 s),
# which cannot outlast a per-minute window, so a burst exhausts every attempt and
# lightrag surfaces tenacity's RetryError. raganything then drops that ONE item:
# an equation or image gets no VLM description, so no entities or relations ever
# enter the graph from it, while the run still finishes EXITCODE=0 and the store
# looks perfectly healthy. Observed 2026-08-29: one equation lost this way.
# Retrying on a minute-scale schedule is what actually clears a per-minute limit.
# Both llm_model_func and vision_model_func route through this name, so rebinding
# the module global covers every call site.
_zai_raw_complete = openai_complete_if_cache
_ZAI_BACKOFF = (20, 45, 90, 150)

# --- quota stop guard ------------------------------------------------------
# Running a base dry is worse than stopping short: the document is left `handling`,
# every in-flight multimodal item dies with a RetryError, and the repair costs a
# delete/re-ingest cycle. Stop while there is still headroom and leave the LLM
# response cache on disk so the relaunch replays free.
# Read BOTH TOKENS_LIMIT rows: `number:5` is the 5-hour window, `number:1` is weekly.
# Watching only the one z.ai names in its 1308 message is how the wrong counter gets
# blamed.
_QUOTA_URL = "https://api.z.ai/api/monitor/usage/quota/limit"
_QUOTA_STOP_PCT = float(os.environ.get("ZAI_QUOTA_STOP_PCT", "98"))
_QUOTA_POLL_SEC = float(os.environ.get("ZAI_QUOTA_POLL_SEC", "60"))
_QUOTA_EXIT_CODE = 17
_quota_checked_at = 0.0
_ACTIVE_RAG = None


def _quota_pct():
    """Highest TOKENS_LIMIT percentage in use, or None if it cannot be read.

    Fails OPEN on purpose: a flaky monitor endpoint must never kill a healthy
    ingest. The guard only stops on a number it actually read.
    """
    try:
        import httpx
        # trust_env=False: this box's system proxy is SOCKS, and picking it up
        # raises "Missing dependencies for SOCKS support" inside the run.
        with httpx.Client(trust_env=False, timeout=10) as client:
            r = client.get(_QUOTA_URL, headers={"Authorization": ZAI_KEY})
            rows = r.json()["data"]["limits"]
        pcts = [row["percentage"] for row in rows if row.get("type") == "TOKENS_LIMIT"]
        return max(pcts) if pcts else None
    except Exception as exc:
        print(f"WARNING: quota check failed ({exc}); continuing", flush=True)
        return None


async def _flush_everything():
    """Best-effort persist of every storage that knows how to persist itself."""
    lr = getattr(_ACTIVE_RAG, "lightrag", None)
    if lr is None:
        return
    for store in list(vars(lr).values()):
        # vars(lr) holds resolved storage CLASSES next to the live instances.
        # getattr on a class returns the unbound function, so calling it with no
        # args raised "index_done_callback() missing 1 required positional
        # argument: 'self'" on every quota abort - noise that would hide a real
        # flush failure.
        if isinstance(store, type):
            continue
        cb = getattr(store, "index_done_callback", None)
        if cb is None:
            continue
        try:
            await cb()
        except Exception as exc:
            print(f"--- flush failed for {type(store).__name__}: {exc}", flush=True)


async def _quota_stop(pct):
    print(f"ABORT: z.ai quota at {pct}% (stop threshold {_QUOTA_STOP_PCT}%). "
          f"Flushing to disk and stopping so the relaunch replays from cache.",
          flush=True)
    await _flush_everything()
    print("ABORT: flush complete, exiting", flush=True)
    sys.stdout.flush()
    sys.stderr.flush()
    # ponytail: os._exit because a raise here gets swallowed by raganything's
    # per-item handler and the run would grind on burning the last of the quota.
    os._exit(_QUOTA_EXIT_CODE)


async def _quota_guard():
    global _quota_checked_at
    now = time.monotonic()
    if now - _quota_checked_at < _QUOTA_POLL_SEC:
        return
    _quota_checked_at = now
    pct = _quota_pct()
    if pct is not None and pct >= _QUOTA_STOP_PCT:
        await _quota_stop(pct)


def _is_usage_limit(exc):
    """True for z.ai code 1308 - the window is spent, backing off will not help."""
    return "1308" in str(exc)
# --- end quota stop guard --------------------------------------------------


def _is_rate_limit(exc):
    """True if exc is a 429, including one wrapped in tenacity's RetryError."""
    from openai import RateLimitError
    if isinstance(exc, RateLimitError):
        return True
    inner = getattr(getattr(exc, "last_attempt", None), "exception", None)
    if callable(inner):
        try:
            return isinstance(inner(), RateLimitError)
        except Exception:
            return False
    return False


async def _complete_with_backoff(*args, **kwargs):
    await _quota_guard()
    for delay in _ZAI_BACKOFF:
        try:
            return await _zai_raw_complete(*args, **kwargs)
        except Exception as exc:
            if _is_usage_limit(exc):
                # Window is spent. Sleeping the full backoff just delays the same
                # failure and leaves less time to flush.
                await _quota_stop(100)
            if not _is_rate_limit(exc):
                raise
            print(f"WARNING: z.ai 429, sleeping {delay}s before retry", flush=True)
            await asyncio.sleep(delay)
    return await _zai_raw_complete(*args, **kwargs)


openai_complete_if_cache = _complete_with_backoff
# --- end 429 hardening --------------------------------------------------------


async def llm_model_func(prompt, system_prompt=None, history_messages=[], **kwargs):
    return await openai_complete_if_cache(
        LLM_MODEL,
        prompt,
        system_prompt=system_prompt,
        history_messages=history_messages,
        api_key=ZAI_KEY,
        base_url=BASE_URL,
        **kwargs,
    )


# --- VLM caption cache --------------------------------------------------------
# RAGAnything never caches modal_caption_func calls (raganything/modalprocessors.py
# calls self.modal_caption_func raw at :943 image, :1138 table, :1326 equation,
# :1498 generic), so a quota-killed run that resumes re-buys every image caption.
# Append-only JSONL sidecar: crash-safe without rewriting the file, and it lives
# inside rag_storage so rag_sync.ps1 push carries it and a pull restores it.
# Key is safe to reuse across runs: MinerU image paths are content-addressed and
# the caption prompt embeds the path, so the same image re-parses to the same key.
_CAPTION_CACHE_PATH = os.path.join(WORKING_DIR, "vlm_caption_cache.jsonl")
_caption_cache = None


def _caption_key(prompt, system_prompt, image_data):
    h = hashlib.sha256()
    for part in (VISION_MODEL, system_prompt or "", prompt or "", image_data or ""):
        h.update(part.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def _caption_cache_load():
    global _caption_cache
    if _caption_cache is not None:
        return _caption_cache
    _caption_cache = {}
    try:
        with open(_CAPTION_CACHE_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = _json.loads(line)
                except ValueError:
                    continue  # truncated tail left by a killed run
                _caption_cache[rec["k"]] = rec["v"]
    except FileNotFoundError:
        pass
    print(f"--- vlm caption cache: {len(_caption_cache)} entries", flush=True)
    return _caption_cache


def _caption_cache_put(key, value):
    _caption_cache[key] = value
    with open(_CAPTION_CACHE_PATH, "a", encoding="utf-8") as f:
        f.write(_json.dumps({"k": key, "v": value}, ensure_ascii=False) + "\n")


async def vision_model_func(
    prompt, system_prompt=None, history_messages=[], image_data=None, messages=None, **kwargs
):
    # Only the image_data branch is cached: that is the caption path the modal
    # processors take. The `messages` branch is query-time multimodal enhancement
    # and must stay live.
    if not image_data:
        return await _vision_model_uncached(
            prompt, system_prompt, history_messages, image_data, messages, **kwargs
        )
    cache = _caption_cache_load()
    key = _caption_key(prompt, system_prompt, image_data)
    hit = cache.get(key)
    if hit is not None:
        return hit
    result = await _vision_model_uncached(
        prompt, system_prompt, history_messages, image_data, messages, **kwargs
    )
    # ponytail: no in-flight dedupe and no cross-process lock. Two coroutines
    # racing the same key just pay twice, and ingest_merged's parse subprocesses
    # run one at a time. Add a lock only if concurrent bases ever share a store.
    if isinstance(result, str) and result:
        _caption_cache_put(key, result)
    return result
# --- end VLM caption cache ----------------------------------------------------


async def _vision_model_uncached(
    prompt, system_prompt=None, history_messages=[], image_data=None, messages=None, **kwargs
):
    async with _VLM_SEMAPHORE:
        # Multimodal VLM enhanced query: pre-built messages take priority
        if messages:
            return await openai_complete_if_cache(
                VISION_MODEL,
                "",
                system_prompt=None,
                history_messages=[],
                messages=messages,
                api_key=ZAI_KEY,
                base_url=BASE_URL,
                **kwargs,
            )
        if image_data:
            vision_messages = []
            if system_prompt:
                vision_messages.append({"role": "system", "content": system_prompt})
            vision_messages.append(
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{image_data}"},
                        },
                    ],
                }
            )
            return await openai_complete_if_cache(
                VISION_MODEL,
                "",
                system_prompt=None,
                history_messages=[],
                messages=vision_messages,
                api_key=ZAI_KEY,
                base_url=BASE_URL,
                **kwargs,
            )
        return await llm_model_func(prompt, system_prompt, history_messages, **kwargs)


# NOTE: plain single wrap — repo examples double-wrap openai_embed (known bug)
embedding_func = EmbeddingFunc(
    embedding_dim=int(ragbase.require_env("EMBEDDING_DIM")),
    max_token_size=8192,
    # model_name is NOT cosmetic: base.py::_generate_collection_suffix turns it
    # into the vector-DB collection suffix. Omit it and this process writes to
    # lightrag_vdb_entities while the API server reads
    # lightrag_vdb_entities_bge_m3_1024d -- two stores, no error, no results.
    model_name=ragbase.require_env("EMBEDDING_MODEL"),
    func=lambda texts: ollama_embed(
        texts, embed_model="bge-m3", host="http://localhost:11434"
    ),
)


async def periodic_cache_flush(rag, every=300):
    """Flush the LLM response cache to disk every `every` seconds.

    LightRAG only persists kv_store_llm_response_cache.json at the end of the
    pipeline, so a crash mid-run loses every extraction since launch. Bounds the
    loss to `every` seconds instead.
    """
    # ponytail: flushes only the LLM response cache, not doc_status/text_chunks;
    # widen if a crash is ever seen to lose more than re-runnable extraction work
    while True:
        await asyncio.sleep(every)
        lr = getattr(rag, "lightrag", None)
        if lr is None:
            continue
        try:
            await lr.llm_response_cache.index_done_callback()
            print("--- llm cache flushed to disk", flush=True)
        except Exception as e:
            print(f"--- llm cache flush failed: {e}", flush=True)


def _register_rag(rag):
    """Let the quota guard reach the live storages when it has to flush."""
    global _ACTIVE_RAG
    _ACTIVE_RAG = rag
    return rag


def build_rag(config, **lightrag_kwargs_overrides):
    # LightRAG's core dataclass hardcodes vector_storage="NanoVectorDBStorage";
    # LIGHTRAG_VECTOR_STORAGE is read by the API server ONLY. So the backend has
    # to be passed explicitly here or the host ingest silently keeps writing
    # nano JSON while the server serves from Qdrant.
    lightrag_kwargs = dict(lightrag_kwargs_overrides)
    vector_storage = ragbase.env_value("LIGHTRAG_VECTOR_STORAGE")
    if vector_storage:
        lightrag_kwargs.setdefault("vector_storage", vector_storage)
        print(f"--- vector storage: {vector_storage}", flush=True)

    return RAGAnything(
        config=config,
        llm_model_func=llm_model_func,
        vision_model_func=vision_model_func,
        embedding_func=embedding_func,
        lightrag_kwargs=lightrag_kwargs,
    )


async def main(paths):
    config = RAGAnythingConfig(
        working_dir=WORKING_DIR,
        parser="mineru",
        parse_method="auto",
        enable_image_processing=True,
        enable_table_processing=True,
        enable_equation_processing=True,
    )
    rag = _register_rag(build_rag(config))
    pct = _quota_pct()
    if pct is not None and pct >= _QUOTA_STOP_PCT:
        print(f"ABORT: z.ai quota already at {pct}% before start "
              f"(threshold {_QUOTA_STOP_PCT}%); not registering any document.",
              flush=True)
        sys.exit(_QUOTA_EXIT_CODE)
    flusher = asyncio.create_task(periodic_cache_flush(rag))
    try:
        for path in paths:
            print(f"--- ingesting {path}")
            await rag.process_document_complete(
                file_path=path,
                output_dir=os.path.join(os.path.dirname(WORKING_DIR), "mineru_output"),
                parse_method="auto",
            )
            print(f"--- done {path}")
    finally:
        flusher.cancel()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    asyncio.run(main(sys.argv[1:]))
