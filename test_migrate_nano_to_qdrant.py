"""Self-check for migrate_nano_to_qdrant.py -- no server, no network.

The point of this test is that the migration writes points LightRAG will
later read. So the id scheme is checked against lightrag's OWN function
rather than against a second copy of my arithmetic: if upstream changes it,
this fails instead of silently migrating into unreadable ids.

    RAGBASE_ROOT=<any base> python test_migrate_nano_to_qdrant.py
"""
import base64
import json
import os
import sys
import tempfile
import zlib

import numpy as np

import migrate_nano_to_qdrant as m


def test_point_id_matches_lightrag():
    from lightrag.kg.qdrant_impl import compute_mdhash_id_for_qdrant

    for ws in ("_", "space1"):
        for doc_id in ("chunk-abc", "ent-0011", "rel-ffff", "doc-äöü"):
            assert m.point_id(doc_id, ws) == compute_mdhash_id_for_qdrant(doc_id, prefix=ws), \
                (doc_id, ws)
    print("ok  point_id matches lightrag.kg.qdrant_impl")


def test_collection_name():
    assert m.collection_name("chunks", "bge-m3", 1024) == "lightrag_vdb_chunks_bge_m3_1024d"
    assert m.collection_name("entities", "BGE-M3", 1024) == "lightrag_vdb_entities_bge_m3_1024d"
    assert m.collection_name(
        "relationships", "text-embedding-3-large", 3072
    ) == "lightrag_vdb_relationships_text_embedding_3_large_3072d"
    print("ok  collection_name matches base.py::_generate_collection_suffix")


def test_meta_fields_match_lightrag_source():
    """The payload keys are taken from lightrag.py; drift would break get_by_id."""
    import lightrag.lightrag as core

    src = open(core.__file__, encoding="utf-8").read()
    for fields in m.META_FIELDS.values():
        needle = "meta_fields={" + ", ".join(
            '"%s"' % f for f in sorted(fields)) + "}"
        # lightrag writes them in its own order, so compare as sets per line
        found = False
        for line in src.splitlines():
            if "meta_fields={" in line:
                got = set(part.strip().strip('",') for part in
                          line.split("{", 1)[1].rsplit("}", 1)[0].split(","))
                if got == fields:
                    found = True
                    break
        assert found, "no meta_fields line in lightrag.py matches %s (%s)" % (fields, needle)
    print("ok  META_FIELDS all found verbatim in lightrag/lightrag.py")


def _fake_store(path, n, dim, prefix):
    rng = np.random.default_rng(7)
    mat = rng.standard_normal((n, dim), dtype=np.float32)
    mat /= np.linalg.norm(mat, axis=1, keepdims=True)
    data = []
    for i in range(n):
        f16 = mat[i].astype(np.float16).tobytes()
        data.append({
            "__id__": "%s-%04d" % (prefix, i),
            "__created_at__": 1700000000 + i,
            "content": "content %d" % i,
            "file_path": "f%d.pdf" % i,
            "full_doc_id": "doc-%d" % i,
            "entity_name": "E%d" % i,
            "source_id": "s%d" % i,
            "src_id": "A%d" % i,
            "tgt_id": "B%d" % i,
            "vector": base64.b64encode(zlib.compress(f16)).decode(),
        })
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"embedding_dim": dim, "data": data,
                   "matrix": base64.b64encode(mat.tobytes()).decode()}, f)
    return mat


def test_load_and_alignment():
    dim = 64
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "vdb_chunks.json")
        mat = _fake_store(p, 50, dim, "chunk")
        records, loaded = m.load_store(p, dim)
        assert loaded.shape == (50, dim)
        assert np.allclose(loaded, mat)
        worst = m.crosscheck_alignment(records, loaded, 50)
        assert worst > 0.999, worst

        # shuffling the matrix must be caught by the float16 cross-check
        shuffled = np.roll(loaded, 1, axis=0)
        try:
            m.crosscheck_alignment(records, shuffled, 50)
        except ValueError as e:
            assert "alignment" in str(e)
        else:
            raise AssertionError("misaligned matrix was NOT detected")
    print("ok  load_store validates, crosscheck_alignment catches a shifted matrix")


def test_payload_shape():
    dim = 8
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "vdb_relationships.json")
        _fake_store(p, 3, dim, "rel")
        records, mat = m.load_store(p, dim)
        pts = list(m.build_points(records, mat, "relationships", "_"))
        assert len(pts) == 3
        keys = set(pts[0].payload)
        assert keys == {"id", "workspace_id", "created_at",
                        "src_id", "tgt_id", "source_id", "content", "file_path"}, keys
        # chunks must NOT carry entity/relationship fields
        records, mat = m.load_store(p, dim)
        pts = list(m.build_points(records, mat, "chunks", "_"))
        assert set(pts[0].payload) == {"id", "workspace_id", "created_at",
                                       "full_doc_id", "content", "file_path"}
        assert pts[0].payload["created_at"] == 1700000000
        assert len(pts[0].vector) == dim
    print("ok  build_points emits exactly the per-namespace meta fields")


def test_batching_respects_caps():
    class P:
        def __init__(self, n):
            self.payload = {"id": "x" * 100, "n": n}
            self.vector = [0.0] * 16

    pts = [P(i) for i in range(1000)]
    batches = list(m.batched(pts, m.MAX_PAYLOAD_BYTES, m.MAX_POINTS_PER_BATCH))
    assert sum(len(b) for b in batches) == 1000
    assert max(len(b) for b in batches) <= m.MAX_POINTS_PER_BATCH

    # a tiny byte budget must split by size, not silently emit oversized batches
    tight = list(m.batched(pts, 500, m.MAX_POINTS_PER_BATCH))
    assert sum(len(b) for b in tight) == 1000
    assert max(len(b) for b in tight) < m.MAX_POINTS_PER_BATCH
    print("ok  batched respects both the point-count and payload-size caps")


def test_check_count_gate():
    # normal mode: exact match only
    m.check_count_gate(100, 0, 100, topup=False)
    try:
        m.check_count_gate(99, 0, 100, topup=False)
    except ValueError:
        pass
    else:
        raise AssertionError("normal-mode gate did not reject a short count")

    # topup mode: growth into a non-empty collection is fine, with overwrites
    msg = m.check_count_gate(1050, 1000, 100, topup=True)
    assert "50 of 100" in msg, msg

    # topup mode: count going backwards must be rejected
    try:
        m.check_count_gate(900, 1000, 100, topup=True)
    except ValueError:
        pass
    else:
        raise AssertionError("topup gate did not reject a count that went backwards")
    print("ok  check_count_gate accepts growth, rejects equality miss and backwards count")


def main():
    test_point_id_matches_lightrag()
    test_collection_name()
    test_meta_fields_match_lightrag_source()
    test_load_and_alignment()
    test_payload_shape()
    test_batching_respects_caps()
    test_check_count_gate()
    print("\nOK: migrate_nano_to_qdrant self-check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
