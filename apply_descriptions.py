"""Apply a batch of entity-description edits in ONE graph write + ONE embed flush.

Phase B of the collect-first / write-once enrichment (phase A is the enrichers'
--collect FILE). An API edit (POST /graph/entity/edit) rewrites the whole graphml
per node (~30 s at 125 MB); this runs the same LightRAG edit code host-side with the
persist deferred, so N edits cost one graphml write and one batched Qdrant upsert.

Usage:  RAGBASE_ROOT=<base> python apply_descriptions.py FILE     (launcher only)
        python apply_descriptions.py --selftest                   (no env needed)
Normally invoked through ragkit/ingest.ps1 -DescFile FILE, which stops the lightrag
service first (shared graphml, single writer) and runs check_vectors.py afterwards.

FILE is JSON {"<entity name>": {"old": "<expected current description, may be ''>",
"new": "<non-empty new description>"}, ...}; duplicate names are rejected.

Exit codes:
  0  ok (including nothing to do)
  2  invalid FILE / usage
  3  preflight failed (QDRANT_URL not loopback, Ollama down, collection missing)
  5  self-verify mismatch (rollback file path is printed)
  6  graphml changed on disk between load and persist (nothing written)
Any other non-zero (1) is an unexpected exception; a persist failure after the graph
commit is healed by re-running the same FILE ([already] entries re-embed).

Rollback: FILE.rollback.<stamp>.json is written BEFORE any mutation and is itself a
valid FILE; apply it with the same launcher to restore graph and vectors.
"""
import asyncio
import json
import os
import sys
import time
import uuid

VERIFY_BATCH = 100


def _pairs(pairs):
    d = {}
    for k, v in pairs:
        if k in d:
            raise ValueError(f"duplicate entity name in FILE: {k!r}")
        d[k] = v
    return d


def load_entries(path):
    """Parse + validate FILE. ValueError/OSError -> caller exits 2."""
    with open(path, encoding="utf-8-sig") as f:
        data = json.load(f, object_pairs_hook=_pairs)
    if not isinstance(data, dict):
        raise ValueError("top level of FILE must be an object {name: {old, new}}")
    for name, e in data.items():
        if not isinstance(e, dict):
            raise ValueError(f"entry {name!r}: value must be an object with old/new")
        if not isinstance(e.get("old"), str):
            raise ValueError(f"entry {name!r}: 'old' must be a string (may be empty)")
        if not isinstance(e.get("new"), str) or not e["new"].strip():
            raise ValueError(f"entry {name!r}: 'new' must be a non-empty string")
    return data


def _stat(path):
    st = os.stat(path)
    return (st.st_mtime_ns, st.st_size)


