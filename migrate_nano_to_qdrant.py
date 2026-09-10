"""Copy a base's nano-vectordb stores into Qdrant, without re-embedding.

Reads the three vdb_*.json files and writes the points LightRAG's
QdrantVectorDBStorage would have written: same collection names, same
deterministic point ids, same payload fields. Embeddings are copied verbatim
from the stores' float32 `matrix`, so retrieval stays directly comparable to
the nano baseline.

    RAGBASE_ROOT=<base> python migrate_nano_to_qdrant.py [--apply] [--url URL]

Defaults to a dry run: it validates and reports, and writes nothing.

Stores are done smallest-first (chunks, entities, relationships) with a point
count assert between them, so a crash costs at most one store. Point ids are a
deterministic hash of the record id, so re-running after a crash overwrites the
same points instead of duplicating -- just run it again.
"""
import argparse
import base64
import gc
import hashlib
import json
import os
import re
import sys
import time
import uuid
import zlib

import numpy as np
from qdrant_client import QdrantClient, models

import ragbase

# Must match lightrag/lightrag.py's meta_fields per vector namespace exactly:
# anything extra becomes payload bloat, anything missing breaks get_by_id.
META_FIELDS = {
    "chunks": {"full_doc_id", "content", "file_path"},
    "entities": {"entity_name", "source_id", "content", "file_path"},
    "relationships": {"src_id", "tgt_id", "source_id", "content", "file_path"},
}
# ascending file size -- the cheap stores prove the code before the 800 MB one
ORDER = ("chunks", "entities", "relationships")

DEFAULT_WORKSPACE = "_"                 # kg/qdrant_impl.py:DEFAULT_WORKSPACE
WORKSPACE_ID_FIELD = "workspace_id"
MAX_PAYLOAD_BYTES = 16 * 1024 * 1024    # QDRANT_UPSERT_MAX_PAYLOAD_BYTES default
MAX_POINTS_PER_BATCH = 128              # QDRANT_UPSERT_MAX_POINTS_PER_BATCH default
CROSSCHECK_SAMPLE = 200


def point_id(content, prefix):
    """Byte-for-byte the id scheme in kg/qdrant_impl.py."""
    digest = hashlib.sha256((prefix + content).encode("utf-8")).digest()
    return uuid.UUID(bytes=digest[:16], version=4).hex


def collection_name(namespace, model_name, dim):
    """Byte-for-byte the naming in base.py::_generate_collection_suffix."""
    safe = re.sub(r"[^a-zA-Z0-9_]", "_", model_name.strip().lower())
    return "lightrag_vdb_{}_{}_{}d".format(namespace, safe, dim)


def load_store(path, dim):
    """Return (records, matrix). Raises on any shape or content problem."""
    with open(path, encoding="utf-8") as f:
        store = json.load(f)

    records = store["data"]
    matrix = np.frombuffer(base64.b64decode(store["matrix"]), dtype=np.float32)
    if matrix.size % dim:
        raise ValueError("{}: matrix size {} not a multiple of dim {}".format(
            path, matrix.size, dim))
    matrix = matrix.reshape(-1, dim)
    if matrix.shape[0] != len(records):
        raise ValueError("{}: {} matrix rows vs {} records".format(
            path, matrix.shape[0], len(records)))
    bad = int((~np.isfinite(matrix)).sum())
    if bad:
        raise ValueError("{}: {} non-finite values in matrix".format(path, bad))
    zero = int((np.linalg.norm(matrix, axis=1) == 0).sum())
    if zero:
        raise ValueError(
            "{}: {} zero-norm vectors -- embedding backend was broken".format(path, zero))
    return records, matrix


