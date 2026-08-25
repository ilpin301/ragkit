"""Post-ingest sanity check for the LightRAG vector stores.

Catches the failure mode where the embedding backend silently returns zero
vectors (e.g. a corrupt Ollama model blob): normalizing a zero vector yields
NaN, NaN poisons nano-vectordb's matrix at load time, and retrieval returns
nothing -- while the ingest itself still exits 0.

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
DIM = int(ragbase.require_env("EMBEDDING_DIM"))


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


def main() -> int:
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
