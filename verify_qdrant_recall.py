"""Measure Qdrant's recall against exact cosine over the same vectors.

Qdrant is an approximate index; nano-vectordb was an exact brute-force scan.
This answers the only question that matters after a migration: does the new
index still return the same neighbours?

Ground truth is computed with numpy from the nano store's own matrix, so this
must run BEFORE the vdb_*.json files are deleted.

    RAGBASE_ROOT=<base> python verify_qdrant_recall.py [--queries 100] [--topk 10]

Exit 0 = every store at or above the threshold, 3 = below.
"""
import argparse
import os
import sys

import numpy as np
from qdrant_client import QdrantClient, models

import ragbase
from migrate_nano_to_qdrant import (DEFAULT_WORKSPACE, ORDER, WORKSPACE_ID_FIELD,
                                    collection_name, load_store)


def recall_for_store(client, namespace, dim, model_name, workspace, n_queries, topk):
    path = os.path.join(ragbase.STORAGE, "vdb_{}.json".format(namespace))
    name = collection_name(namespace, model_name, dim)
    records, matrix = load_store(path, dim)

    # rows are unit-norm, so a dot product IS the cosine
    ids = [r["__id__"] for r in records]
    rng = np.random.default_rng(1234)
    picks = rng.choice(len(ids), min(n_queries, len(ids)), replace=False)

    hits = 0
    total = 0
    empty = 0
    for i in picks:
        q = matrix[i]
        exact_rows = np.argpartition(-(matrix @ q), topk)[:topk]
        exact = {ids[int(j)] for j in exact_rows}

        res = client.query_points(
            collection_name=name,
            query=q.tolist(),
            limit=topk,
            with_payload=True,
            query_filter=models.Filter(must=[models.FieldCondition(
                key=WORKSPACE_ID_FIELD,
                match=models.MatchValue(value=workspace))]),
        ).points
        if not res:
            empty += 1
        got = {p.payload.get("id") for p in res}
        hits += len(exact & got)
        total += len(exact)

    del records, matrix
    return hits / total if total else 0.0, len(picks), empty


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--queries", type=int, default=100)
    ap.add_argument("--topk", type=int, default=10)
    ap.add_argument("--threshold", type=float, default=0.98)
    ap.add_argument("--url", default=None)
    args = ap.parse_args()

    dim = int(ragbase.require_env("EMBEDDING_DIM"))
    model_name = ragbase.require_env("EMBEDDING_MODEL")
    workspace = ragbase.env_value("WORKSPACE") or DEFAULT_WORKSPACE
    url = args.url or ragbase.env_value("QDRANT_URL") or "http://127.0.0.1:6333"

    print("RECALL CHECK  base={}  qdrant={}".format(ragbase.ROOT, url))
    print("  {} queries, top-{}, threshold {:.2f}\n".format(
        args.queries, args.topk, args.threshold))

    client = QdrantClient(url=url, timeout=300)
    failures = []
    for ns in ORDER:
        recall, nq, empty = recall_for_store(
            client, ns, dim, model_name, workspace, args.queries, args.topk)
        flag = "OK " if recall >= args.threshold else "LOW"
        print("  {} {:<15} recall@{} = {:.4f}  ({} queries, {} empty)".format(
            flag, ns, args.topk, recall, nq, empty), flush=True)
        if recall < args.threshold:
            failures.append("{}: recall {:.4f} < {:.2f}".format(ns, recall, args.threshold))
        if empty:
            failures.append("{}: {} queries returned nothing".format(ns, empty))

    if failures:
        print("\nRECALL CHECK FAILED:")
        for f in failures:
            print("  !", f)
        return 3
    print("\nRECALL CHECK OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