def crosscheck_alignment(records, matrix, sample):
    """Prove row i of `matrix` really belongs to records[i].

    Each record carries an independent zlib-compressed float16 copy of its own
    vector. If the two ever disagree, the matrix is offset against the records
    and every migrated payload would be attached to the wrong embedding.
    """
    n = len(records)
    idx = np.random.default_rng(0).choice(n, min(sample, n), replace=False)
    worst = 1.0
    for i in idx:
        raw = records[int(i)].get("vector")
        if not raw:
            continue
        v = np.frombuffer(
            zlib.decompress(base64.b64decode(raw)), dtype=np.float16
        ).astype(np.float32)
        if v.shape[0] != matrix.shape[1]:
            raise ValueError("record {}: float16 copy has {} dims".format(i, v.shape[0]))
        cos = float(v @ matrix[i] / (np.linalg.norm(v) * np.linalg.norm(matrix[i])))
        worst = min(worst, cos)
    if worst < 0.999:
        raise ValueError(
            "row alignment check failed: worst cosine {:.6f} < 0.999 -- "
            "the matrix does not line up with the records".format(worst))
    return worst


def build_points(records, matrix, namespace, workspace):
    """Yield PointStructs shaped exactly like _flush_pending_vector_ops does."""
    meta = META_FIELDS[namespace]
    now = int(time.time())
    for i, rec in enumerate(records):
        doc_id = rec["__id__"]
        payload = {
            "id": doc_id,
            WORKSPACE_ID_FIELD: workspace,
            # keep the record's own timestamp; qdrant_impl would stamp "now",
            # but preserving it keeps the store's history honest
            "created_at": rec.get("__created_at__", now),
        }
        payload.update({k: rec[k] for k in meta if k in rec})
        yield models.PointStruct(
            id=point_id(doc_id, prefix=workspace),
            vector=matrix[i].tolist(),
            payload=payload,
        )


def batched(points, max_bytes, max_points):
    """Split into batches under both the payload-size and point-count caps."""
    batch, size = [], 0
    for p in points:
        # rough but conservative: payload json + 4 bytes per float
        est = len(json.dumps(p.payload, ensure_ascii=False).encode()) + len(p.vector) * 4
        if batch and (size + est > max_bytes or len(batch) >= max_points):
            yield batch
            batch, size = [], 0
        batch.append(p)
        size += est
    if batch:
        yield batch


def ensure_collection(client, name, dim, apply_writes):
    """Returns True if the collection was missing. Creates it only with --apply."""
    if client.collection_exists(name):
        return False
    if not apply_writes:
        return True
    client.create_collection(
        collection_name=name,
        vectors_config=models.VectorParams(size=dim, distance=models.Distance.COSINE),
        # same as kg/qdrant_impl.py::initialize -- global HNSW off, payload index on
        hnsw_config=models.HnswConfigDiff(payload_m=16, m=0),
    )
    client.create_payload_index(
        collection_name=name,
        field_name=WORKSPACE_ID_FIELD,
        field_schema=models.PayloadSchemaType.KEYWORD,
    )
    return True


def check_count_gate(counted, before, expected, topup):
    """Raise SystemExit-worthy ValueError if the post-upsert count is wrong.

    Normal mode: the collection must end up with exactly `expected` points.
    Topup mode: the collection already held `before` points; upserting
    `expected` more (with possible overwrites of existing ids) must land
    somewhere in [before, before + expected].
    """
    if not topup:
        if counted != expected:
            raise ValueError(
                "qdrant has {:,}, store has {:,}".format(counted, expected))
        return "count gate OK: {:,} points".format(counted)
    if not (before <= counted <= before + expected):
        raise ValueError(
            "qdrant has {:,}, expected between {:,} (before) and {:,} "
            "(before + upserted)".format(counted, before, before + expected))
    overwrites = before + expected - counted
    return ("count gate OK (topup): {:,} -> {:,} points "
            "({:,} of {:,} upserted were overwrites)".format(
                before, counted, overwrites, expected))


