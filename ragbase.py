"""Resolve the RAG base this run targets.

Every kit script is base-agnostic: the base root arrives as the RAGBASE_ROOT
environment variable (ingest.ps1 -Root sets it). Nothing here guesses a path --
an unset or wrong root would silently write into the wrong store.
"""
import os
import sys


def _root():
    r = os.environ.get("RAGBASE_ROOT")
    if not r:
        sys.exit("RAGBASE_ROOT is not set - run through ragkit/ingest.ps1 -Root <base>")
    r = os.path.abspath(r)
    if not os.path.isdir(os.path.join(r, "lightrag")):
        sys.exit(f"RAGBASE_ROOT={r} has no lightrag/ subdirectory - not a RAG base")
    return r


ROOT = _root()
LIGHTRAG = os.path.join(ROOT, "lightrag")
STORAGE = os.path.join(LIGHTRAG, "data", "rag_storage")
DATA = os.path.join(LIGHTRAG, "data")


def env_value(key):
    """Read one KEY=value from <root>/lightrag/.env. Returns None if absent."""
    path = os.path.join(LIGHTRAG, ".env")
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith(f"{key}="):
                    return line.split("=", 1)[1].strip()
    except OSError:
        return None
    return None


def require_env(key):
    """Same, but fail loudly. A wrong default (e.g. dim 1024) poisons every check."""
    v = env_value(key)
    if v is None:
        sys.exit(f"{key} is missing from {os.path.join(LIGHTRAG, '.env')} - refusing to guess")
    return v


def demo():
    assert os.path.isdir(ROOT), ROOT
    assert STORAGE.startswith(ROOT)
    assert require_env("EMBEDDING_DIM").isdigit()
    assert env_value("__no_such_key__") is None
    print("ragbase self-check OK:", ROOT)


if __name__ == "__main__":
    demo()
