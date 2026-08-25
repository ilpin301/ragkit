"""Repair a RAG base's vdb: embed graph entities/edges and text chunks that have no vector, drop stale vectors.
Container MUST be stopped. Dry run by default; pass --apply to write.
"""
import base64, hashlib, json, os, sys, time, urllib.request, zlib
import xml.etree.ElementTree as ET
import numpy as np
from nano_vectordb import NanoVectorDB

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ragbase

os.environ["NO_PROXY"] = "*"
BASE = ragbase.STORAGE
NS = "{http://graphml.graphdrawing.org/xmlns}"
DIM, APPLY = int(ragbase.require_env("EMBEDDING_DIM")), "--apply" in sys.argv
NL, TAB, CAP = chr(10), chr(9), 8000
OLLAMA = "http://localhost:11434/api/embed"

def mdid(s, prefix):
    return prefix + hashlib.md5(s.encode()).hexdigest()

def embed(texts, batch=10):
    out = []
    for i in range(0, len(texts), batch):
        body = json.dumps({"model": "bge-m3", "input": texts[i:i + batch]}).encode()
        req = urllib.request.Request(OLLAMA, body, {"Content-Type": "application/json"})
        r = json.load(urllib.request.urlopen(req, timeout=300))
        out.extend(r["embeddings"])
        print("  embedded", min(i + batch, len(texts)), "/", len(texts), flush=True)
    a = np.array(out, dtype=np.float32)
    assert a.shape == (len(texts), DIM), a.shape
    return a

def pack(vec):
    return base64.b64encode(zlib.compress(vec.astype(np.float16).tobytes())).decode()

# --- parse graph ---
print("parsing graph...", flush=True)
root = ET.parse(BASE + "/graph_chunk_entity_relation.graphml").getroot()
km = {k.get("id"): k.get("attr.name") for k in root.findall(NS + "key")}
def attrs(el):
    return {km[d.get("key")]: (d.text or "") for d in el.findall(NS + "data") if d.get("key") in km}
g = root.find(NS + "graph")
nodes = {n.get("id"): attrs(n) for n in g.findall(NS + "node")}
edges = {}
for e in g.findall(NS + "edge"):
    s, t = e.get("source"), e.get("target")
    if s > t: s, t = t, s
    edges[(s, t)] = attrs(e)
print("graph:", len(nodes), "nodes,", len(edges), "edges", flush=True)

ent = NanoVectorDB(DIM, storage_file=BASE + "/vdb_entities.json")
rel = NanoVectorDB(DIM, storage_file=BASE + "/vdb_relationships.json")
chk = NanoVectorDB(DIM, storage_file=BASE + "/vdb_chunks.json")
est = ent._NanoVectorDB__storage
rst = rel._NanoVectorDB__storage
cst = chk._NanoVectorDB__storage
eids = set(d["__id__"] for d in est["data"])
rids = set(d["__id__"] for d in rst["data"])
cids = set(d["__id__"] for d in cst["data"])
print("vdb:", len(eids), "entities,", len(rids), "relations,", len(cids), "chunks", flush=True)

with open(BASE + "/kv_store_text_chunks.json", encoding="utf-8") as f:
    kvchunks = json.load(f)
print("kv:", len(kvchunks), "chunks", flush=True)

now = int(time.time())
ent_new, ent_txt = [], []
for name, a in nodes.items():
    i = mdid(name, "ent-")
    if i in eids: continue
    c = (name + NL + a.get("description", ""))[:CAP]
    rec = {"__id__": i, "__created_at__": now, "entity_name": name,
           "content": c, "source_id": a.get("source_id", "")}
    if a.get("file_path"): rec["file_path"] = a["file_path"]
    ent_new.append(rec); ent_txt.append(c)

rel_new, rel_txt = [], []
for (s, t), a in edges.items():
    i = mdid(s + t, "rel-")
    if i in rids or mdid(t + s, "rel-") in rids: continue
    c = (a.get("keywords", "") + TAB + s + NL + t + NL + a.get("description", ""))[:CAP]
    rec = {"__id__": i, "__created_at__": now, "src_id": s, "tgt_id": t,
           "content": c, "source_id": a.get("source_id", "")}
    if a.get("file_path"): rec["file_path"] = a["file_path"]
    rel_new.append(rec); rel_txt.append(c)

chk_new, chk_txt = [], []
for i, a in kvchunks.items():
    if i in cids: continue
    c = (a.get("content", ""))[:CAP]
    rec = {"__id__": i, "__created_at__": now, "content": c,
           "full_doc_id": a.get("full_doc_id", ""), "file_path": a.get("file_path", "")}
    chk_new.append(rec); chk_txt.append(c)

stale_e = [d["__id__"] for d in est["data"] if d.get("entity_name") not in nodes]
gk = set(edges)
stale_r = [d["__id__"] for d in rst["data"]
           if (d.get("src_id"), d.get("tgt_id")) not in gk
           and (d.get("tgt_id"), d.get("src_id")) not in gk]
stale_c = [d["__id__"] for d in cst["data"] if d["__id__"] not in kvchunks]
print("TO ADD: entities", len(ent_new), " relations", len(rel_new), " chunks", len(chk_new))
print("TO DROP: entities", len(stale_e), " relations", len(stale_r), " chunks", len(stale_c))
if not APPLY:
    print("dry run; re-run with --apply"); sys.exit(0)

for path in ("vdb_entities.json", "vdb_relationships.json", "vdb_chunks.json"):
    bak = BASE + "/" + path + ".bak"
    if not os.path.exists(bak):
        import shutil; shutil.copy2(BASE + "/" + path, bak); print("backup ->", bak, flush=True)

if ent_new:
    print("embedding entities...", flush=True)
    for r, v in zip(ent_new, embed(ent_txt)):
        r["vector"] = pack(v); r["__vector__"] = v
if rel_new:
    print("embedding relations...", flush=True)
    for r, v in zip(rel_new, embed(rel_txt)):
        r["vector"] = pack(v); r["__vector__"] = v

if chk_new:
    print("embedding chunks...", flush=True)
    for r, v in zip(chk_new, embed(chk_txt)):
        r["vector"] = pack(v); r["__vector__"] = v

if stale_e: ent.delete(stale_e)
if stale_r: rel.delete(stale_r)
if stale_c: chk.delete(stale_c)
if ent_new: ent.upsert(ent_new)
if rel_new: rel.upsert(rel_new)
if chk_new: chk.upsert(chk_new)
print("saving...", flush=True)
ent.save(); rel.save(); chk.save()
print("DONE entities", len(est["data"]), est["matrix"].shape,
      "| relations", len(rst["data"]), rst["matrix"].shape,
      "| chunks", len(cst["data"]), cst["matrix"].shape)
