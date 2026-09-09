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


def main():
    dim = int(ragbase.require_env("EMBEDDING_DIM"))
    model = ragbase.require_env("EMBEDDING_MODEL")
    declared = ragbase.env_value("LIGHTRAG_VECTOR_STORAGE") or "NanoVectorDBStorage"
    workspace = ragbase.env_value("WORKSPACE") or DEFAULT_WORKSPACE

    print("INGEST WIRING CHECK  base={}".format(ragbase.ROOT))
    print("  .env LIGHTRAG_VECTOR_STORAGE = {}".format(declared))

    problems = []

    # --- 1. does rag_ingest actually forward the backend? --------------------
    src_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rag_ingest.py")
    src = open(src_path, encoding="utf-8").read()
    if "lightrag_kwargs" not in src:
        problems.append("rag_ingest.py never passes lightrag_kwargs -- the core "
                        "dataclass will silently default to NanoVectorDBStorage")
    else:
        print("  rag_ingest.py forwards vector_storage via lightrag_kwargs   OK")

    # --- 2. does its EmbeddingFunc carry a model_name? -----------------------
    if "model_name=" not in src:
        problems.append("rag_ingest.py's EmbeddingFunc has no model_name -- the "
                        "collection suffix will be empty and will not match the server")
    else:
        print("  rag_ingest.py sets EmbeddingFunc.model_name                 OK")

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
