"""Assert the HOST ingest path resolves to the same vector store the server uses.

Two silent failure modes this exists to catch, both found while planning the
Qdrant migration:

  1. LightRAG's core dataclass hardcodes vector_storage="NanoVectorDBStorage".
     LIGHTRAG_VECTOR_STORAGE is read by the API server ONLY. Miss the explicit
     kwarg and the host ingest keeps writing nano JSON while the server serves
     from Qdrant. No error, no warning.
  2. EmbeddingFunc.model_name feeds the vector-DB collection suffix. Omit it
     and the host writes lightrag_vdb_entities while the server reads
     lightrag_vdb_entities_bge_m3_1024d. Again: no error, no results.

Builds the same objects rag_ingest.py builds, then reports what they resolved
to. Embeds nothing, calls no LLM, writes nothing.

    RAGBASE_ROOT=<base> python check_ingest_wiring.py
Exit 0 = host and server agree, 3 = they do not.
"""
import os
import sys

import ragbase
from lightrag.utils import EmbeddingFunc

from migrate_nano_to_qdrant import DEFAULT_WORKSPACE, ORDER, collection_name


def _probe_build_rag():
    """Import rag_ingest for real, swap its RAGAnything symbol for a stub
    that just records the kwargs it's called with, call the real build_rag
    with a dummy config, restore the symbol, and return what was captured.

    No LLM call, no embedding call, no network, no disk write: the stub's
    __init__ does nothing but store kwargs, and build_rag itself only builds
    plain Python objects (dict, EmbeddingFunc) before calling RAGAnything(...).
    """
    os.environ.setdefault("ZAI_API_KEY", "dummy-for-wiring-check")
    import rag_ingest

    captured = {}

    class _RecordingRAGAnything:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    orig = rag_ingest.RAGAnything
    rag_ingest.RAGAnything = _RecordingRAGAnything
    try:
        rag_ingest.build_rag(object())
    finally:
        rag_ingest.RAGAnything = orig
    return captured


def main():
    dim = int(ragbase.require_env("EMBEDDING_DIM"))
    model = ragbase.require_env("EMBEDDING_MODEL")
    declared = ragbase.env_value("LIGHTRAG_VECTOR_STORAGE") or "NanoVectorDBStorage"
    workspace = ragbase.env_value("WORKSPACE") or DEFAULT_WORKSPACE

    print("INGEST WIRING CHECK  base={}".format(ragbase.ROOT))
    print("  .env LIGHTRAG_VECTOR_STORAGE = {}".format(declared))

    problems = []

    src_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rag_ingest.py")
    src = open(src_path, encoding="utf-8").read()

    # --- 1/2c. what does build_rag ACTUALLY produce? --------------------------
    # Import rag_ingest for real, swap its RAGAnything symbol for a stub that
    # just records the kwargs it is called with, then call the real build_rag.
    # This asserts resolved behaviour instead of grepping identifiers out of
    # source text -- a `lightrag_kwargs.setdefault(...)` silently replaced with
    # `pass` still leaves every name in the file, but the stub would no longer
    # see vector_storage in its kwargs.
    captured = _probe_build_rag()
    lightrag_kwargs = captured.get("lightrag_kwargs", {})
    # LightRAG's own dataclass default when the kwarg is absent entirely.
    actual_vector_storage = lightrag_kwargs.get("vector_storage", "NanoVectorDBStorage")
    if actual_vector_storage != declared:
        problems.append(
            "rag_ingest.build_rag resolves vector_storage={!r} but .env declares "
            "{!r} -- the host ingest would write to the wrong backend".format(
                actual_vector_storage, declared))
    else:
        print("  build_rag resolves vector_storage={!r}   OK".format(actual_vector_storage))

    # --- 2. does its EmbeddingFunc carry a model_name? -----------------------
    model_name = getattr(captured.get("embedding_func"), "model_name", None)
    if not model_name:
        problems.append(
            "build_rag's embedding_func has no model_name -- the collection "
            "suffix would be empty and would not match the server")
    else:
        print("  build_rag's embedding_func.model_name={!r}   OK".format(model_name))

    # --- 2b. does anything else construct RAGAnything( for insertion? --------
    # Static-only: constructing RAGAnything/LightRAG has undocumented I/O risk,
    # so this walks source text instead of importing/instantiating anything.
    here = os.path.dirname(os.path.abspath(__file__))
    dup = None
    for fname in sorted(os.listdir(here)):
        if not fname.endswith(".py") or fname == "check_ingest_wiring.py":
            continue
        fpath = os.path.join(here, fname)
        text = src if fname == "rag_ingest.py" else open(fpath, encoding="utf-8").read()
        lines = text.split("\n")
        calls = [i for i, ln in enumerate(lines) if "RAGAnything(" in ln]
        if not calls:
            continue
        if fname == "rag_ingest.py":
            def_line = next((i for i, ln in enumerate(lines) if ln.startswith("def build_rag")), None)
            bad = def_line is None or len(calls) != 1 or calls[0] < def_line
        elif fname == "ingest_merged.py":
            def_line = next((i for i, ln in enumerate(lines) if ln.startswith("def parse_one")), None)
            next_def = None
            if def_line is not None:
                next_def = next((i for i in range(def_line + 1, len(lines))
                                  if lines[i].startswith("def ")), len(lines))
            bad = def_line is None or any(not (def_line < i < (next_def or -1)) for i in calls)
        else:
            bad = True
        if bad:
            dup = fname
            break
    if dup:
        problems.append(
            "{} constructs RAGAnything( outside build_rag/parse_one -- duplicate "
            "vector-backend decision".format(dup))
    else:
        print("  no duplicate RAGAnything(...) construction outside build_rag/parse_one   OK")

    # --- 3. what collection names does that combination actually produce? ----
    # Build the real object rather than trusting the source read above.
    ef = EmbeddingFunc(embedding_dim=dim, func=lambda texts: None, model_name=model)
    suffix = ef.model_name and "{}_{}d".format(
        __import__("re").sub(r"[^a-zA-Z0-9_]", "_", ef.model_name.strip().lower()), dim)
    if not suffix:
        problems.append("EmbeddingFunc produced no collection suffix")
    print("  embedding model {!r} -> suffix {!r}".format(model, suffix))
    print("  workspace {!r}".format(workspace))
    for ns in ORDER:
        print("    {:<15} -> {}".format(ns, collection_name(ns, model, dim)))

    # --- 4. if the base is on Qdrant, do those collections exist? ------------
    if declared == "QdrantVectorDBStorage":
        from qdrant_client import QdrantClient

        url = ragbase.env_value("QDRANT_URL") or "http://127.0.0.1:6333"
        client = QdrantClient(url=url, timeout=60)
        live = {c.name for c in client.get_collections().collections}
        for ns in ORDER:
            name = collection_name(ns, model, dim)
            if name not in live:
                problems.append(
                    "collection {} does not exist on {} -- the host ingest would "
                    "create a SECOND, empty one".format(name, url))
        stray = {n for n in live if n.startswith("lightrag_vdb_")
                 and n not in {collection_name(ns, model, dim) for ns in ORDER}}
        if stray:
            problems.append(
                "unexpected lightrag collections on the server: {} -- usually the "
                "footprint of an ingest that ran with the wrong model_name".format(
                    sorted(stray)))
        print("  qdrant at {} holds {} collection(s)".format(url, len(live)))

    if problems:
        print("\nINGEST WIRING CHECK FAILED:")
        for p in problems:
            print("  !", p)
        return 3
    print("\nINGEST WIRING CHECK OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
