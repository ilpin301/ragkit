"""Drop cached query ANSWERS from a base's LLM response cache, keep extraction.

Why this exists: after any change to retrieval -- fixing broken embeddings,
switching the vector backend -- the cache will happily replay the answer it
computed from the OLD retrieval. A before/after comparison then "passes"
without the new index ever being touched.

Keys are `<mode>:<type>:<hash>`. Two things must be KEPT:

  default:*        entity/relationship extraction and summaries. Real LLM
                   spend per document, nothing to do with retrieval.
  <mode>:keywords  the high/low-level keywords extracted FROM THE QUESTION.
                   Pure function of the query text -- a vector-store change
                   cannot poison it. Dropping it makes a before/after
                   comparison meaningless, because keyword extraction is a
                   non-deterministic LLM call: the second run then searches
                   with DIFFERENT terms and the diff measures that, not the
                   index. (Learned the hard way on the PCM migration.)

Only `<mode>:query` -- the cached final answer -- is dropped.

    RAGBASE_ROOT=<base> python purge_query_cache.py [--apply] [--drop-keywords]

Dry run by default. Stop the container first: this rewrites a store file.
"""
import argparse
import collections
import json
import os
import shutil
import sys

import ragbase

CACHE = "kv_store_llm_response_cache.json"
KEEP_PREFIX = "default"
KEEP_TYPE = "keywords"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="actually rewrite the file")
    ap.add_argument("--drop-keywords", action="store_true",
                    help="also drop <mode>:keywords -- breaks before/after comparisons")
    args = ap.parse_args()

    path = os.path.join(ragbase.STORAGE, CACHE)
    if not os.path.exists(path):
        print("no cache at {} - nothing to do".format(path))
        return 0

    with open(path, encoding="utf-8") as f:
        cache = json.load(f)

    def keeper(key, val):
        if key.split(":", 1)[0] == KEEP_PREFIX:
            return True
        if not args.drop_keywords and (val or {}).get("cache_type") == KEEP_TYPE:
            return True
        return False

    counts = collections.Counter(
        (k.split(":", 1)[0], (v or {}).get("cache_type")) for k, v in cache.items())
    keep = {k: v for k, v in cache.items() if keeper(k, v)}
    drop = len(cache) - len(keep)

    print("cache {}".format(path))
    for (prefix, ctype), n in sorted(counts.items(), key=lambda x: str(x[0])):
        sample = next(k for k, v in cache.items()
                      if k.split(":", 1)[0] == prefix and (v or {}).get("cache_type") == ctype)
        verb = "KEEP" if keeper(sample, cache[sample]) else "DROP"
        print("  {} {:<10} {:<10} {:>6}".format(verb, prefix, str(ctype), n))
    print("  -> {} of {} entries removed".format(drop, len(cache)))

    if not drop:
        print("nothing to drop")
        return 0
    if not args.apply:
        print("\ndry run only -- re-run with --apply")
        return 0

    # keep a copy: extraction entries are expensive to regenerate
    shutil.copy2(path, path + ".bak")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(keep, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    print("rewrote {} ({} entries kept, backup at {}.bak)".format(path, len(keep), CACHE))
    return 0


if __name__ == "__main__":
    sys.exit(main())
