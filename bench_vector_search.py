"""Time the vector-search primitive: Qdrant vs the numpy scan nano-vectordb did.

The end-to-end query timings in a LightRAG run are dominated by LLM calls and
by whether keyword extraction hit the cache, so they cannot answer "is search
faster". This times only the search itself, on the same vectors, with the same
top-k, in the same process.

nano-vectordb's query is a brute-force `matrix @ q` over every row plus a
partial sort -- that is what is reproduced here as the baseline.

    RAGBASE_ROOT=<base> python bench_vector_search.py [--queries 50] [--topk 10]

Needs the vdb_*.json files to still exist (they are the baseline).
"""
import argparse
import os
import statistics
import sys
import time

import numpy as np
from qdrant_client import QdrantClient, models

import ragbase
from migrate_nano_to_qdrant import (DEFAULT_WORKSPACE, ORDER, WORKSPACE_ID_FIELD,
                                    collection_name, load_store)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--queries", type=int, default=50)
    ap.add_argument("--topk", type=int, default=10)
    ap.add_argument("--url", default=None)
    args = ap.parse_args()

    dim = int(ragbase.require_env("EMBEDDING_DIM"))
    model = ragbase.require_env("EMBEDDING_MODEL")
    workspace = ragbase.env_value("WORKSPACE") or DEFAULT_WORKSPACE
    url = args.url or ragbase.env_value("QDRANT_URL") or "http://127.0.0.1:6333"

    client = QdrantClient(url=url, timeout=300)
    print("SEARCH BENCHMARK  {} queries, top-{}\n".format(args.queries, args.topk))
    print("  {:<15} {:>8}  {:>12}  {:>12}  {:>7}".format(
        "store", "rows", "numpy ms", "qdrant ms", "speedup"))
    print("  " + "-" * 60)

    for ns in ORDER:
        path = os.path.join(ragbase.STORAGE, "vdb_{}.json".format(ns))
        if not os.path.exists(path):
            print("  {:<15} (no {} - baseline unavailable)".format(ns, os.path.basename(path)))
            continue
        records, matrix = load_store(path, dim)
        name = collection_name(ns, model, dim)
        rng = np.random.default_rng(99)
        picks = rng.choice(len(records), min(args.queries, len(records)), replace=False)
        qs = [matrix[i] for i in picks]

        # warm both paths once so neither pays a first-call cost
        _ = np.argpartition(-(matrix @ qs[0]), args.topk)[:args.topk]
        client.query_points(collection_name=name, query=qs[0].tolist(),
                            limit=args.topk, with_payload=False)

        numpy_ms = []
        for q in qs:
            t0 = time.perf_counter()
            np.argpartition(-(matrix @ q), args.topk)[:args.topk]
            numpy_ms.append((time.perf_counter() - t0) * 1000)

        qdrant_ms = []
        for q in qs:
            t0 = time.perf_counter()
            client.query_points(
                collection_name=name, query=q.tolist(), limit=args.topk,
                with_payload=False,
                query_filter=models.Filter(must=[models.FieldCondition(
                    key=WORKSPACE_ID_FIELD,
                    match=models.MatchValue(value=workspace))]),
            )
            qdrant_ms.append((time.perf_counter() - t0) * 1000)

        n_med = statistics.median(numpy_ms)
        q_med = statistics.median(qdrant_ms)
        print("  {:<15} {:>8,}  {:>12.1f}  {:>12.1f}  {:>6.2f}x".format(
            ns, len(records), n_med, q_med, n_med / q_med if q_med else 0))
        del records, matrix

    print("\n  numpy = brute-force scan over every row, in-process (what nano did)")
    print("  qdrant = HNSW over HTTP, including network + JSON serialization")
    return 0


if __name__ == "__main__":
    sys.exit(main())