def _write_json_atomic(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


async def _drop_pending(rag):
    # A buffered vector upsert must never reach Qdrant for a graph edit that was
    # discarded: finalize_storages() would flush it.
    await rag.entities_vdb.drop_pending_index_ops()
    await rag.relationships_vdb.drop_pending_index_ops()


async def apply(rag, entries, graph_path, stat0, rollback_path, log=print,
                before_persist=None):
    """Classify, back up, edit in memory, guard, persist once.

    Returns (code, result): code 0 or 6; result has lists applied/already/gone/
    changed and rollback (path or None). before_persist is a test hook only.
    """
    from lightrag import utils_graph

    graph = rag.chunk_entity_relation_graph
    res = {"applied": [], "already": [], "gone": [], "changed": [], "rollback": None}
    cur_desc = {}

    # first pass: no mutation. get_node returns the LIVE attr dict, so read the
    # description string out now, before any edit touches it.
    for name, e in entries.items():
        node = await graph.get_node(name)
        if node is None:
            log(f"[gone] {name}")
            res["gone"].append(name)
            continue
        cur = node.get("description") or ""
        cur_desc[name] = cur
        if cur == e["new"]:
            log(f"[already] {name}")
            res["already"].append(name)   # still re-embedded: heals a half-persisted run
        elif cur != e["old"]:
            log(f"[changed] {name}")
            res["changed"].append(name)   # stale-clobber guard: someone edited it since collect
        else:
            res["applied"].append(name)

    todo = res["applied"] + res["already"]
    if not todo:
        return 0, res

    # inverse file first. ponytail: a node whose ORIGINAL description was empty gives
    # an inverse entry with new='', which load_entries rejects (exit 2 names it);
    # delete that entry from the rollback file by hand.
    _write_json_atomic(rollback_path, {
        n: {"old": entries[n]["new"], "new": cur_desc[n]} for n in todo})
    res["rollback"] = rollback_path

    orig_persist = utils_graph._persist_graph_updates

    async def _noop(*a, **k):
        return None

    # _edit_entity_impl resolves _persist_graph_updates from module globals at call
    # time, so this keeps the full edit semantics minus the per-edit graphml rewrite.
    utils_graph._persist_graph_updates = _noop
    try:
        for name in todo:
            await utils_graph._edit_entity_impl(
                graph, rag.entities_vdb, rag.relationships_vdb, name,
                {"description": entries[name]["new"]},
                entity_chunks_storage=rag.entity_chunks,
                relation_chunks_storage=rag.relation_chunks)
            if name in res["applied"]:
                log(f"[applied] {name}")
    except BaseException:
        await _drop_pending(rag)
        raise
    finally:
        utils_graph._persist_graph_updates = orig_persist

    if before_persist is not None:
        await before_persist()
    # second-writer guard. ponytail: catches a writer between load and here only, not
    # one racing the persist itself; the launcher stops the server and runs one job.
    if _stat(graph_path) != stat0:
        log(f"ABORT: {graph_path} changed on disk since load; nothing written")
        await _drop_pending(rag)
        return 6, res

    await orig_persist(
        entities_vdb=rag.entities_vdb,
        relationships_vdb=rag.relationships_vdb,
        chunk_entity_relation_graph=graph,
        entity_chunks_storage=rag.entity_chunks,
        relation_chunks_storage=rag.relation_chunks)
    return 0, res


def summary(res):
    return ("APPLY SUMMARY applied={} already={} gone={} changed={} rollback={}"
            .format(len(res["applied"]), len(res["already"]), len(res["gone"]),
                    len(res["changed"]), res["rollback"] or "none"))


def _preflight():
    """Everything that can refuse, before any storage is built. Returns (url, collection, dim)."""
    import httpx
    import ragbase
    from migrate_nano_to_qdrant import collection_name
    from qdrant_client import QdrantClient

    def fail(msg):
        print(f"PREFLIGHT FAILED: {msg}", flush=True)
        sys.exit(3)

    # ponytail: Qdrant bases only; a nano base has no independent reader to verify with
    if ragbase.env_value("LIGHTRAG_VECTOR_STORAGE") != "QdrantVectorDBStorage":
        fail("LIGHTRAG_VECTOR_STORAGE is not QdrantVectorDBStorage")
    url = ragbase.env_value("QDRANT_URL") or ""
    # loopback only, and exported: a missing URL would make LightRAG's Qdrant storage
    # fall back to a silent local/in-memory store instead of the served collections
    if not url.startswith("http://127.0.0.1:"):
        fail("QDRANT_URL must start with http://127.0.0.1:")
    os.environ["QDRANT_URL"] = url
    model, dim = ragbase.env_value("EMBEDDING_MODEL"), ragbase.env_value("EMBEDDING_DIM")
    if not model or not dim or not dim.isdigit():
        fail("EMBEDDING_MODEL / EMBEDDING_DIM missing from .env")
    try:
        with httpx.Client(trust_env=False, timeout=5) as c:
            c.get("http://127.0.0.1:11434/api/version").raise_for_status()
    except Exception as exc:
        fail(f"Ollama not reachable on 127.0.0.1:11434 ({type(exc).__name__}: {exc})")
    coll = collection_name("entities", model, int(dim))
    try:
        ok = QdrantClient(url=url, timeout=30).collection_exists(coll)
    except Exception as exc:
        fail(f"Qdrant not reachable at {url} ({type(exc).__name__}: {exc})")
    if not ok:
        fail(f"collection {coll} does not exist")
    return url, coll, int(dim)


def _verify(rag, names, entries, url, coll, dim, graph_path):
    """Independent readers (fresh Qdrant client, fresh graphml parse). Returns mismatches."""
    import networkx as nx
    from qdrant_client import QdrantClient
    from lightrag.kg.qdrant_impl import compute_mdhash_id_for_qdrant
    from lightrag.utils import compute_mdhash_id

    bad = []
    ws = rag.entities_vdb.effective_workspace
    pid = {compute_mdhash_id_for_qdrant(compute_mdhash_id(n, prefix="ent-"), prefix=ws): n
           for n in names}
    client = QdrantClient(url=url, timeout=120)
    ids = list(pid)
    got = {}
    for i in range(0, len(ids), VERIFY_BATCH):
        for r in client.retrieve(coll, ids=ids[i:i + VERIFY_BATCH],
                                 with_payload=True, with_vectors=True):
            got[uuid.UUID(str(r.id)).hex] = r
    for i, n in pid.items():
        r = got.get(i)
        if r is None:
            bad.append(f"{n}: qdrant point missing")
            continue
        want = n + "\n" + entries[n]["new"]
        if (r.payload or {}).get("content") != want:
            bad.append(f"{n}: qdrant payload content differs from name+description")
        if r.vector is None or len(r.vector) != dim:
            bad.append(f"{n}: qdrant vector missing or not {dim}-dim")

    g = nx.read_graphml(graph_path)
    for n in names:
        if n not in g.nodes:
            bad.append(f"{n}: node missing from re-parsed graphml")
        elif g.nodes[n].get("description") != entries[n]["new"]:
            bad.append(f"{n}: graphml description differs from new")
    return bad


async def run(path, entries):
    url, coll, dim = _preflight()
    import ragbase
    import rag_ingest
    from lightrag import LightRAG

    kw = {}
    vs = ragbase.env_value("LIGHTRAG_VECTOR_STORAGE")
    if vs:
        kw["vector_storage"] = vs
    rag = LightRAG(working_dir=ragbase.STORAGE,
                   embedding_func=rag_ingest.embedding_func,
                   llm_model_func=rag_ingest.llm_model_func, **kw)
    await rag.initialize_storages()
    graph_path = rag.chunk_entity_relation_graph._graphml_xml_file
    stat0 = _stat(graph_path)
    rollback = f"{path}.rollback.{time.strftime('%Y%m%d-%H%M%S')}.json"
    try:
        code, res = await apply(rag, entries, graph_path, stat0, rollback)
    finally:
        # an abort/exception path already dropped its pending vectors, so this only
        # flushes what a completed persist left or retries a failed one
        await rag.finalize_storages()
    print(summary(res), flush=True)
    if code:
        return code
    todo = res["applied"] + res["already"]
    if todo:
        bad = _verify(rag, todo, entries, url, coll, dim, graph_path)
        if bad:
            for b in bad:
                print(f"VERIFY MISMATCH: {b}", flush=True)
            print(f"rollback file: {res['rollback']}", flush=True)
            return 5
        print(f"SELF-VERIFY OK ({len(todo)} entities, qdrant + graphml)", flush=True)
    return 0


# --- selftest ---------------------------------------------------------------

async def _selftest():
    import hashlib
    import shutil
    import tempfile

    import numpy as np
    from lightrag import LightRAG
    from lightrag.utils import EmbeddingFunc, compute_mdhash_id

    async def fake_embed(texts, **kw):
        out = []
        for t in texts:
            seed = int.from_bytes(hashlib.md5(t.encode("utf-8")).digest()[:4], "little")
            out.append(np.random.default_rng(seed).random(8, dtype=np.float32) + 0.1)
        return np.stack(out)

    async def dummy_llm(*a, **k):
        return ""

    tmp = tempfile.mkdtemp(prefix="apply_desc_selftest_")
    try:
        rag = LightRAG(working_dir=tmp, vector_storage="NanoVectorDBStorage",
                       embedding_func=EmbeddingFunc(embedding_dim=8, max_token_size=512,
                                                    func=fake_embed),
                       llm_model_func=dummy_llm)
        await rag.initialize_storages()
        graph = rag.chunk_entity_relation_graph
        gp = graph._graphml_xml_file
        vdb = rag.entities_vdb

        seed = {"G": "g-orig", "C": "c-orig", "A": "a-new"}
        for n, d in seed.items():
            await graph.upsert_node(n, {"entity_id": n, "entity_type": "t",
                                        "description": d, "source_id": "chunk-1"})
        await graph.index_done_callback()

        calls = {"n": 0}
        real_cb = graph.index_done_callback

        async def counted():
            calls["n"] += 1
            return await real_cb()

        graph.index_done_callback = counted

        def vid(n):
            return compute_mdhash_id(n, prefix="ent-")

        entries = {
            "G": {"old": "g-orig", "new": "g-new"},        # applied
            "C": {"old": "c-STALE", "new": "c-new"},       # changed
            "A": {"old": "whatever", "new": "a-new"},      # already
            "X": {"old": "", "new": "x-new"},              # gone
        }
        logs = []
        code, res = await apply(rag, entries, gp, _stat(gp),
                                os.path.join(tmp, "f.rollback.json"), log=logs.append)
        assert code == 0, code
        assert (res["applied"], res["already"], res["gone"], res["changed"]) == \
            (["G"], ["A"], ["X"], ["C"]), res
        assert {"[gone] X", "[changed] C", "[already] A", "[applied] G"} <= set(logs), logs
        assert (await graph.get_node("G"))["description"] == "g-new"
        assert (await graph.get_node("C"))["description"] == "c-orig"
        assert (await graph.get_node("A"))["description"] == "a-new"
        assert (await vdb.get_by_id(vid("G")))["content"] == "G\ng-new"
        assert (await vdb.get_by_id(vid("A")))["content"] == "A\na-new"
        assert await vdb.get_by_id(vid("C")) is None, "changed entry must not be embedded"
        assert calls["n"] == 1, f"graph persisted {calls['n']} times, want 1"
        assert os.path.exists(res["rollback"])
        assert "APPLY SUMMARY applied=1 already=1 gone=1 changed=1" in summary(res)

        # rollback file is itself a valid FILE and restores graph + vector
        back = load_entries(res["rollback"])
        assert back["G"] == {"old": "g-new", "new": "g-orig"}, back
        code, res2 = await apply(rag, back, gp, _stat(gp),
                                 os.path.join(tmp, "f2.rollback.json"), log=lambda m: None)
        assert code == 0 and res2["applied"] == ["G"] and res2["already"] == ["A"], res2
        assert (await graph.get_node("G"))["description"] == "g-orig"
        assert (await vdb.get_by_id(vid("G")))["content"] == "G\ng-orig"

        # mtime guard: a second writer touches the graphml before persist -> exit 6
        touched = {}

        async def second_writer():
            with open(gp, "rb") as f:
                data = f.read()
            with open(gp, "wb") as f:
                f.write(data + b"\n")
            touched["bytes"] = data + b"\n"

        calls["n"] = 0
        code, res3 = await apply(rag, {"G": {"old": "g-orig", "new": "g-guard"}}, gp,
                                 _stat(gp), os.path.join(tmp, "f3.rollback.json"),
                                 log=lambda m: None, before_persist=second_writer)
        assert code == 6, code
        assert calls["n"] == 0, "guard must abort before any persist"
        with open(gp, "rb") as f:
            assert f.read() == touched["bytes"], "graphml written despite guard"
        assert (await vdb.get_by_id(vid("G")))["content"] == "G\ng-orig", "vector leaked"
        await rag.finalize_storages()

        # FILE validation
        def rejects(text):
            p = os.path.join(tmp, "bad.json")
            with open(p, "w", encoding="utf-8") as f:
                f.write(text)
            try:
                load_entries(p)
            except ValueError:
                return True
            return False

        assert rejects('{"a": {"old": "", "new": "x"}, "a": {"old": "", "new": "y"}}')
        assert rejects('[]')
        assert rejects('{"a": {"old": "", "new": "  "}}')
        assert rejects('{"a": {"old": 1, "new": "x"}}')
        assert not rejects('{"a": {"old": "", "new": "x"}}')
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("selftest OK")


def main(argv):
    if argv[1:] == ["--selftest"]:
        asyncio.run(_selftest())
        return 0
    if len(argv) != 2:
        print(__doc__)
        return 2
    try:
        entries = load_entries(argv[1])
    except (ValueError, OSError) as exc:
        print(f"INVALID FILE {argv[1]}: {exc}", flush=True)
        return 2
    if not entries:
        print("nothing to apply", flush=True)
        return 0
    return asyncio.run(run(argv[1], entries))


if __name__ == "__main__":
    sys.exit(main(sys.argv))