def migrate_store(client, namespace, dim, model_name, workspace, apply_writes, topup=False):
    path = os.path.join(ragbase.STORAGE, "vdb_{}.json".format(namespace))
    name = collection_name(namespace, model_name, dim)
    size_mb = os.path.getsize(path) / 1024 / 1024
    print("\n=== {}: {:,.0f} MB -> {}".format(namespace, size_mb, name), flush=True)

    t0 = time.time()
    records, matrix = load_store(path, dim)
    worst = crosscheck_alignment(records, matrix, CROSSCHECK_SAMPLE)
    print("    {:,} records, matrix {}, row-alignment worst cosine {:.6f}, "
          "loaded in {:.1f}s".format(len(records), matrix.shape, worst,
                                     time.time() - t0), flush=True)

    missing = ensure_collection(client, name, dim, apply_writes)
    if not missing:
        state = "exists"
    elif apply_writes:
        state = "created"
    else:
        state = "WOULD create"
    print("    collection {}".format(state), flush=True)

    counted = None
    if apply_writes:
        before = 0 if missing else client.count(collection_name=name, exact=True).count

        t0 = time.time()
        sent = 0
        last = None
        for batch in batched(build_points(records, matrix, namespace, workspace),
                             MAX_PAYLOAD_BYTES, MAX_POINTS_PER_BATCH):
            client.upsert(collection_name=name, points=batch, wait=False)
            sent += len(batch)
            last = batch
            if sent % 10000 < MAX_POINTS_PER_BATCH:
                print("    {:,}/{:,} ({:,.0f} pts/s)".format(
                    sent, len(records), sent / max(time.time() - t0, 1e-9)), flush=True)
        if last is not None:
            # one blocking write so the server has flushed before we count
            client.upsert(collection_name=name, points=last, wait=True)
        print("    upserted {:,} points in {:.1f}s".format(sent, time.time() - t0),
              flush=True)

        counted = client.count(collection_name=name, exact=True).count
        try:
            msg = check_count_gate(counted, before, len(records), topup)
        except ValueError as e:
            raise SystemExit(
                "COUNT GATE FAILED for {}: {} -- "
                "stopping before the next store".format(namespace, e))
        print("    " + msg, flush=True)

    expected = len(records)
    del records, matrix
    gc.collect()
    return {"namespace": namespace, "collection": name,
            "records": expected, "counted": counted, "mb": size_mb}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="actually write; without it this is a dry run")
    ap.add_argument("--topup", action="store_true",
                    help="incremental top-up into non-empty collections; the "
                         "count gate checks growth instead of equality")
    ap.add_argument("--url", default=os.environ.get("QDRANT_URL", "http://127.0.0.1:6333"))
    ap.add_argument("--workspace", default=None,
                    help="defaults to WORKSPACE from .env, else '_'")
    ap.add_argument("--model-name", default=None,
                    help="defaults to EMBEDDING_MODEL from .env")
    args = ap.parse_args()

    dim = int(ragbase.require_env("EMBEDDING_DIM"))
    model_name = args.model_name or ragbase.require_env("EMBEDDING_MODEL")
    workspace = args.workspace or (ragbase.env_value("WORKSPACE") or DEFAULT_WORKSPACE)

    print("base      {}".format(ragbase.ROOT))
    print("qdrant    {}".format(args.url))
    print("workspace {!r}   model {!r}   dim {}".format(workspace, model_name, dim))
    print("mode      {}".format("APPLY" if args.apply else "DRY RUN (nothing written)"))

    client = QdrantClient(url=args.url, timeout=300)
    print("server    {}".format(client.info().version))

    results = [migrate_store(client, ns, dim, model_name, workspace, args.apply,
                             topup=args.topup)
               for ns in ORDER]

    print("\n=== summary")
    for r in results:
        got = "{:,}".format(r["counted"]) if r["counted"] is not None else "-"
        print("  {:<15} {:>8,} records  {:>9} in qdrant  ({:,.0f} MB json)".format(
            r["namespace"], r["records"], got, r["mb"]))
    print("  {:<15} {:>8,} records".format("TOTAL", sum(r["records"] for r in results)))
    if not args.apply:
        print("\ndry run only -- re-run with --apply to write")
    return 0


if __name__ == "__main__":
    sys.exit(main())
