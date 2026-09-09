"""Check the streamed NanoVectorDB.save() patch in rag_ingest.py.

Run:  RAGBASE_ROOT=<any base> ZAI_API_KEY=dummy python test_streamed_save.py

Guards the flush-phase memory spike fix: nano-vectordb's stock save() encodes
the whole matrix and builds the entire JSON document in memory (2.2 GB of heap
churn for a 744 MB file, on a box with a known RAM fault). The streamed writer
must produce a file that parses back identically to the stock writer's.
"""
import json
import os
import tempfile

import numpy as np

import rag_ingest as R
from nano_vectordb.dbs import NanoVectorDB, load_storage

DIM = 16
tmpdir = tempfile.mkdtemp()


def _build(path):
    db = NanoVectorDB(embedding_dim=DIM, storage_file=path)
    rng = np.random.default_rng(0)
    db.upsert(
        [
            {
                "__id__": f"id-{i}",
                "__vector__": rng.random(DIM, dtype=np.float32),
                "content": f"Stäbe {i} — Lösung",
            }
            for i in range(50)
        ]
    )
    db.store_additional_data(note="ümlaut")
    return db


stock_path = os.path.join(tmpdir, "stock.json")
db = _build(stock_path)
R._orig_nvdb_save(db)  # stock writer, kept by the patch

stream_path = os.path.join(tmpdir, "stream.json")
db.storage_file = stream_path
db.save()  # patched streamed writer

with open(stock_path, encoding="utf-8") as f:
    stock = json.load(f)
with open(stream_path, encoding="utf-8") as f:
    streamed = json.load(f)
assert stock == streamed, "streamed JSON differs from stock JSON"

got = load_storage(stream_path)
assert got["embedding_dim"] == DIM
assert len(got["data"]) == 50
assert got["data"][7]["content"] == "Stäbe 7 — Lösung"
assert got["additional_data"] == {"note": "ümlaut"}
np.testing.assert_array_equal(got["matrix"], load_storage(stock_path)["matrix"])

empty_path = os.path.join(tmpdir, "empty.json")
NanoVectorDB(embedding_dim=DIM, storage_file=empty_path).save()
assert load_storage(empty_path)["matrix"].shape == (0, DIM)

print("OK: streamed save matches stock save (50 rows + empty store)")
