"""Post-ingest sanity check for the LightRAG vector stores.

Catches the failure mode where the embedding backend silently returns zero
vectors (e.g. a corrupt Ollama model blob): normalizing a zero vector yields
NaN, NaN poisons nano-vectordb's matrix at load time, and retrieval returns
nothing -- while the ingest itself still exits 0.

Backend-aware: with LIGHTRAG_VECTOR_STORAGE=QdrantVectorDBStorage the three
vdb_*.json files no longer exist (or, worse, still exist and are stale), so the
same checks run against the Qdrant collections instead. Reading the stale files
would report a healthy store that nothing queries any more.

Run standalone at any time:
    RAGBASE_ROOT=<base> python check_vectors.py
Exit code 0 = healthy, 3 = problems found.
"""

import base64
import json
import os
import sys

import numpy as np

import ragbase

STORAGE = ragbase.STORAGE
STORES = ("vdb_chunks.json", "vdb_entities.json", "vdb_relationships.json")
NAMESPACES = ("chunks", "entities", "relationships")
DIM = int(ragbase.require_env("EMBEDDING_DIM"))
QDRANT_SAMPLE = 512


def check_store(path: str) -> list[str]:
    """Return a list of problem descriptions for one vector store (empty = healthy)."""
    problems: list[str] = []
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    records = data["data"]
    matrix = np.frombuffer(base64.b64decode(data["matrix"]), dtype=np.float32)
    if matrix.size % DIM:
        problems.append(f"matrix size {matrix.size} is not a multiple of dim {DIM}")
        return problems

    matrix = matrix.reshape(-1, DIM)
    if matrix.shape[0] != len(records):
        problems.append(
            f"data/matrix misaligned: {len(records)} records vs {matrix.shape[0]} matrix rows"
        )

    norms = np.linalg.norm(matrix, axis=1)
    nonfinite = int(np.sum(~np.isfinite(norms)))
    zero = int(np.sum(norms == 0))
    if nonfinite:
        problems.append(f"{nonfinite} NaN/inf vector rows")
    if zero:
        problems.append(f"{zero} all-zero vector rows")

    name = os.path.basename(path)
    print(
        f"  {name:<26} rows={len(records):<7} matrix={matrix.shape[0]:<7} "
        f"nonfinite={nonfinite:<5} zero={zero}"
    )
    return problems


def check_qdrant() -> list[str]:
    """Same guarantees as check_store, against the Qdrant collections.

    Cannot compare row counts against a matrix (there is none), so it checks
    the collection is green and non-empty, samples real vectors for the
    zero/NaN failure mode, and cross-checks the chunks collection against
    kv_store_text_chunks.json -- the one count that still lives on disk and
    would expose a half-finished migration.
    """
    # imported here so a nano base with no qdrant-client installed still runs
    from qdrant_client import QdrantClient

    from migrate_nano_to_qdrant import collection_name

    url = ragbase.env_value("QDRANT_URL") or "http://127.0.0.1:6333"
    model = ragbase.require_env("EMBEDDING_MODEL")
    print("VECTOR SANITY CHECK (qdrant):", url)

    problems: list[str] = []
    client = QdrantClient(url=url, timeout=120)

    expected_chunks = None
    chunk_kv = os.path.join(STORAGE, "kv_store_text_chunks.json")
    if os.path.exists(chunk_kv):
        with open(chunk_kv, encoding="utf-8") as f:
            expected_chunks = len(json.load(f))

    for ns in NAMESPACES:
        name = collection_name(ns, model, DIM)
        if not client.collection_exists(name):
            problems.append(f"{ns}: collection {name} missing")
            print(f"  {ns:<26} MISSING ({name})")
            continue

        info = client.get_collection(name)
        count = client.count(collection_name=name, exact=True).count
        status = str(info.status)

        points, _ = client.scroll(collection_name=name, limit=QDRANT_SAMPLE,
                                  with_vectors=True, with_payload=False)
        vecs = np.array([p.vector for p in points], dtype=np.float32) if points else             np.zeros((0, DIM), dtype=np.float32)
        if vecs.size and vecs.shape[1] != DIM:
            problems.append(f"{ns}: vectors are {vecs.shape[1]}-dim, expected {DIM}")
        norms = np.linalg.norm(vecs, axis=1) if vecs.size else np.zeros(0)
        nonfinite = int(np.sum(~np.isfinite(norms)))
        zero = int(np.sum(norms == 0))

        if count == 0:
            problems.append(f"{ns}: collection is empty")
        if "green" not in status.lower():
            problems.append(f"{ns}: collection status is {status}, not green")
        if nonfinite:
            problems.append(f"{ns}: {nonfinite} NaN/inf vectors in a {len(points)} sample")
        if zero:
            problems.append(f"{ns}: {zero} all-zero vectors in a {len(points)} sample")
        if ns == "chunks" and expected_chunks is not None and count != expected_chunks:
            problems.append(
                f"chunks: {count} points vs {expected_chunks} in kv_store_text_chunks.json"
            )

        print(f"  {ns:<26} points={count:<7} status={status:<8} "
              f"sampled={len(points):<5} nonfinite={nonfinite:<5} zero={zero}")

    return problems


def main() -> int:
    if (ragbase.env_value("LIGHTRAG_VECTOR_STORAGE") or "") == "QdrantVectorDBStorage":
        failures = check_qdrant()
        if failures:
            print("VECTOR SANITY CHECK FAILED:")
            for f in failures:
                print("  !", f)
            return 3
        print("VECTOR SANITY CHECK OK")
        return 0

    print("VECTOR SANITY CHECK:", STORAGE)
    failures: list[str] = []
    for name in STORES:
        path = os.path.join(STORAGE, name)
        if not os.path.exists(path):
            failures.append(f"{name}: missing")
            print(f"  {name:<26} MISSING")
            continue
        try:
            for problem in check_store(path):
                failures.append(f"{name}: {problem}")
        except Exception as exc:  # unreadable/corrupt file is itself the finding
            failures.append(f"{name}: unreadable ({type(exc).__name__}: {exc})")
            print(f"  {name:<26} UNREADABLE: {exc}")

    if failures:
        print("VECTOR SANITY CHECK FAILED:")
        for f in failures:
            print("  !", f)
        print(
            "Fix before trusting queries: verify the embedding model "
            "(ollama blob hash) and re-embed the bad rows."
        )
        return 3

    print("VECTOR SANITY CHECK OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
